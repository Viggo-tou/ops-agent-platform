"""LLM-based semantic reranker for retrieval candidates.

Keyword/BM25-style scoring (the existing KnowledgeService) is a strong
baseline but orders candidates by token overlap, which often puts
almost-relevant files ahead of truly-relevant ones. A cheap LLM pass
can re-order a pool of ~15 candidates to put the truly-relevant files
on top before we slice to top_k.

Fails safe: if MiniMax is unreachable or the response is malformed, the
original keyword ordering is returned unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

_logger = get_logger(component="knowledge_rerank")


@dataclass(frozen=True)
class RerankCandidate:
    candidate_id: int
    relative_path: str
    source_name: str
    snippet: str


def rerank_candidates(
    *,
    query: str,
    candidates: Sequence[RerankCandidate],
    settings: Settings,
) -> list[int]:
    """Ask an LLM to re-order ``candidates`` by relevance to ``query``.

    Returns a list of candidate_id values in the LLM's ranked order.
    Candidates the LLM omits are appended at the end in their original
    order (so we never lose a document — just re-prioritise).

    Returns the original order unchanged on any failure.
    """
    if not candidates:
        return []
    original_ids = [c.candidate_id for c in candidates]
    if len(candidates) == 1:
        return original_ids

    if not settings.minimax_api_key:
        return original_ids
    if not settings.knowledge_rerank_enabled:
        return original_ids

    snippet_cap = int(getattr(settings, "knowledge_rerank_snippet_chars", 600))
    numbered = "\n".join(
        f"[{c.candidate_id}] path: {c.relative_path} | source: {c.source_name} | "
        f"snippet: {(c.snippet or '').strip()[:snippet_cap]}"
        for c in candidates
    )

    system_prompt = (
        "You are a retrieval re-ranker for a repository-grounded Q&A system. "
        "Given a user query and a list of numbered candidate files (each with "
        "path + short content snippet), return the candidate IDs in order of "
        "relevance to the query. Put files that best answer the query first. "
        "Deprioritise: tests, fixtures, generated assets, translations, lock "
        "files, build output, documentation-only files that don't define the "
        "behaviour being asked about. Omit candidates that look clearly "
        "irrelevant to the query; the system will still keep them for the "
        'UI timeline. Output JSON only: {"ranked_ids": [12, 3, 7, ...]}.'
    )
    user_prompt = (
        f"Query:\n{query}\n\n"
        f"Candidates:\n{numbered}\n\n"
        f'Return JSON: {{"ranked_ids": [...]}}. Most relevant first.'
    )

    payload = {
        "model": getattr(settings, "semantic_translator_model", "MiniMax-Text-01"),
        "temperature": 0,
        "messages": [
            {"role": "system", "name": "Knowledge Reranker", "content": system_prompt},
            {"role": "user", "name": "retrieval", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=settings.knowledge_rerank_timeout_seconds) as client:
            response = client.post(
                f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("rerank_llm_call_failed", error=str(exc)[:200])
        return original_ids

    content = _extract_content(response_payload)
    parsed = _parse_ranked_ids(content, valid_ids=set(original_ids))
    if parsed is None:
        _logger.warning("rerank_llm_parse_failed", content_preview=content[:200])
        return original_ids

    # Append any original ids not included by the LLM, preserving original order.
    tail = [i for i in original_ids if i not in set(parsed)]
    return list(parsed) + tail


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


def _parse_ranked_ids(content: str, *, valid_ids: set[int]) -> list[int] | None:
    if not content:
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m is None:
        return None
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    raw = parsed.get("ranked_ids") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return None
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            cid = int(item)
        except (TypeError, ValueError):
            continue
        if cid in valid_ids and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out if out else None
