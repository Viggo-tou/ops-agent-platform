"""Post-review LLM reviewer that surfaces reservations and potential risks
a human approver should know before clicking Approve.

Runs AFTER all gate checks pass and BEFORE parking for approval. Emits a list
of short reviewer notes flagging security trade-offs, missing deploy config,
untested edge cases, and similar pragmatic concerns that gates don't catch.

Each reservation is tagged with a severity so the UI can route it:
  - bug          → auto-fixable; safe to feed back into codegen
  - missing_test → auto-fixable (codegen can add tests)
  - security     → BLOCK, must be reviewed by a human; auto-fix forbidden
  - policy       → BLOCK, business judgement required
  - style        → optional polish; auto-fix allowed but low priority

Fails safe: if MiniMax is not configured or the call fails, returns an empty
list so the pipeline is not blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

_logger = get_logger(component="reservations")


Severity = Literal["bug", "missing_test", "security", "policy", "style"]
_AUTO_FIXABLE_SEVERITIES: frozenset[Severity] = frozenset({"bug", "missing_test", "style"})
_BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({"security", "policy"})


@dataclass(frozen=True)
class ReservationItem:
    text: str
    severity: Severity

    @property
    def auto_fixable(self) -> bool:
        # bug / missing_test / style — safe for codegen to amend
        return self.severity in _AUTO_FIXABLE_SEVERITIES

    @property
    def blocking(self) -> bool:
        # security / policy — must be human-decided; do NOT auto-amend
        return self.severity in _BLOCKING_SEVERITIES

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "text": self.text,
            "severity": self.severity,
            "auto_fixable": self.auto_fixable,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ReservationsReport:
    items: list[ReservationItem] = field(default_factory=list)
    provider: str = "skipped"
    model: str = "none"
    raw_response: str = ""

    # Backwards-compatible accessor: existing callers that read
    # `report.reservations` get a flat list of text strings.
    @property
    def reservations(self) -> list[str]:
        return [item.text for item in self.items]

    @property
    def auto_fixable(self) -> list[ReservationItem]:
        return [i for i in self.items if i.auto_fixable]

    @property
    def blocking(self) -> list[ReservationItem]:
        return [i for i in self.items if i.blocking]

    def to_dicts(self) -> list[dict[str, str | bool]]:
        return [item.to_dict() for item in self.items]


def build_reservations(
    *,
    task_request: str,
    change_summary: str,
    diff: str,
    plan_objective: str = "",
    settings: Settings | None = None,
) -> ReservationsReport:
    """Ask a reviewer LLM to list reservations and potential risks for this diff."""
    settings = settings or get_settings()
    if not settings.minimax_api_key:
        return ReservationsReport(
            items=[],
            provider="skipped",
            model="none",
            raw_response="OPS_AGENT_MINIMAX_API_KEY not configured; reservations skipped.",
        )

    # Diff may be huge; cap at ~20KB to keep the prompt manageable and costs low.
    trimmed_diff = diff[:20_000]
    if len(diff) > 20_000:
        trimmed_diff += "\n\n[... diff truncated for reviewer prompt ...]"

    system_prompt = (
        "You are a senior engineering reviewer looking at a patch moments before "
        "it ships. List RESERVATIONS — risks, trade-offs, and gotchas a human "
        "approver should know. Be concrete and practical.\n\n"
        "Tag EVERY item with a `severity`. The downstream system will route\n"
        "auto-fixable severities back to codegen for an automated amend round, \n"
        "and surface blocking ones to the human approver. Tag accurately:\n\n"
        "  - 'bug'           → real defect: scope error, null deref, wrong\n"
        "                       branch, dangling reference, type mismatch.\n"
        "                       SAFE to auto-fix.\n"
        "  - 'missing_test'  → new code path lacks coverage. SAFE to auto-fix\n"
        "                       (codegen can add a unit test).\n"
        "  - 'security'      → auth / data-exposure / takeover / privilege\n"
        "                       trade-off. Tag this and STOP — human MUST\n"
        "                       decide; do NOT mark security as bug.\n"
        "  - 'policy'        → business / product judgement: does the change\n"
        "                       match the user's intent, is the rollback plan\n"
        "                       acceptable, is a manual ops step ok. Human\n"
        "                       MUST decide.\n"
        "  - 'style'         → cosmetic / naming / comment-only. Optional.\n\n"
        "What to flag (any severity):\n"
        "- Security/auth choices (severity=security)\n"
        "- Missing deploy/config artifacts (severity=bug if literally missing,\n"
        "  policy if it's an ops decision)\n"
        "- Unhandled edge cases (severity=bug)\n"
        "- Tests not added for new code paths (severity=missing_test)\n"
        "- Naming/API surface that may surprise callers (severity=style or bug)\n"
        "- Comment-only / placebo diffs that don't advance the task\n"
        "  (severity=bug — they should be removed)\n"
        "- Operational gotchas requiring manual steps (severity=policy)\n\n"
        "What NOT to flag: style preferences the team accepts; things the diff\n"
        "handles correctly; praise.\n\n"
        "Each item: 1 short sentence (<200 chars), concrete and actionable.\n"
        "Return JSON ONLY:\n"
        '{"reservations": [\n'
        '   {"text": "...", "severity": "bug"},\n'
        '   {"text": "...", "severity": "security"},\n'
        '   ...\n'
        "]}\n\n"
        "If no reservations, return: {\"reservations\": []}"
    )

    user_prompt = (
        f"TASK REQUEST:\n{task_request[:2000]}\n\n"
        f"PLANNER OBJECTIVE:\n{plan_objective[:1000] or '(not provided)'}\n\n"
        f"PLAN CHANGE SUMMARY:\n{change_summary[:1500] or '(not provided)'}\n\n"
        f"DIFF (the actual patch to review):\n{trimmed_diff}\n\n"
        'Return JSON: {"reservations": ["concern one", "concern two"]}'
    )

    payload = {
        "model": settings.semantic_translator_model,
        "messages": [
            {"role": "system", "name": "Ops Agent Reservations Reviewer", "content": system_prompt},
            {"role": "user", "name": "pipeline", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=settings.semantic_translator_timeout_seconds) as client:
            response = client.post(
                f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("reservations_llm_call_failed", error=str(exc)[:300])
        return ReservationsReport(
            items=[],
            provider="minimax",
            model=settings.semantic_translator_model,
            raw_response=f"error: {str(exc)[:300]}",
        )

    content = _extract_content(response_payload)
    items = _parse_reservations(content)
    return ReservationsReport(
        items=items,
        provider="minimax",
        model=settings.semantic_translator_model,
        raw_response=content[:2000],
    )


def _extract_content(response_payload: dict) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some MiniMax responses return a list of segments
        parts = [seg.get("text", "") for seg in content if isinstance(seg, dict)]
        return "".join(parts)
    return str(content or "")


_VALID_SEVERITIES: tuple[Severity, ...] = ("bug", "missing_test", "security", "policy", "style")


def _normalize_severity(raw: object) -> Severity:
    """Coerce free-form severity strings the model returns into our enum.
    Default to 'bug' when ambiguous so the item is at least surfaced.
    """
    if not isinstance(raw, str):
        return "bug"
    s = raw.lower().strip()
    if s in _VALID_SEVERITIES:
        return s  # type: ignore[return-value]
    # Common synonyms the model uses.
    if "security" in s or "auth" in s or "vulner" in s or "takeover" in s:
        return "security"
    if "policy" in s or "ops" in s or "judgement" in s or "judgment" in s:
        return "policy"
    if "test" in s or "coverage" in s:
        return "missing_test"
    if "style" in s or "cosmetic" in s or "naming" in s:
        return "style"
    return "bug"


def _heuristic_severity_from_text(text: str) -> Severity:
    """When the model returned plain strings (legacy format), infer severity."""
    t = text.lower()
    if any(k in t for k in ("takeover", "spoof", "auth", "security", "credential", "leak", "expose", "vulner")):
        return "security"
    if any(k in t for k in ("test", "coverage", "regression test")):
        return "missing_test"
    if any(k in t for k in ("policy", "rollback", "manual step", "ops", "deployment plan")):
        return "policy"
    if any(k in t for k in ("naming", "style", "cosmetic", "comment-only", "comment only")):
        return "style"
    return "bug"


def _parse_reservations(content: str) -> list[ReservationItem]:
    """Parse the LLM content into ReservationItems.

    Accepts both the new tagged shape:
      {"reservations": [{"text": "...", "severity": "bug"}, ...]}
    and the legacy plain-string shape:
      {"reservations": ["...", "..."]}
    where severity is inferred heuristically from the text.

    Tolerates markdown code fences and leading/trailing prose.
    """
    if not content:
        return []
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m is None:
        return []
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    raw_list = parsed.get("reservations") if isinstance(parsed, dict) else None
    if not isinstance(raw_list, list):
        return []

    out: list[ReservationItem] = []
    seen: set[str] = set()
    for item in raw_list:
        if isinstance(item, str):
            s = item.strip()
            if not s:
                continue
            severity = _heuristic_severity_from_text(s)
        elif isinstance(item, dict):
            s = str(item.get("text") or "").strip()
            if not s:
                continue
            severity = _normalize_severity(item.get("severity"))
        else:
            continue

        if len(s) > 400:
            s = s[:397] + "..."
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ReservationItem(text=s, severity=severity))
        if len(out) >= 15:
            break  # cap; UI shouldn't show a wall of text
    return out
