from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import EventSource, EventType, RoleName, WorkflowStage
from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.task import Task


class Event(Base):
    __tablename__ = "event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    event_type: Mapped[EventType] = mapped_column(SqlEnum(EventType, native_enum=False), index=True)
    source: Mapped[EventSource] = mapped_column(SqlEnum(EventSource, native_enum=False), index=True)
    stage: Mapped[WorkflowStage | None] = mapped_column(SqlEnum(WorkflowStage, native_enum=False), nullable=True)
    role: Mapped[RoleName | None] = mapped_column(SqlEnum(RoleName, native_enum=False), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    task: Mapped["Task"] = relationship(back_populates="events")
