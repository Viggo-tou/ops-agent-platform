"""Multi-turn agent loop for codegen (Tier 4 main course).

Replaces the static pipeline's "one-shot codegen + retry" with a
turn-based loop where the model:

  1. Reads files / searches symbols / lists directories on demand
  2. Reasons over what it found
  3. Emits a diff (Aider search/replace blocks)
  4. Optionally requests verification (run_tests, deferred to a later
     wiring step that needs the swebench Docker evaluator)
  5. Iterates if verification fails

The model speaks via **markdown-fenced JSON tool calls** — the
Aider-style protocol that DeepSeek handles well per v3-v12 evidence.
Unlike OpenAI function-calling JSON, this works on any LLM that can
emit text, doesn't depend on provider SDK features, and maps cleanly
onto our existing Aider response parser.

## Tool protocol

The model emits a tool call like:

    ## TOOL_CALL
    ```json
    {"tool": "read_file", "args": {"path": "django/db/models/sql/query.py", "line_start": 200, "line_end": 280}}
    ```

The harness parses the JSON, executes the tool, and replies in the
next turn with:

    ## TOOL_RESULT
    ```json
    {"tool": "read_file", "ok": true, "content": "..."}
    ```

The model can also emit a final diff in Aider format (the existing
``<<<<<<< SEARCH`` blocks). When the harness detects a diff response,
it stops the loop and returns the diff as the final ``CodegenResult``.

Terminal markers (``## DONE``, ``## CANNOT_PROCEED``) work the same as
in the static pipeline.

## Budget

- ``max_turns`` (default 12): hard cap on round-trip count
- ``max_seconds`` (default 600): wall-clock cap
- No cost cap in MVP per 2026-05-10 product decision

## Why not OpenAI function calling

DeepSeek's `/v1/chat/completions` function-calling support varies by
model version. Markdown-fenced JSON has zero provider coupling, plays
to DeepSeek's strong markdown training, and parses identically to
the existing Aider format. Anthropic's tool-use is XML-based; we
keep planner (Claude Code CLI) on its native XML and codegen
(DeepSeek) on Markdown-JSON — different formats per role, both
strong.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("agent_loop")


_TOOL_CALL_RE = re.compile(
    r"##\s*TOOL[_ ]?CALL\s*\n+```(?:json)?\s*(?P<body>\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_TERMINAL_DONE_RE = re.compile(r"##\s*DONE\b", re.IGNORECASE)
_TERMINAL_CANNOT_RE = re.compile(r"##\s*CANNOT[_\- ]?PROCEED\b", re.IGNORECASE)
_AIDER_DIFF_RE = re.compile(r"<{4,}\s*SEARCH\b", re.IGNORECASE)


# --- Data model -------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]
    raw: str = ""


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    content: Any = None
    error: str = ""


@dataclass(frozen=True)
class Turn:
    index: int
    role: str                 # "model" | "tool" | "system"
    content: str = ""
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    elapsed_seconds: float = 0.0


@dataclass
class AgentSessionState:
    task_id: str
    turns: list[Turn] = field(default_factory=list)
    final_diff: str = ""
    terminated_reason: str = ""    # "done" | "cannot_proceed" | "budget_turns" | "budget_seconds" | "error" | "diff_emitted"
    started_at: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0


@dataclass
class AgentLoopBudget:
    max_turns: int = 12
    max_seconds: float = 600.0
    # Per-tool soft caps. The model can call a tool more than this, but
    # subsequent calls return a terse warning instead of the real
    # result, nudging the model to commit to a diff.
    soft_max_calls_per_tool: dict[str, int] = field(
        default_factory=lambda: {
            "read_file": 8,
            "search_symbol": 4,
            "list_directory": 3,
            "apply_diff": 1,
        }
    )


# --- Tool registry ---------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: dict[str, str]   # arg_name -> "required str" / "optional int" etc.
    handler: Callable[[dict[str, Any], "AgentLoopContext"], ToolResult]


class AgentLoopContext:
    """Bag of dependencies the tool handlers need.

    Kept as a single object so adding a new tool doesn't require
    threading new args through every callsite.
    """

    def __init__(
        self,
        *,
        sandbox_dir: Path | None,
        repo_root: Path | None,
        candidate_files: dict[str, str],
        max_read_bytes: int = 8_000,
        max_search_hits: int = 12,
    ) -> None:
        self.sandbox_dir = sandbox_dir
        self.repo_root = repo_root
        self.candidate_files = dict(candidate_files or {})
        self.max_read_bytes = max_read_bytes
        self.max_search_hits = max_search_hits


def _safe_resolve(base: Path | None, rel: str) -> Path | None:
    """Resolve ``rel`` against ``base`` if it stays inside ``base``.

    Path-traversal hardening: any attempt to escape via ``..`` returns
    None. Without ``base`` returns None.
    """
    if base is None:
        return None
    try:
        target = (base / rel).resolve()
        target.relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return target


def _tool_read_file(args: dict[str, Any], ctx: AgentLoopContext) -> ToolResult:
    path = str(args.get("path") or "").strip()
    if not path:
        return ToolResult(tool="read_file", ok=False, error="missing path")
    line_start = args.get("line_start")
    line_end = args.get("line_end")

    # Try candidate_files (in-memory) first; falls back to sandbox/repo
    # disk read for files the harness didn't pre-load.
    text: str | None = ctx.candidate_files.get(path)
    if text is None:
        for base in (ctx.sandbox_dir, ctx.repo_root):
            target = _safe_resolve(base, path)
            if target is not None and target.is_file():
                try:
                    text = target.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    return ToolResult(
                        tool="read_file",
                        ok=False,
                        error=f"read failed: {exc}",
                    )
                break
    if text is None:
        return ToolResult(
            tool="read_file",
            ok=False,
            error=f"file not found: {path}",
        )

    if line_start is not None or line_end is not None:
        lines = text.splitlines(keepends=True)
        try:
            ls = max(1, int(line_start)) if line_start is not None else 1
            le = int(line_end) if line_end is not None else len(lines)
            le = min(le, len(lines))
        except (TypeError, ValueError):
            return ToolResult(
                tool="read_file",
                ok=False,
                error="line_start/line_end must be integers",
            )
        text = "".join(lines[ls - 1 : le])

    if len(text) > ctx.max_read_bytes:
        text = text[: ctx.max_read_bytes] + f"\n... (truncated at {ctx.max_read_bytes} bytes; request a smaller line range to see more)"
    return ToolResult(
        tool="read_file",
        ok=True,
        content={"path": path, "text": text, "bytes": len(text)},
    )


_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:async\s+)?(?:def|class)\s+(?P<name>\w+)\s*[\(:]",
    re.MULTILINE,
)


def _tool_search_symbol(args: dict[str, Any], ctx: AgentLoopContext) -> ToolResult:
    """Find files where a Python identifier is defined or referenced.

    Searches ``candidate_files`` first (cheap), then walks ``repo_root``
    if provided. Returns up to ``max_search_hits`` (file_path, line) pairs.
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolResult(tool="search_symbol", ok=False, error="missing name")
    file_glob = str(args.get("file_glob") or "").strip()

    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits: list[dict[str, Any]] = []

    def consider(path_str: str, content: str) -> None:
        if file_glob and not _glob_match(path_str, file_glob):
            return
        for line_no, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                hits.append({"path": path_str, "line": line_no, "preview": line.strip()[:160]})
                if len(hits) >= ctx.max_search_hits:
                    return

    for path_str, content in ctx.candidate_files.items():
        consider(path_str, content)
        if len(hits) >= ctx.max_search_hits:
            break

    if len(hits) < ctx.max_search_hits and ctx.repo_root and ctx.repo_root.is_dir():
        for target in ctx.repo_root.rglob("*.py"):
            if len(hits) >= ctx.max_search_hits:
                break
            if any(part in {".git", "node_modules", "__pycache__", ".venv", "build", "dist"} for part in target.parts):
                continue
            try:
                rel = target.relative_to(ctx.repo_root).as_posix()
            except ValueError:
                continue
            if rel in ctx.candidate_files:
                continue  # already considered above
            if file_glob and not _glob_match(rel, file_glob):
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            consider(rel, content)

    return ToolResult(
        tool="search_symbol",
        ok=True,
        content={"name": name, "hits": hits, "truncated": len(hits) >= ctx.max_search_hits},
    )


