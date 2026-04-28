from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ActorRole, RiskCategory, RiskLevel, RoleName, TaskStatus, WorkflowStage
from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.approval import Approval
    from app.models.event import Event
    from app.models.tool_execution import ToolExecution


class Task(Base):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(100), default="employee")
    actor_role: Mapped[ActorRole] = mapped_column(
        SqlEnum(ActorRole, native_enum=False),
        default=ActorRole.EMPLOYEE,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    request_text: Mapped[str] = mapped_column(Text)
    scenario: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[TaskStatus] = mapped_column(
        SqlEnum(TaskStatus, native_enum=False),
        default=TaskStatus.QUEUED,
        index=True,
    )
    workflow_stage: Mapped[WorkflowStage] = mapped_column(
        SqlEnum(WorkflowStage, native_enum=False),
        default=WorkflowStage.INTAKE,
        index=True,
    )
    current_role: Mapped[RoleName | None] = mapped_column(SqlEnum(RoleName, native_enum=False), nullable=True)
    risk_level: Mapped[RiskLevel] = mapped_column(
        SqlEnum(RiskLevel, native_enum=False),
        default=RiskLevel.LOW,
        index=True,
    )
    risk_category: Mapped[RiskCategory] = mapped_column(
        SqlEnum(RiskCategory, native_enum=False),
        default=RiskCategory.GENERAL,
        index=True,
    )
    pending_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    governance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    translation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # JSON result envelope. Failure/approval transitions may include
    # ``failure_diagnosis`` with DiagnosisOutput-compatible fields.
    latest_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list["Event"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Approval.requested_at",
    )
    tool_executions: Mapped[list["ToolExecution"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ToolExecution.started_at",
    )

    def _plan_provider_data(self) -> dict[str, Any]:
        if isinstance(self.plan_json, dict):
            provider = self.plan_json.get("provider")
            if isinstance(provider, dict):
                return provider
        return {}

    @property
    def plan_provider_name(self) -> str | None:
        provider = self._plan_provider_data().get("name")
        return provider if isinstance(provider, str) else None

    @property
    def plan_provider_mode(self) -> str | None:
        mode = self._plan_provider_data().get("mode")
        return mode if isinstance(mode, str) else None

    @property
    def plan_model_name(self) -> str | None:
        model = self._plan_provider_data().get("model")
        return model if isinstance(model, str) else None

    @property
    def plan_used_fallback(self) -> bool:
        mode = self.plan_provider_mode or ""
        return "fallback" in mode

    @property
    def plan_fallback_reason(self) -> str | None:
        reason = self._plan_provider_data().get("error")
        return reason if isinstance(reason, str) else None

    def _review_data(self) -> dict[str, Any]:
        if isinstance(self.review_json, dict):
            return self.review_json
        return {}

    @property
    def review_stage(self) -> str | None:
        stage = self._review_data().get("review_stage")
        return stage if isinstance(stage, str) else None

    @property
    def review_verdict(self) -> str | None:
        verdict = self._review_data().get("verdict")
        return verdict if isinstance(verdict, str) else None

    @property
    def review_summary(self) -> str | None:
        summary = self._review_data().get("summary")
        return summary if isinstance(summary, str) else None
