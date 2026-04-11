from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ActorRole, PolicyDecision, RiskCategory, RiskLevel
from app.models.base import Base, utcnow


class PolicyRule(Base):
    __tablename__ = "policy_rule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    rule_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    subject_role: Mapped[ActorRole] = mapped_column(SqlEnum(ActorRole, native_enum=False), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    action_key: Mapped[str] = mapped_column(String(64), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    scope_selector: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    decision: Mapped[PolicyDecision] = mapped_column(SqlEnum(PolicyDecision, native_enum=False), index=True)
    risk_level: Mapped[RiskLevel] = mapped_column(SqlEnum(RiskLevel, native_enum=False), index=True)
    risk_category: Mapped[RiskCategory] = mapped_column(SqlEnum(RiskCategory, native_enum=False), index=True)
    required_approver_role: Mapped[ActorRole | None] = mapped_column(
        SqlEnum(ActorRole, native_enum=False),
        nullable=True,
    )
    constraints_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
