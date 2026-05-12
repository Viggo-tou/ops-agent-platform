from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class ModelProvider(Base):
    __tablename__ = "model_provider"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    models: Mapped[list[ModelEntry]] = relationship(
        "ModelEntry",
        order_by="(ModelEntry.sort_order, ModelEntry.display_name)",
    )


class ModelEntry(Base):
    __tablename__ = "model_entry"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(64), ForeignKey("model_provider.name"), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SelectedModel(Base):
    __tablename__ = "selected_model"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    model_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("model_entry.id"), nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
