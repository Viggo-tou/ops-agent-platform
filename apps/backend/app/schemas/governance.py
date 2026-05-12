from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.enums import ActorRole, PolicyDecision, RiskCategory, RiskLevel


class RbacRoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role_key: ActorRole
    display_name: str
    description: str
    is_human: bool
    is_system: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PolicyRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rule_key: str
    title: str
    description: str
    subject_role: ActorRole
    resource_type: str
    action_key: str
    tool_name: str | None = None
    scope_selector: str | None = None
    decision: PolicyDecision
    risk_level: RiskLevel
    risk_category: RiskCategory
    required_approver_role: ActorRole | None = None
    constraints_json: dict[str, Any] | None = None
    metadata_json: dict[str, Any] | None = None
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