def _glob_match(path: str, pattern: str) -> bool:
    """Lightweight glob: supports ``*`` and ``**`` only."""
    from fnmatch import fnmatchcase

    if "**" in pattern:
        # Simple ** support: replace with .*
        re_pat = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return bool(re.fullmatch(re_pat, path))
    return fnmatchcase(path, pattern)


def _tool_list_directory(args: dict[str, Any], ctx: AgentLoopContext) -> ToolResult:
    path = str(args.get("path") or ".").strip() or "."
    base = ctx.sandbox_dir or ctx.repo_root
    if base is None:
        return ToolResult(
            tool="list_directory",
            ok=False,
            error="no sandbox or repo root available",
        )
    target = _safe_resolve(base, path)
    if target is None or not target.is_dir():
        return ToolResult(
            tool="list_directory",
            ok=False,
            error=f"not a directory: {path}",
        )
    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(target.iterdir()):
            entries.append({
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
            })
            if len(entries) >= 200:
                break
    except OSError as exc:
        return ToolResult(
            tool="list_directory",
            ok=False,
            error=f"listdir failed: {exc}",
        )
    return ToolResult(
        tool="list_directory",
        ok=True,
        content={"path": path, "entries": entries},
    )


def _tool_apply_diff(args: dict[str, Any], ctx: AgentLoopContext) -> ToolResult:
    """Validate and 'apply' Aider blocks in-memory; the actual file
    apply happens at the boundary by the existing aider_format module.

    The agent loop uses this to mark "I have a candidate diff; please
    accept or run gates on it". The returned content is the unified
    diff (post-Aider conversion) so the caller can persist it directly.
    """
    raw_blocks = args.get("blocks")
    if not isinstance(raw_blocks, str) or not raw_blocks.strip():
        return ToolResult(
            tool="apply_diff",
            ok=False,
            error="missing or empty 'blocks' argument (expected Aider search/replace blocks as a string)",
        )
    try:
        from app.services.aider_format import (
            aider_blocks_to_unified_diff,
            apply_aider_blocks_in_memory,
            parse_aider_blocks,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="apply_diff",
            ok=False,
            error=f"aider_format import failed: {exc}",
        )
    try:
        blocks = parse_aider_blocks(raw_blocks)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="apply_diff",
            ok=False,
            error=f"parse failed: {exc}",
        )
    apply_result = apply_aider_blocks_in_memory(blocks, ctx.candidate_files)
    if apply_result.errors:
        reasons = "; ".join(f"#{e.block_index}:{e.reason}" for e in apply_result.errors[:3])
        return ToolResult(
            tool="apply_diff",
            ok=False,
            error=f"apply failed: {reasons}",
        )
    diff = aider_blocks_to_unified_diff(apply_result)
    return ToolResult(
        tool="apply_diff",
        ok=True,
        content={"unified_diff": diff, "files": list(apply_result.before_after.keys())},
    )


