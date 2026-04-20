"""Agent-based intent resolution via Claude Code CLI and MCP tools."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, get_settings

_logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_TOOLS = ["mcp__jira__get_issue"]
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


@dataclass(frozen=True)
class ResolvedIntent:
    refined_text: str
    tool_calls_made: int
    sources_consulted: list[str]
    elapsed_seconds: float


class MCPNotConfiguredError(RuntimeError):
    """Raised when the requested MCP-backed intent resolver is unavailable."""


class IntentResolutionTimeoutError(TimeoutError):
    """Raised when Claude Code CLI does not finish before the timeout."""


_AGENT_SYSTEM_PROMPT = """\
You are an intent resolution agent. Your job is to figure out what the
user wants done, gather enough context to write a precise instruction,
and output that instruction.

WORKFLOW:
1. Read the user's message. It is usually a short reference like
   "complete P69-10", "fix #42", or "implement the design at [url]".
2. Determine what external sources you need to read.
   - Jira ticket reference -> call jira.get_issue.
   - GitHub issue reference -> call github.get_issue.
   - URL -> call web.fetch.
   - File path -> call file.read.
3. Read the external source.
4. If the source references other sources you need, fetch those too.
5. Once you have enough context, output a single block of plain text:
   the precise code operation instruction.

OUTPUT RULES:
- Plain text, no JSON, no markdown fences.
- In English, regardless of the input language.
- List each required change as a numbered step.
- Each step names: file path, operation, code element.
- Self-contained: readable without seeing the original sources.

CONSTRAINTS:
- Maximum {max_tool_calls} tool calls. Stop gathering and produce a
  best-effort output when that limit is reached.
- Do not generate code. Only describe what to change.
- Do not add requirements beyond what the sources describe.
- If the user's intent is genuinely ambiguous after reading all available
  sources, say what is clear and note what is ambiguous.
