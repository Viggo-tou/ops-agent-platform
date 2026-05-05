"""R-B: Dense embedding retrieval via model2vec.

Uses ``minishlab/potion-base-8M`` — a static, distilled, 8M-param,
256-dim sentence encoder. Pure NumPy at inference time (no torch),
so it runs anywhere Python runs and the per-query latency is in the
single-digit milliseconds.

Embeddings for all KB documents are computed once on first use and
cached on the strategy instance. The cache key includes the model
name + the document content hashes so a re-ingest invalidates the
cache automatically.

Path matching: returns ``relative_path`` strings so the metric layer
can use its suffix-tolerant matcher unchanged.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.rag_bench.dataset import BenchmarkQuestion


logger = logging.getLogger(__name__)


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class DenseEmbeddingStrategy:
    def __init__(
        self,
        db: Session,
        *,
        model_name: str = "minishlab/potion-base-8M",
        max_doc_chars: int = 8000,
    ) -> None:
        self._db = db
        self._model_name = model_name
        self._max_doc_chars = max_doc_chars
        self._model = None  # lazy
        # source_name -> (paths_tuple, embedding_matrix, content_hash)
        self._cache: dict[str, tuple[tuple[str, ...], np.ndarray, str]] = {}

    @property
    def name(self) -> str:
        return f"dense_{self._model_name.split('/')[-1]}"

    def _get_model(self):
        if self._model is None:
            from model2vec import StaticModel  # type: ignore
            logger.info("loading dense model: %s", self._model_name)
            self._model = StaticModel.from_pretrained(self._model_name)
        return self._model

    def _build_index(
        self, source_name: str,
    ) -> tuple[tuple[str, ...], np.ndarray]:
        rows = self._db.execute(
            text(
                "SELECT relative_path, content "
                "FROM knowledge_document "
                "WHERE source_name = :sn "
                "ORDER BY relative_path"
            ),
            {"sn": source_name},
        ).all()
        paths: list[str] = []
        texts: list[str] = []
        for r in rows:
            path = str(r[0]).replace("\\", "/")
            content = (r[1] or "")[: self._max_doc_chars]
            if not content.strip():
                continue
            paths.append(path)
            # Prepend the file path so the encoder sees the filename
            # signal — boosts recall for "where is X file" questions.
            texts.append(f"{path}\n{content}")
        if not texts:
            return (), np.zeros((0, 256), dtype=np.float32)
        model = self._get_model()
        embs = model.encode(texts)
        embs = embs.astype(np.float32, copy=False)
        embs = _l2_normalize(embs)
        return tuple(paths), embs

    def _index_for_source(
        self, source_name: str,
    ) -> tuple[tuple[str, ...], np.ndarray]:
        # Use a content-hash key so re-ingestion auto-invalidates cache.
        rows = self._db.execute(
            text(
                "SELECT content_hash FROM knowledge_document "
                "WHERE source_name = :sn ORDER BY relative_path"
            ),
            {"sn": source_name},
        ).all()
        sig = hashlib.sha256(
            ("|".join(str(r[0] or "") for r in rows)).encode("utf-8")
        ).hexdigest()[:16]
        cached = self._cache.get(source_name)
        if cached and cached[2] == sig:
            return cached[0], cached[1]
        paths, embs = self._build_index(source_name)
        self._cache[source_name] = (paths, embs, sig)
        return paths, embs

    def retrieve(
        self, *, question: BenchmarkQuestion, top_k: int = 10,
    ) -> tuple[str, ...]:
        paths, doc_embs = self._index_for_source(question.source_name)
        if not paths:
            return ()
        model = self._get_model()
        q_emb = model.encode([question.question]).astype(np.float32, copy=False)
        q_emb = _l2_normalize(q_emb)[0]  # (256,)
        scores = doc_embs @ q_emb  # cosine since L2-normed (n,)
        order = np.argsort(-scores)[:top_k]
        return tuple(paths[i] for i in order)
