"""ReAct-style two-turn wrapper for DeepSeek codegen.

Turn 1 plans (lists symbols + their source files), harness verifies,
Turn 2 emits the diff using only verified symbols.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SymbolPlan:
    """Output of Turn 1: structured plan listing symbols the diff will reference."""

    referenced_symbols: list[dict[str, str]]
    files_to_modify: list[str]
    rationale: str

    @classmethod
    def parse(cls, text: str) -> "SymbolPlan | None":
        """Parse Turn-1 LLM output expected to be a JSON object."""
        try:
            stripped = _strip_json_fences(text)
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        syms = data.get("referenced_symbols") or []
        files = data.get("files_to_modify") or []
        if not isinstance(syms, list) or not isinstance(files, list):
            return None

        return cls(
            referenced_symbols=[
                s for s in syms
                if isinstance(s, dict) and "name" in s
            ],
            files_to_modify=[f for f in files if isinstance(f, str)],
            rationale=str(data.get("rationale", ""))[:500],
        )


@dataclass(frozen=True)
class SymbolVerification:
    """Result of Turn-1.5 verification step."""

    verified: list[dict[str, str]]
    hallucinated: list[dict[str, str]]
    misattributed: list[dict[str, str]]


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    if not s.startswith("{"):
        match = re.search(r"\{", s)
        if match:
            s = s[match.start():]
    return s


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def verify_symbol_plan(
    plan: SymbolPlan,
    context_files: dict[str, str],
) -> SymbolVerification:
    """Grep each ``(name, source_file)`` pair against context file contents."""
    verified: list[dict[str, str]] = []
    hallucinated: list[dict[str, str]] = []
    misattributed: list[dict[str, str]] = []

    by_basename: dict[str, list[tuple[str, str]]] = {}
    for path, content in context_files.items():
        by_basename.setdefault(_basename(path), []).append((path, content))

    for sym in plan.referenced_symbols:
        name = str(sym.get("name", "")).strip()
        declared_file = str(sym.get("source_file", "")).strip()
        kind = str(sym.get("kind", "")).strip().lower()
        if not name:
            continue
        if kind == "new":
            verified.append(sym)
            continue
        if len(name) < 3:
            verified.append(sym)
            continue

        token_re = re.compile(r"\b" + re.escape(name) + r"\b")
        target_content = context_files.get(declared_file)
        if target_content is None and declared_file:
            for _, content in by_basename.get(_basename(declared_file), []):
                target_content = content
                break

        if target_content is None:
            found_in = [
                path for path, content in context_files.items()
                if token_re.search(content)
            ]
            if found_in:
                misattributed.append({**sym, "actually_in": ", ".join(found_in[:3])})
            else:
                hallucinated.append(sym)
            continue

        if token_re.search(target_content):
            verified.append(sym)
            continue

        found_in = [
            path for path, content in context_files.items()
            if path != declared_file and token_re.search(content)
        ]
        if found_in:
            misattributed.append({**sym, "actually_in": ", ".join(found_in[:3])})
        else:
            hallucinated.append(sym)

    return SymbolVerification(
        verified=verified,
        hallucinated=hallucinated,
        misattributed=misattributed,
    )


def build_turn1_prompt(task_description: str, plan_json: dict[str, Any]) -> str:
    """Build the Turn-1 plan-only prompt."""
    plan_context = ""
    if plan_json:
        plan_context = (
            "\n\nPLAN JSON CONTEXT:\n"
            f"{json.dumps(plan_json, ensure_ascii=False, sort_keys=True)}\n"
        )
    return (
        "BEFORE you produce any diff, FIRST list the symbols (fields, "
        "methods, classes, imports) you'll reference and which source "
        "file declares each one.\n\n"
        "Output ONLY a JSON object - no diff, no markdown fences, no prose:\n"
        "{\n"
        '  "referenced_symbols": [\n'
        '    {"name": "<symbol>", "source_file": "<relative path>", "kind": "field|method|class|import|new"},\n'
        "    ...\n"
        "  ],\n"
        '  "files_to_modify": ["<relative path>", ...],\n'
        '  "rationale": "<one-sentence summary of the change>"\n'
        "}\n\n"
        "Rules:\n"
        "- Every symbol you list MUST already exist in the provided source files OR be a "
        'symbol you\'ll add yourself (mark `kind: "new"` for those).\n'
        "- Be honest: if you don't know which file declares a symbol, omit it. The harness will "
        "reject any existing symbol that doesn't grep in the named file.\n"
        "- Keep the list tight - only what your diff will actually touch.\n"
        f"{plan_context}\n"
        f"TASK:\n{task_description}\n"
    )


def build_turn2_prompt(
    task_description: str,
    plan: SymbolPlan,
    verification: SymbolVerification,
) -> str:
    """Build the Turn-2 diff prompt that incorporates verification feedback."""
    del plan
    verified_list = ", ".join(
        f"{s.get('name', '?')} (in {s.get('source_file', '?')})"
        for s in verification.verified
    ) or "(none)"
    parts = [
        "Now produce the unified diff.\n",
        f"Verified symbols you may use: {verified_list}.\n",
    ]
    if verification.hallucinated:
        names = ", ".join(s.get("name", "?") for s in verification.hallucinated)
        parts.append(
            f"REJECTED (do NOT reference these - they are not in any source file): {names}.\n"
        )
    if verification.misattributed:
        for sym in verification.misattributed:
            parts.append(
                f"Note: '{sym.get('name', '?')}' is NOT in {sym.get('source_file', '?')} - "
                f"it's actually in {sym.get('actually_in', '?')}. Reference it from the correct file.\n"
            )
    parts.append("\n")
    parts.append("Output ONLY the unified diff. Same format rules as before.\n\n")
    parts.append(f"TASK (recap):\n{task_description}\n")
    return "".join(parts)


def react_codegen_call(
    *,
    task_description: str,
    plan_json: dict[str, Any],
    context_files: dict[str, str],
    once_call: Callable[[str], str],
    max_attempts: int = 2,
) -> str:
    """Run the two-turn wrapper and return the augmented final codegen prompt.

    ``once_call`` executes the Turn-1 plan call and returns raw response text.
    If Turn 1 cannot produce a parsable plan, this function falls back to the
    original task description so DeepSeek codegen degrades gracefully.
    """
    plan: SymbolPlan | None = None
    for attempt in range(max_attempts):
        turn1_prompt = build_turn1_prompt(task_description, plan_json)
        try:
            text = once_call(turn1_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "react_codegen.turn1_call_failed",
                extra={"attempt": attempt, "error": str(exc)[:200]},
            )
            continue
        plan = SymbolPlan.parse(text)
        if plan is not None:
            break
        logger.info(
            "react_codegen.turn1_unparsable",
            extra={"attempt": attempt, "text_head": text[:200]},
        )

    if plan is None:
        logger.info("react_codegen.fallback_no_plan")
        return task_description

    verification = verify_symbol_plan(plan, context_files)
    logger.info(
        "react_codegen.verified",
        extra={
            "verified": len(verification.verified),
            "hallucinated": len(verification.hallucinated),
            "misattributed": len(verification.misattributed),
        },
    )

    if (
        len(verification.hallucinated) > len(verification.verified)
        and len(plan.referenced_symbols) >= 3
    ):
        logger.info("react_codegen.too_many_hallucinations_fallback")
        return task_description

    return build_turn2_prompt(task_description, plan, verification)
