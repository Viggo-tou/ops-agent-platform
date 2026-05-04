"""LLM-driven source picker for knowledge retrieval.

`_route_query` in `knowledge.py` selects which knowledge source(s) to
query. The original logic only matches source names literally in the
query string, so a Chinese query like "完成 Jira P69-19" or any natural-
language ticket title will skip routing entirely. FTS5 keyword scoring
then picks whichever source has the most surface-token overlap, which
for cross-cutting feature work routinely picks the wrong repo (e.g.
dashboard for an app feature, because dashboard has more verbose .js
identifiers).

This service asks an LLM to choose among configured sources based on
short per-source descriptions. Fails safe: any error returns empty list
and the caller falls back to the existing literal/scoring behavior.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

_logger = get_logger(component="knowledge_source_router")


def select_sources(
    *,
    query: str,
    source_descriptions: dict[str, str],
    settings: Settings,
) -> list[str]:
    """Return a subset of ``source_descriptions`` keys judged most relevant.

    Args:
        query: User query (can be the original request, the translated
            objective, or a concatenation — caller decides).
        source_descriptions: Mapping of ``source_name`` (lowercase) ->
            short human description (1-2 sentences).
        settings: Backend settings (provides MiniMax key + flag).

    Returns:
        List of source names (subset of input keys). Empty list on
        disabled flag, missing API key, empty query, empty source map,
        LLM error, or unparseable response.
    """
    if not getattr(settings, "knowledge_source_router_enabled", True):
        return []
    if not getattr(settings, "minimax_api_key", None):
        return []
    if not query.strip():
        return []
    if not source_descriptions:
        return []

    available = list(source_descriptions.keys())
    if len(available) == 1:
        # Only one source — no routing needed.
        return list(available)

    catalogue_lines = []
    for name, desc in source_descriptions.items():
        clean_desc = (desc or "").strip() or "(no description configured)"
        catalogue_lines.append(f"- {name}: {clean_desc}")
    catalogue = "\n".join(catalogue_lines)

    system_prompt = (
        "You are a source-selection router for a code-search system. "
        "Given a user query and a catalogue of available knowledge "
        "sources (each with a short description of what code lives "
        "there), return ONLY the source name(s) that most likely "
        "contain code relevant to the query.\n\n"
        "Rules:\n"
        "  - Return one OR more sources. If the query genuinely spans "
        "multiple sources, list all relevant ones.\n"
        "  - If unclear, return all sources (the caller will fall back "
        "to keyword scoring).\n"
        "  - Source names MUST be exact matches from the catalogue.\n"
        "  - Do NOT invent new source names.\n\n"
        'Output JSON only: {"selected": ["name", ...], "reason": "<short>"}.'
    )
    user_prompt = (
        f"Query:\n{query}\n\n"
        f"Available sources:\n{catalogue}\n\n"
        'Return JSON: {"selected": [...], "reason": "..."}.'
    )

    payload = {
        "model": getattr(settings, "semantic_translator_model", "MiniMax-Text-01"),
        "temperature": 0,
        "messages": [
            {"role": "system", "name": "Source Router", "content": system_prompt},
            {"role": "user", "name": "retrieval", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(
            timeout=getattr(settings, "knowledge_source_router_timeout_seconds", 15.0)
        ) as client:
            response = client.post(
                f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("source_router_call_failed", error=str(exc)[:200])
        return []

    selected = _parse_selected(_extract_content(response_payload))
    available_lower = {n.lower() for n in available}
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in selected:
        norm = name.strip().lower()
        if not norm or norm in seen or norm not in available_lower:
            continue
        cleaned.append(norm)
        seen.add(norm)
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


def _parse_selected(content: str) -> list[str]:
    if not content:
        return []
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    raw = obj.get("selected", [])
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, (str, int))]
