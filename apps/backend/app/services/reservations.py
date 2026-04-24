"""Post-review LLM reviewer that surfaces reservations and potential risks
a human approver should know before clicking Approve.

Runs AFTER all gate checks pass and BEFORE parking for approval. Emits a list
of short reviewer notes flagging security trade-offs, missing deploy config,
untested edge cases, and similar pragmatic concerns that gates don't catch.

Fails safe: if MiniMax is not configured or the call fails, returns an empty
list so the pipeline is not blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

_logger = get_logger(component="reservations")


@dataclass(frozen=True)
class ReservationsReport:
    reservations: list[str]
    provider: str
    model: str
    raw_response: str


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
            reservations=[],
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
        "it ships. Your job is to list RESERVATIONS — risks, trade-offs, and "
        "gotchas a human approver should know. Be concrete and practical.\n\n"
        "What to flag:\n"
        "- Security/auth choices that could be tightened (e.g., anonymous auth "
        "where admin-only would be stricter)\n"
        "- Missing deploy/config artifacts (e.g., firebase.json not updated, "
        "migration script missing, env var undocumented)\n"
        "- Edge cases the diff doesn't handle (null values, race conditions, "
        "empty collections, error states)\n"
        "- Tests not added for new code paths\n"
        "- Naming/API surface choices that may surprise callers\n"
        "- Changes that are cosmetic (comment-only) and don't advance the task\n"
        "- Operational gotchas (e.g., requires a manual step, breaks existing "
        "users until they re-login, affects rollback)\n\n"
        "What NOT to flag:\n"
        "- Style preferences\n"
        "- Things the diff handles correctly\n"
        "- Praise or neutral observations\n\n"
        "Each item: 1 short sentence (under 200 chars), concrete and actionable.\n"
        "If the diff has no meaningful reservations, return an empty list.\n"
        'Output JSON ONLY in this shape: {"reservations": ["...", "..."]}.'
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
            reservations=[],
            provider="minimax",
            model=settings.semantic_translator_model,
            raw_response=f"error: {str(exc)[:300]}",
        )

    content = _extract_content(response_payload)
    reservations = _parse_reservations(content)
    return ReservationsReport(
        reservations=reservations,
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


def _parse_reservations(content: str) -> list[str]:
    """Parse the LLM content into a clean list of reservation strings.

    Accepts the happy-path JSON, but also tolerates stray markdown code fences
    and leading/trailing prose since reviewer LLMs sometimes add preamble.
    """
    if not content:
        return []
    text = content.strip()
    # Strip common markdown wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    # Find the first JSON object in the text
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
    # Normalize: keep strings, trim, cap length, drop empties/duplicates.
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_list:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        if len(s) > 400:
            s = s[:397] + "..."
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= 15:
            break  # cap to 15 entries; reviewer shouldn't see a wall of text
    return out
