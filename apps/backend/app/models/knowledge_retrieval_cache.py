from __future__ import annotations

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class KnowledgeRetrievalCache(Base):
    __tablename__ = "knowledge_retrieval_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA256 hex
    query_hash_inputs: Mapped[str] = mapped_column(String(2000), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    cached_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_hit_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
