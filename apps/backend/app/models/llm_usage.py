from __future__ import annotations

from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), index=True, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(128), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    purpose: Mapped[str] = mapped_column(String(64), default="unknown")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
