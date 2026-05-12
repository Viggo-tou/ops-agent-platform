"""LLM-driven query token expansion for knowledge retrieval.

The keyword-based retriever scores documents by token overlap with the
user query (plus a tiny static SEMANTIC_EXPANSIONS table). It misses
files where the user's natural-language phrasing doesn't share surface
tokens with the actual identifiers — e.g. "approval workflow" vs
HandymanVerification.js, "access control" vs AdminSettings.js role
checks, "ticket reply pipeline" vs SupportFeedback.js emailjs handler.

This service asks an LLM for ADDITIONAL likely-source tokens to fold
into the retrieval token set. Way cheaper than maintaining a vector
index, addresses the same recall gap. Fails safe: any error returns
empty list and the existing token set is unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

_logger = get_logger(component="query_rewrite")


def expand_query_tokens(
    *,
    query: str,
    settings: Settings,
    existing_tokens: Iterable[str] | None = None,
) -> set[str]:
    """Return a set of additional source-code tokens (lowercase) to add to
    the retrieval token set. The returned set excludes anything already
    in ``existing_tokens`` so callers can simply union it.
    """
    if not settings.minimax_api_key or not getattr(
        settings, "knowledge_query_rewrite_enabled", False
    ):
        return set()
    if not query.strip():
        return set()

    existing_lower = {t.lower() for t in (existing_tokens or [])}

    system_prompt = (
        "You help a code-search system retrieve files relevant to a user "
        "query. Given the query, list 3-10 ADDITIONAL likely tokens that "
        "could appear in source code identifiers, file names, or comments "
        "of the files that answer the query. Include CamelCase variants, "
        "common synonyms, and adjacent concepts the original query did not "
        "spell out. Do NOT repeat tokens already in the original query.\n\n"
        "Examples:\n"
        '  query: "How does the support ticket reply pipeline work?"\n'
        '  -> {"tokens": ["SupportFeedback", "emailjs", "reply", '
        '"sendEmail", "support_requests", "TicketDetail", "Inbox"]}\n\n'
        '  query: "Where is admin role checked?"\n'
        '  -> {"tokens": ["AdminSettings", "MasterAdmin", "currentUser", '
        '"role", "isAdmin", "RequireAuth", "RoleGuard"]}\n\n'
        'Output JSON only: {"tokens": [..]}.'
    )
    user_prompt = (
        f"Query:\n{query}\n\n"
        "Return JSON: {\"tokens\": [...]}.  Each token: a single word, "
        "snake_case identifier, or CamelCase identifier — no spaces, no "
        "punctuation, no quotes around individual tokens."
    )

    payload = {
        "model": getattr(settings, "semantic_translator_model", "MiniMax-Text-01"),
        "temperature": 0,
        "messages": [
            {"role": "system", "name": "Query Token Expander", "content": system_prompt},
            {"role": "user", "name": "retrieval", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(
            timeout=getattr(settings, "knowledge_query_rewrite_timeout_seconds", 15.0)
        ) as client:
            response = client.post(
                f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("query_rewrite_call_failed", error=str(exc)[:200])
        return set()

    tokens = _parse_tokens(_extract_content(response_payload))
    cleaned: set[str] = set()
    for tok in tokens:
        norm = tok.strip().lower()
        if not norm or len(norm) < 2 or len(norm) > 60:
            continue
        if not re.match(r"^[a-z0-9][a-z0-9_]*$", norm):
            # Allow CamelCase originals through as both pieces and the
            # snake_case lower form that the retriever's tokenizer would
            # produce. Strip non-identifier chars.
            norm = re.sub(r"[^a-z0-9_]", "", norm)
            if not norm or len(norm) < 2:
                continue
        if norm in existing_lower:
            continue
        cleaned.add(norm)
        if len(cleaned) >= 12:
            break
    return cleaned


def _extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = [seg.get("text", "") for seg in content if isinstance(seg, dict)]
        return "".join(parts)
    return str(content or "")


def _parse_tokens(content: str) -> list[str]:
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
    raw = parsed.get("tokens") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            # Split CamelCase into both joined and split variants so the
            # downstream tokenizer can match either form.
            spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", item)
            spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
            for part in spaced.split():
                out.append(part)
            out.append(item)
    return out
