from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ApprovalStatus, RiskCategory, RiskLevel, RoleName
from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.task import Task


class Approval(Base):
    __tablename__ = "approval"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), index=True)
    action_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[ApprovalStatus] = mapped_column(
        SqlEnum(ApprovalStatus, native_enum=False),
        default=ApprovalStatus.PENDING,
        index=True,
    )
    requested_by_role: Mapped[RoleName] = mapped_column(SqlEnum(RoleName, native_enum=False))
    approver_role: Mapped[str] = mapped_column(String(64), default="team_lead")
    requested_by_actor_name: Mapped[str] = mapped_column(String(100), default="employee")
    decided_by_actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    risk_level: Mapped[RiskLevel] = mapped_column(
        SqlEnum(RiskLevel, native_enum=False),
        default=RiskLevel.MEDIUM,
        index=True,
    )
    risk_category: Mapped[RiskCategory] = mapped_column(
        SqlEnum(RiskCategory, native_enum=False),
        default=RiskCategory.GENERAL,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text)
    request_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    policy_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requested_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="approvals")
