from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ActorRole, ApprovalStatus, RiskCategory, RiskLevel, RoleName


class ApprovalDecisionRequest(BaseModel):
    actor_name: str = Field(default="team_lead", min_length=1, max_length=100)
    actor_role: ActorRole = ActorRole.TEAM_LEAD
    notes: str | None = Field(default=None, max_length=1000)


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    action_name: str
    status: ApprovalStatus
    requested_by_role: RoleName
    approver_role: ActorRole
    requested_by_actor_name: str
    decided_by_actor_name: str | None = None
    risk_level: RiskLevel
    risk_category: RiskCategory
    reason: str
    request_payload_json: dict[str, Any] | None = None
    policy_snapshot_json: dict[str, Any] | None = None
    decision_payload_json: dict[str, Any] | None = None
    requested_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
