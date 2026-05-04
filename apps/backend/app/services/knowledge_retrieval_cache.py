from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.knowledge_retrieval_cache import KnowledgeRetrievalCache

logger = logging.getLogger(__name__)


class RetrievalCache:
    """Content-hash-keyed cache layer for knowledge retrieval results.

    Same query on same KB source re-runs full retrieval (RAG + cards
    + CC agentic + synth) every time.  This cache stores the final
    ``(citations, claims, answer_text)`` payload keyed by a SHA-256
    hash of ``(normalised_query || source_name)`` so repeat queries
    within the TTL window skip the entire pipeline.

    Invalidation: call ``invalidate_source(source_name)`` after a
    knowledge sync to prevent stale answers.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, query: str, source_name: str) -> dict | None:
        """Return cached retrieval result dict if fresh, else ``None``.

        The returned dict is the JSON payload originally passed to
        ``put()``.  If the entry is absent or its age exceeds *ttl_seconds*
        the method returns ``None``.
        """
        key = self._compute_key(query, source_name)
        row = self.db.get(KnowledgeRetrievalCache, key)
        if row is None:
            return None

        cached_at = row.cached_at
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age > row.ttl_seconds:
            return None  # stale — let caller re-populate

        # Best-effort hit metadata update (not in a transaction).
        try:
            row.last_hit_at = datetime.now(timezone.utc)
            row.hit_count += 1
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.warning("Failed to update cache hit metadata for key %s", key[:16])

        try:
            return json.loads(row.response_json)
        except json.JSONDecodeError:
            logger.warning("Corrupt cache entry for key %s — purging", key[:16])
            self.db.delete(row)
            self.db.commit()
            return None

    def put(
        self,
        query: str,
        source_name: str,
        response: dict,
        ttl: int | None = None,
    ) -> None:
        """Store a retrieval result in the cache (upsert)."""
        key = self._compute_key(query, source_name)
        ttl_seconds = (
            ttl
            if ttl is not None
            else int(getattr(self.settings, "knowledge_retrieval_cache_ttl_seconds", 3600))
        )
        now = datetime.now(timezone.utc)

        # Enforce max-entries cap: if we're over, delete oldest entry.
        max_entries = int(
            getattr(self.settings, "knowledge_retrieval_cache_max_entries", 1000)
        )
        if max_entries > 0:
            try:
                from sqlalchemy import func
                current_count = self.db.query(
                    func.count(KnowledgeRetrievalCache.cache_key)
                ).scalar()
                if current_count is not None and current_count >= max_entries:
                    oldest = (
                        self.db.query(KnowledgeRetrievalCache)
                        .order_by(KnowledgeRetrievalCache.cached_at.asc())
                        .limit(1)
                        .first()
                    )
                    if oldest is not None and oldest.cache_key != key:
                        self.db.delete(oldest)
            except Exception:
                self.db.rollback()
                logger.warning("Failed to enforce max-entries cap", exc_info=True)

        existing = self.db.get(KnowledgeRetrievalCache, key)
        if existing is not None:
            existing.query_hash_inputs = f"query={query[:500]} | source={source_name}"
            existing.response_json = json.dumps(response, ensure_ascii=False)
            existing.cached_at = now
            existing.last_hit_at = None
            existing.hit_count = 0
            existing.ttl_seconds = ttl_seconds
        else:
            entry = KnowledgeRetrievalCache(
                cache_key=key,
                query_hash_inputs=f"query={query[:500]} | source={source_name}",
                response_json=json.dumps(response, ensure_ascii=False),
                cached_at=now,
                ttl_seconds=ttl_seconds,
            )
            self.db.add(entry)

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.warning("Failed to persist cache entry for key %s", key[:16])

    def invalidate_source(self, source_name: str) -> int:
        """Drop all cache entries for a given knowledge source.

        Returns the number of rows deleted.
        """
        try:
            from sqlalchemy import delete

            stmt = delete(KnowledgeRetrievalCache).where(
                KnowledgeRetrievalCache.query_hash_inputs.contains(
                    f"source={source_name}"
                )
            )
            result = self.db.execute(stmt)
            self.db.commit()
            return result.rowcount
        except Exception:
            self.db.rollback()
            logger.warning(
                "Failed to invalidate cache for source %s", source_name, exc_info=True
            )
            return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_key(self, query: str, source_name: str) -> str:
        """SHA-256 hex digest of normalised (query + source_name).

        Normalisation is case-fold + whitespace-collapse so that
        semantically identical queries hit the same cache slot.
        """
        normalised_query = " ".join(query.strip().lower().split())
        normalised_source = source_name.strip().lower()
        raw = f"{normalised_query}||{normalised_source}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
