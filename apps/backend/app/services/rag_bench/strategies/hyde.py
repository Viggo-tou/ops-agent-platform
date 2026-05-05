"""R-E: HyDE — Hypothetical Document Embeddings.

Asks an LLM to draft a *hypothetical answer* to the question, then
retrieves using the hypothetical answer as the search query rather
than the raw question.

Why it helps: many questions like "where is X implemented?" have very
different vocabulary from the source code that answers them. A
hypothetical answer ("X is implemented in a Kotlin Composable named
XScreen at app/src/main/...") shares much more vocabulary with the
ground-truth file than the question does.

Reference: Gao et al. 2022 (Precise Zero-Shot Dense Retrieval without
Relevance Labels).

Wraps an existing strategy (typically FTS5 or dense_embedding); the
LLM call is made once per question, the hypothetical text is then
fed to the inner strategy as the new query.

LLM provider is parameterized; default uses the deepseek path that
already powers semantic_review.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Callable

import httpx

from app.services.rag_bench.dataset import BenchmarkQuestion
from app.services.rag_bench.strategies.base import RetrievalStrategy


logger = logging.getLogger(__name__)


_HYDE_SYSTEM_PROMPT = (
    "You write a short, plausible code-aware ANSWER to the question — "
    "as if you knew the project intimately. The answer will be used as a "
    "search query against the project source code, so use realistic "
    "filenames, class names, and method names that a real codebase of "
    "this kind would use. Output 1-3 sentences, no preamble."
)


def _call_deepseek_text(
    prompt: str, *, settings, timeout: float = 30.0,
) -> str:
    if not getattr(settings, "deepseek_api_key", None):
        raise RuntimeError("deepseek_api_key is not configured")
    base = getattr(settings, "deepseek_base_url",
                   "https://api.deepseek.com/anthropic")
    body = {
        "model": getattr(settings, "deepseek_model", "deepseek-v4-pro"),
        "max_tokens": 256,
        "system": _HYDE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = httpx.post(
        f"{base.rstrip('/')}/v1/messages",
        json=body,
        headers={
            "x-api-key": settings.deepseek_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()


class HydeStrategy:
    def __init__(
        self,
        inner: RetrievalStrategy,
        *,
        llm_caller: Callable[[str], str] | None = None,
        settings=None,
        cache: dict[str, str] | None = None,
    ) -> None:
        """``inner`` is the underlying retriever the HyDE-rewritten query
        is fed to. ``llm_caller`` is an injectable stub for tests; in
        production, ``settings`` is used to dispatch to DeepSeek."""
        self._inner = inner
        self._llm_caller = llm_caller
        self._settings = settings
        # Question-id -> hypothetical answer; avoids re-calling the LLM
        # when the same question appears in repeated benchmark runs.
        self._cache: dict[str, str] = cache if cache is not None else {}

    @property
    def name(self) -> str:
        return f"hyde({self._inner.name})"

    def _hypothesize(self, question: BenchmarkQuestion) -> str:
        cached = self._cache.get(question.id)
        if cached is not None:
            return cached
        prompt = (
            f"Question: {question.question}\n\n"
            f"Source codebase context: this is a project named "
            f"'{question.source_name}' (likely an Android Kotlin app or "
            f"a React/JS dashboard, depending on source name)."
        )
        if self._llm_caller is not None:
            text = self._llm_caller(prompt)
        else:
            try:
                text = _call_deepseek_text(prompt, settings=self._settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HyDE LLM call failed for %s: %s — falling back to "
                    "raw question", question.id, exc,
                )
                text = question.question
        text = (text or "").strip()
        if not text:
            text = question.question
        self._cache[question.id] = text
        return text

    def retrieve(
        self, *, question: BenchmarkQuestion, top_k: int = 10,
    ) -> tuple[str, ...]:
        hypo = self._hypothesize(question)
        # Wrap the question with the hypothetical answer so the inner
        # strategy still sees the actual identifier-shaped query terms,
        # but augmented with code-shaped vocabulary.
        augmented = f"{question.question}\n{hypo}"
        rewritten = replace(question, question=augmented)
        return self._inner.retrieve(question=rewritten, top_k=top_k)
