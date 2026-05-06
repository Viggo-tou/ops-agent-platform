from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ActorRole, RiskCategory, RiskLevel, RoleName, TaskStatus, WorkflowStage
from app.schemas.approval import ApprovalRead


class TaskCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    request: str = Field(min_length=3, max_length=4000)
    actor_name: str = Field(default="employee", min_length=1, max_length=100)
    actor_role: ActorRole = ActorRole.EMPLOYEE
    session_id: str | None = Field(default=None, min_length=1, max_length=36)
    previous_task_id: str | None = Field(default=None, min_length=1, max_length=64)


class TaskRollbackRequest(BaseModel):
    actor_name: str = Field(default="operator", min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=1000)


class TaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str | None = None
    actor_name: str
    actor_role: ActorRole
    title: str
    scenario: str
    status: TaskStatus
    workflow_stage: WorkflowStage
    current_role: RoleName | None = None
    risk_level: RiskLevel
    risk_category: RiskCategory
    pending_approval: bool
    retry_count: int
    plan_provider_name: str | None = None
    plan_provider_mode: str | None = None
    plan_model_name: str | None = None
    plan_used_fallback: bool = False
    plan_fallback_reason: str | None = None
    review_stage: str | None = None
    review_verdict: str | None = None
    review_summary: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskSummary):
    request_text: str
    governance_json: dict[str, Any] | None = None
    translation_json: dict[str, Any] | None = None
    plan_json: dict[str, Any] | None = None
    review_json: dict[str, Any] | None = None
    latest_result_json: dict[str, Any] | None = None
    approvals: list[ApprovalRead] = Field(default_factory=list)