_DEFAULT_TOOLS: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file",
        description="Read a file's contents (or a line range). Args: path (str), line_start (int, optional), line_end (int, optional).",
        args_schema={"path": "required str", "line_start": "optional int", "line_end": "optional int"},
        handler=_tool_read_file,
    ),
    "search_symbol": ToolSpec(
        name="search_symbol",
        description="Find lines where a Python identifier is defined or referenced. Args: name (str), file_glob (str, optional).",
        args_schema={"name": "required str", "file_glob": "optional str"},
        handler=_tool_search_symbol,
    ),
    "list_directory": ToolSpec(
        name="list_directory",
        description="List entries in a directory inside the sandbox or repo. Args: path (str, default '.').",
        args_schema={"path": "optional str"},
        handler=_tool_list_directory,
    ),
    "apply_diff": ToolSpec(
        name="apply_diff",
        description="Submit Aider search/replace blocks as your final patch. Args: blocks (str — the SEARCH/REPLACE blocks).",
        args_schema={"blocks": "required str"},
        handler=_tool_apply_diff,
    ),
}


# --- Response parsing -------------------------------------------------------


def parse_model_response(text: str) -> tuple[ToolCall | None, str]:
    """Detect a TOOL_CALL block in the model response.

    Returns ``(tool_call, terminal_marker)`` where:
      - ``tool_call`` is the parsed ToolCall or None
      - ``terminal_marker`` is "done" / "cannot_proceed" / ""

    A model response may contain neither (caller treats as
    "nothing actionable") or both (the marker wins; we honor terminal
    markers as the explicit signal).
    """
    if not text:
        return None, ""
    if _TERMINAL_DONE_RE.search(text):
        return None, "done"
    if _TERMINAL_CANNOT_RE.search(text):
        return None, "cannot_proceed"
    m = _TOOL_CALL_RE.search(text)
    if m is None:
        return None, ""
    body = m.group("body")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(parsed, dict):
        return None, ""
    tool = str(parsed.get("tool") or "").strip()
    if not tool:
        return None, ""
    args = parsed.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    return ToolCall(tool=tool, args=args, raw=body), ""


