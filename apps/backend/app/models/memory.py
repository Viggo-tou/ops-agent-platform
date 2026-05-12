from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class MemoryItem(Base):
    __tablename__ = "memory_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(64), index=True, default="general")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MemorySettings(Base):
    __tablename__ = "memory_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_list: Mapped[str] = mapped_column(Text, default="")
    block_list: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    observation: Mapped[str] = mapped_column(String(2000), nullable=False)
    resolution: Mapped[str] = mapped_column(String(2000), nullable=False)
    provenance_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("event.id"),
        nullable=True,
    )
    provenance_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task.id"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        nullable=False,
    )
    last_used_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_agent_memory_scope_kind", "scope", "kind"),)