"""


_TOOL_ALIASES = {
    "jira.get_issue": "mcp__jira__get_issue",
    "jira.getIssue": "mcp__jira__get_issue",
    "mcp__jira__get_issue": "mcp__jira__get_issue",
    "file.read": "Read",
    "web.fetch": "WebFetch",
}


def resolve_intent(
    *,
    user_input: str,
    pre_fetched_context: dict | None = None,
    translation: dict | None = None,
    source_tree_summary: str | None = None,
    allowed_tools: list[str] | None = None,
    max_tool_calls: int = 3,
    timeout_seconds: float = 90.0,
    claude_model: str | None = None,
) -> ResolvedIntent:
    """Run the intent resolution agent via Claude Code CLI with MCP tools.

    The orchestrator owns fallback behavior. This function raises
    ``MCPNotConfiguredError`` when MCP/Claude Code is unavailable so callers
    can fall back to V1 request refinement.
    """

    started = time.monotonic()
    settings = get_settings()
    normalized_tools = _normalize_allowed_tools(allowed_tools)

    if not bool(getattr(settings, "mcp_jira_enabled", False)):
        raise MCPNotConfiguredError(
            "Jira MCP is disabled. Set OPS_AGENT_MCP_JIRA_ENABLED=true to use V2 intent resolution."
        )
    if not any(tool.startswith("mcp__jira__") for tool in normalized_tools):
        raise MCPNotConfiguredError("No Jira MCP tool was allowed for intent resolution.")

    mcp_config_path = _find_jira_mcp_config(settings=settings, allowed_tools=normalized_tools)
    if mcp_config_path is None and not str(getattr(settings, "mcp_jira_server_url", "") or "").strip():
        raise MCPNotConfiguredError(
            "Jira MCP is not configured. Set OPS_AGENT_MCP_JIRA_ENABLED=true "
            "and configure a Jira MCP server."
        )

    claude_command = str(getattr(settings, "claude_code_command", "claude") or "claude")
    claude_cmd = shutil.which(claude_command)
    if not claude_cmd:
        raise MCPNotConfiguredError(f"Claude Code CLI not found: {claude_command}")

    prompt = _build_agent_prompt(
        user_input=user_input,
        pre_fetched_context=pre_fetched_context,
        translation=translation,
        source_tree_summary=source_tree_summary,
        allowed_tools=normalized_tools,
        max_tool_calls=max_tool_calls,
    )

    prompt_file_path: str | None = None
    try:
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        )
        prompt_file.write(prompt)
        prompt_file.close()
        prompt_file_path = prompt_file.name

        cmd = _build_claude_command(
            claude_cmd=claude_cmd,
            claude_args=str(getattr(settings, "claude_code_args", "") or ""),
            allowed_tools=normalized_tools,
            claude_model=claude_model,
        )
        cwd = str(mcp_config_path.parent if mcp_config_path is not None else Path.cwd())
        env = _build_claude_env(settings)

        _logger.info("Intent resolution CLI cmd: %s (cwd=%s)", cmd, cwd)
        with open(prompt_file_path, "r", encoding="utf-8") as stdin_f:
            proc = subprocess.Popen(
                cmd,
                stdin=stdin_f,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_tree(proc)
                raise IntentResolutionTimeoutError(
                    f"Intent resolution CLI timed out after {timeout_seconds}s"
                ) from exc

        if proc.returncode != 0:
            stderr_text = (stderr or "").strip()[:500]
            raise RuntimeError(
                f"Intent resolution CLI failed (rc={proc.returncode}): {stderr_text}"
            )

        refined_text, tool_calls_made, sources_consulted = _parse_agent_output(
            stdout=stdout or "",
            stderr=stderr or "",
            user_input=user_input,
            pre_fetched_context=pre_fetched_context,
            allowed_tools=normalized_tools,
        )
        if not refined_text:
            raise ValueError("Intent resolution CLI returned empty output")

        return ResolvedIntent(
            refined_text=refined_text,
            tool_calls_made=tool_calls_made,
            sources_consulted=sources_consulted,
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        if prompt_file_path:
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass


def _build_agent_prompt(
    *,
    user_input: str,
    pre_fetched_context: dict | None,
    translation: dict | None,
    source_tree_summary: str | None,
    allowed_tools: list[str],
    max_tool_calls: int,
) -> str:
    max_tool_calls = max(1, int(max_tool_calls))
    sections: list[str] = [
        "SYSTEM INSTRUCTIONS:",
        _AGENT_SYSTEM_PROMPT.format(max_tool_calls=max_tool_calls),
        "",
        "AVAILABLE TOOLS:",
        "\n".join(f"- {tool}" for tool in allowed_tools) if allowed_tools else "- none",
        "",
        "USER INPUT:",
        user_input.strip(),
    ]

    if pre_fetched_context:
        sections.extend(
            [
                "",
                "INITIAL CONTEXT (already fetched; verify with tools if needed):",
                _json_dumps(pre_fetched_context),
            ]
        )

    if translation:
        sections.extend(["", "SEMANTIC TRANSLATION:", _json_dumps(translation)])

    if source_tree_summary:
        sections.extend(["", "SOURCE TREE SUMMARY:", source_tree_summary.strip()])

    sections.extend(
        [
            "",
            "FINAL OUTPUT:",
            "Return only the refined request text.",
        ]
    )
    return "\n".join(sections).strip() + "\n"


def _parse_agent_output(
    *,
    stdout: str,
    stderr: str = "",
    user_input: str = "",
    pre_fetched_context: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> tuple[str, int, list[str]]:
    raw = (stdout or "").strip()
    metadata_blob = "\n".join(part for part in (stdout, stderr) if part)
    tool_calls_made: int | None = None
    sources: list[str] = []

    parsed = _parse_json(raw)
    if isinstance(parsed, dict):
        result = parsed.get("result")
        if isinstance(result, str):
            raw = result.strip()
        elif isinstance(parsed.get("text"), str):
            raw = str(parsed["text"]).strip()
        elif isinstance(parsed.get("content"), list):
            raw = "".join(
                str(block.get("text", ""))
                for block in parsed["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()

        explicit_tool_count = parsed.get("tool_calls_made")
        if isinstance(explicit_tool_count, int):
            tool_calls_made = explicit_tool_count

        for key in ("sources_consulted", "sources"):
            value = parsed.get(key)
            if isinstance(value, list):
                sources.extend(str(item) for item in value if str(item).strip())

        for key in ("tool_calls", "tool_uses", "tools"):
            value = parsed.get(key)
            if isinstance(value, list):
                if tool_calls_made is None:
                    tool_calls_made = len(value)
                sources.extend(_sources_from_tool_calls(value))

    if tool_calls_made is None:
        tool_calls_made = _count_tool_mentions(
            metadata_blob=metadata_blob,
            allowed_tools=allowed_tools or [],
        )

    if not sources and tool_calls_made > 0:
        sources.extend(
            _infer_sources(
                user_input=user_input,
                pre_fetched_context=pre_fetched_context,
                metadata_blob=metadata_blob,
            )
        )

    return raw.strip(), tool_calls_made, _dedupe_nonempty(sources)


def _normalize_allowed_tools(allowed_tools: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw_tool in allowed_tools or _DEFAULT_ALLOWED_TOOLS:
        tool = _TOOL_ALIASES.get(str(raw_tool).strip(), str(raw_tool).strip())
        if tool and tool not in normalized:
            normalized.append(tool)
    if not normalized:
        raise MCPNotConfiguredError("No intent-resolution tools were allowed.")
    return normalized


def _find_jira_mcp_config(*, settings: Any, allowed_tools: list[str]) -> Path | None:
    if not bool(getattr(settings, "mcp_jira_enabled", False)):
        return None
    if not any(tool.startswith("mcp__jira__") for tool in allowed_tools):
        return None
    for path in _candidate_mcp_config_paths():
        if _mcp_config_has_jira(path):
            return path
    return None


def _candidate_mcp_config_paths() -> list[Path]:
    candidates: list[Path] = []
    starts = [Path.cwd(), BASE_DIR, BASE_DIR.parent, BASE_DIR.parents[1], Path.home()]
    seen: set[Path] = set()
    for start in starts:
        try:
            resolved = start.resolve()
        except OSError:
            resolved = start
        for parent in (resolved, *resolved.parents):
            candidate = parent / ".mcp.json"
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
    return candidates


def _mcp_config_has_jira(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    servers = data.get("mcpServers") or data.get("servers") or {}
    if isinstance(servers, dict):
        return any("jira" in str(name).lower() for name in servers)
    if isinstance(servers, list):
        return any("jira" in str(server).lower() for server in servers)
    return False


def _build_claude_command(
    *,
    claude_cmd: str,
    claude_args: str,
    allowed_tools: list[str],
    claude_model: str | None,
) -> list[str]:
    args = claude_args.split()
    if "-p" not in args and "--print" not in args:
        args.append("-p")
    if "--allowedTools" not in args:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    if claude_model and "--model" not in args:
        args.extend(["--model", claude_model])
    return [claude_cmd, *args, "-"]


def _build_claude_env(settings: Any) -> dict[str, str]:
    env = {**os.environ}
    env.pop("ANTHROPIC_API_KEY", None)
    if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
        configured = getattr(settings, "claude_code_git_bash_path", None)
        candidates = [
            str(configured) if configured else "",
            r"D:\Git\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                break
    return env


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _parse_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _count_tool_mentions(*, metadata_blob: str, allowed_tools: list[str]) -> int:
    if not metadata_blob or not allowed_tools:
        return 0
    return sum(metadata_blob.count(tool) for tool in allowed_tools)


def _sources_from_tool_calls(tool_calls: list[Any]) -> list[str]:
    sources: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        arguments = call.get("input") or call.get("arguments") or call.get("params") or {}
        argument_text = _json_dumps(arguments) if isinstance(arguments, (dict, list)) else str(arguments)
        if "jira" in name.lower():
            for key in _JIRA_KEY_RE.findall(argument_text):
                sources.append(f"jira:{key}")
        if "fetch" in name.lower() or "url" in name.lower():
            for url in _URL_RE.findall(argument_text):
                sources.append(f"url:{url.rstrip('.,')}")
    return sources


def _infer_sources(
    *,
    user_input: str,
    pre_fetched_context: dict | None,
    metadata_blob: str,
) -> list[str]:
    sources: list[str] = []
    text_parts = [user_input, metadata_blob]
    if pre_fetched_context:
        text_parts.append(_json_dumps(pre_fetched_context))
    text = "\n".join(part for part in text_parts if part)
    for key in _JIRA_KEY_RE.findall(text):
        sources.append(f"jira:{key}")
    for url in _URL_RE.findall(text):
        sources.append(f"url:{url.rstrip('.,')}")
    return sources


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result
