from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SqlEnum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ToolExecutionStatus, ToolPermissionCategory
from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.task import Task


class ToolExecution(Base):
    __tablename__ = "tool_execution"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    permission_category: Mapped[ToolPermissionCategory] = mapped_column(
        SqlEnum(ToolPermissionCategory, native_enum=False),
        index=True,
    )
    status: Mapped[ToolExecutionStatus] = mapped_column(
        SqlEnum(ToolExecutionStatus, native_enum=False),
        default=ToolExecutionStatus.RUNNING,
        index=True,
    )
    actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_log_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="tool_executions")