def render_tool_result_for_prompt(result: ToolResult) -> str:
    """Format a ToolResult as the next-turn user message.

    The model sees this exactly — kept structurally identical to the
    tool-call format so the model can mimic it and we can extend later.
    """
    payload: dict[str, Any] = {"tool": result.tool, "ok": result.ok}
    if result.ok:
        payload["content"] = result.content
    else:
        payload["error"] = result.error
    return (
        "## TOOL_RESULT\n"
        + "```json\n"
        + json.dumps(payload, ensure_ascii=False, default=str)[:8000]
        + "\n```\n"
    )


def render_system_prompt(tools: dict[str, ToolSpec]) -> str:
    """Build the agent-loop system prompt that explains the protocol."""
    tool_blurb_lines = []
    for spec in tools.values():
        args_str = ", ".join(f"{k}: {v}" for k, v in spec.args_schema.items())
        tool_blurb_lines.append(f"- `{spec.name}`: {spec.description} Args: {args_str}.")
    tools_doc = "\n".join(tool_blurb_lines)
    return (
        "You are a code-generation agent. You have access to a small set of "
        "tools. To call one, emit:\n\n"
        "## TOOL_CALL\n"
        "```json\n"
        '{"tool": "TOOL_NAME", "args": {...}}\n'
        "```\n\n"
        "Available tools:\n"
        f"{tools_doc}\n\n"
        "After each tool call, the harness will respond with:\n\n"
        "## TOOL_RESULT\n"
        "```json\n"
        '{"tool": ..., "ok": true|false, "content": ..., "error": ...}\n'
        "```\n\n"
        "Iterate as needed. To finalise your patch, emit one last call:\n\n"
        "## TOOL_CALL\n"
        "```json\n"
        '{"tool": "apply_diff", "args": {"blocks": "<aider search/replace blocks>"}}\n'
        "```\n\n"
        "If you can produce the fix without further reads, you may go straight "
        "to apply_diff on the first turn.\n\n"
        "When the patch is in and the harness has accepted it, emit `## DONE`. "
        "If you genuinely cannot proceed (no path forward even with more reads), "
        "emit `## CANNOT_PROCEED: <one-line reason>`.\n\n"
        "Aider search/replace block format:\n"
        "```\n"
        "filename.py\n"
        "<<<<<<< SEARCH\n"
        "exact verbatim source text\n"
        "=======\n"
        "new text\n"
        ">>>>>>> REPLACE\n"
        "```\n\n"
        "RULES:\n"
        "1. Only request files / symbols you can name from the task or prior "
        "tool results. Don't invent paths.\n"
        "2. Each SEARCH region must be a verbatim substring of the file as "
        "fetched via read_file (use line ranges to keep it tight).\n"
        "3. Do NOT emit free-form prose between turns. Every model response "
        "should be either a TOOL_CALL block or a terminal marker.\n"
        "4. Patch as small as possible. Re-read the file just before patching "
        "if anything changed.\n"
    )


# --- Loop driver ------------------------------------------------------------


@dataclass(frozen=True)
class AgentLoopResult:
    final_diff: str
    state: AgentSessionState
    terminated_reason: str


