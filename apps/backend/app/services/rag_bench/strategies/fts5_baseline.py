"""FTS5 baseline strategy — tokenized boolean retrieval over
``knowledge_document_fts``. Mirrors the production Plan A query shape:

  AND: "(t1 AND t2 AND ...) OR <concat(t1t2...)>"  -- precision-first
  OR (fallback): "t1 OR t2 OR ... OR concat"        -- recall-first

This is the SAME code path the live ``evidence_bundle`` uses; it
serves as the reproducible reference baseline for newer strategies.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.rag_bench.dataset import BenchmarkQuestion


# Mirror of evidence_bundle._FTS5_STOPWORDS to keep the baseline
# consistent with what production retrieval does.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "and", "or", "for", "to", "is",
    "as", "at", "by", "be", "with", "from", "that", "this", "these",
    "those", "it", "its", "but", "not", "no", "so",
    "component", "page", "module", "file", "function", "method", "class",
    "screen", "view", "ui", "flow",
    "shared", "common", "main", "base", "global", "generic", "default",
    "parent", "root", "wrapper", "container",
    # question-form noise
    "what", "where", "which", "how", "does", "do", "is", "are", "was",
    "were", "if", "when",
})


def _tokenize(text_value: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text_value)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", spaced.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def _build_query(tokens: list[str], *, operator: str) -> str:
    if not tokens:
        return ""
    if operator == "AND":
        if len(tokens) == 1:
            return tokens[0]
        joined = "".join(tokens)
        return f"({' AND '.join(tokens)}) OR {joined}"
    joined = "".join(tokens)
    parts = list(tokens)
    if joined not in parts:
        parts.append(joined)
    return " OR ".join(parts)


class FTS5BaselineStrategy:
    """Plan A retrieval — used as the reference baseline."""

    def __init__(self, db: Session, *, top_k_cap: int = 20) -> None:
        self._db = db
        self._top_k_cap = top_k_cap

    @property
    def name(self) -> str:
        return "fts5_baseline"

    def retrieve(
        self, *, question: BenchmarkQuestion, top_k: int = 10,
    ) -> tuple[str, ...]:
        tokens = _tokenize(question.question)
        if not tokens:
            return ()
        cap = min(max(top_k, 1), self._top_k_cap)

        for operator in ("AND", "OR"):
            q = _build_query(tokens, operator=operator)
            if not q:
                continue
            try:
                rows = self._db.execute(
                    text(
                        "SELECT relative_path "
                        "FROM knowledge_document_fts "
                        "WHERE knowledge_document_fts MATCH :q "
                        "  AND source_name = :sn "
                        "ORDER BY rank LIMIT :lim"
                    ),
                    {
                        "q": q,
                        "sn": question.source_name,
                        "lim": int(cap),
                    },
                ).all()
            except Exception:
                continue
            if rows:
                return tuple(
                    str(r[0]).replace("\\", "/")
                    for r in rows if r[0]
                )
        return ()