def run_agent_loop(
    *,
    task_id: str,
    user_prompt: str,
    llm_call: Callable[[str, list[dict[str, str]]], str],
    ctx: AgentLoopContext,
    tools: dict[str, ToolSpec] | None = None,
    budget: AgentLoopBudget | None = None,
) -> AgentLoopResult:
    """Drive a turn-based agent loop.

    ``llm_call(system_prompt, messages) -> response_text`` is provider-
    agnostic — the caller wraps DeepSeek / Claude / etc. ``messages``
    is a list of ``{role, content}`` dicts. The function returns the
    raw model response text; this module parses it.

    ``ctx`` carries the file/repo state that tools touch.
    ``tools`` defaults to the standard registry (read_file +
    search_symbol + list_directory + apply_diff).
    ``budget`` controls turn / time caps.

    Returns the final unified diff (empty on terminate-without-diff)
    plus the full session state for inspection / debugging.
    """
    tools = tools or dict(_DEFAULT_TOOLS)
    budget = budget or AgentLoopBudget()
    state = AgentSessionState(task_id=task_id, started_at=time.monotonic())
    system_prompt = render_system_prompt(tools)
    messages: list[dict[str, str]] = [
        {"role": "user", "content": user_prompt},
    ]
    per_tool_count: dict[str, int] = {}

    for turn_idx in range(budget.max_turns):
        if time.monotonic() - state.started_at > budget.max_seconds:
            state.terminated_reason = "budget_seconds"
            break

        turn_start = time.monotonic()
        try:
            response = llm_call(system_prompt, list(messages))
        except Exception as exc:  # noqa: BLE001
            state.terminated_reason = f"error: llm_call: {exc}"
            break
        if not response:
            state.terminated_reason = "error: empty response"
            break
        state.bytes_in += len(response)
        elapsed = time.monotonic() - turn_start

        tool_call, terminal = parse_model_response(response)
        state.turns.append(Turn(
            index=turn_idx,
            role="model",
            content=response,
            tool_call=tool_call,
            elapsed_seconds=elapsed,
        ))
        messages.append({"role": "assistant", "content": response})

        if terminal == "done":
            state.terminated_reason = "done"
            break
        if terminal == "cannot_proceed":
            state.terminated_reason = "cannot_proceed"
            break

        if tool_call is None:
            # Model produced narrative without a tool call. Nudge it
            # back on protocol with a brief reminder; counts as a turn.
            messages.append({
                "role": "user",
                "content": (
                    "Reminder: respond with `## TOOL_CALL` + JSON, "
                    "or `## DONE` / `## CANNOT_PROCEED`. Pure prose "
                    "is not actionable."
                ),
            })
            continue

        spec = tools.get(tool_call.tool)
        if spec is None:
            messages.append({
                "role": "user",
                "content": render_tool_result_for_prompt(
                    ToolResult(
                        tool=tool_call.tool,
                        ok=False,
                        error=f"unknown tool: {tool_call.tool}. Available: {sorted(tools)}",
                    )
                ),
            })
            continue

        # Soft per-tool quota. Past the threshold, return a terse
        # "you've already called this enough; commit to a diff" so
        # the model doesn't infinitely browse.
        cap = budget.soft_max_calls_per_tool.get(tool_call.tool, 99)
        used = per_tool_count.get(tool_call.tool, 0)
        if used >= cap:
            quota_result = ToolResult(
                tool=tool_call.tool,
                ok=False,
                error=(
                    f"soft limit reached for {tool_call.tool} "
                    f"({cap} calls). Commit to apply_diff with "
                    "what you have, or emit CANNOT_PROCEED."
                ),
            )
            state.turns.append(Turn(
                index=turn_idx,
                role="tool",
                content=tool_call.tool,
                tool_call=tool_call,
                tool_result=quota_result,
            ))
            messages.append({
                "role": "user",
                "content": render_tool_result_for_prompt(quota_result),
            })
            continue
        per_tool_count[tool_call.tool] = used + 1

        try:
            result = spec.handler(tool_call.args, ctx)
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(
                tool=tool_call.tool,
                ok=False,
                error=f"handler raised: {exc}",
            )
        state.turns.append(Turn(
            index=turn_idx,
            role="tool",
            content=spec.name,
            tool_call=tool_call,
            tool_result=result,
        ))

        # apply_diff success → final diff captured, end loop
        if tool_call.tool == "apply_diff" and result.ok:
            content = result.content or {}
            state.final_diff = str(content.get("unified_diff") or "")
            state.terminated_reason = "diff_emitted"
            break

        rendered = render_tool_result_for_prompt(result)
        state.bytes_out += len(rendered)
        messages.append({"role": "user", "content": rendered})
    else:
        state.terminated_reason = "budget_turns"

    return AgentLoopResult(
        final_diff=state.final_diff,
        state=state,
        terminated_reason=state.terminated_reason,
    )
