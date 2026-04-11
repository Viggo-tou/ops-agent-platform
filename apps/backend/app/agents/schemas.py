from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.enums import RiskLevel, RoleName, ToolPermissionCategory


class PlanTool(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    permission_category: ToolPermissionCategory
    purpose: str = Field(min_length=1, max_length=200)


class PlanStep(BaseModel):
    step_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["analysis", "knowledge", "action", "review"]
    owner_role: RoleName
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    tool_name: str | None = Field(default=None, max_length=120)
    expected_output: str = Field(min_length=1, max_length=240)
    success_criteria: str = Field(min_length=1, max_length=240)


class PlanCodeLocation(BaseModel):
    source_name: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=240)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class FinalOutputContract(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    required_fields: list[str] = Field(min_length=1, max_length=12)


class GeneratedPlanPayload(BaseModel):
    objective: str = Field(min_length=1, max_length=240)
    request_summary: str = Field(min_length=1, max_length=500)
    scenario: str = Field(min_length=1, max_length=64)
    change_summary: str = Field(min_length=1, max_length=320)
    change_explanation: str = Field(min_length=1, max_length=1200)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    risk_level: RiskLevel
    requires_approval: bool
    approval_reasons: list[str] = Field(default_factory=list, max_length=6)
    affected_code_locations: list[PlanCodeLocation] = Field(default_factory=list, max_length=8)
    tools: list[PlanTool] = Field(min_length=1, max_length=6)
    steps: list[PlanStep] = Field(min_length=1, max_length=10)
    final_output_contract: FinalOutputContract


class GeneratedPlan(GeneratedPlanPayload):
    schema_version: str = "phase2.plan.v2"
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4()}")
    task_id: str = Field(min_length=1, max_length=36)
    provider: dict[str, Any] | None = None


class SemanticTranslationPayload(BaseModel):
    normalized_request: str = Field(min_length=1, max_length=600)
    intent: str = Field(min_length=1, max_length=120)
    work_type: Literal["bugfix", "feature", "investigation", "operations", "question", "unknown"]
    objective: str = Field(min_length=1, max_length=240)
    issue_key: str | None = Field(default=None, max_length=32)
    issue_url: str | None = Field(default=None, max_length=1000)
    candidate_modules: list[str] = Field(default_factory=list, max_length=8)
    search_queries: list[str] = Field(default_factory=list, max_length=6)
    constraints: list[str] = Field(default_factory=list, max_length=8)
    requested_outputs: list[str] = Field(default_factory=list, max_length=6)
    grounding_terms: list[str] = Field(default_factory=list, max_length=10)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)


class GeneratedSemanticTranslation(SemanticTranslationPayload):
    schema_version: str = "phase4.semantic_translation.v1"
    translation_id: str = Field(default_factory=lambda: f"translation_{uuid4()}")
    task_id: str = Field(min_length=1, max_length=36)
    provider: dict[str, Any] | None = None


class ReviewFinding(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=240)
    step_id: str | None = Field(default=None, max_length=100)
    field: str | None = Field(default=None, max_length=100)


class PolicyCheck(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    status: Literal["passed", "warning", "failed"]
    detail: str = Field(min_length=1, max_length=240)


class ApprovalRequirement(BaseModel):
    action_name: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=240)
    approver_role: str = Field(default="team_lead", min_length=1, max_length=64)


class GeneratedReviewPayload(BaseModel):
    review_stage: Literal["pre_execution", "post_execution"]
    verdict: Literal["approved", "requires_approval", "needs_info", "rejected"]
    ready_for_execution: bool
    summary: str = Field(min_length=1, max_length=400)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=8)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    policy_checks: list[PolicyCheck] = Field(default_factory=list, max_length=8)
    approval_requirements: list[ApprovalRequirement] = Field(default_factory=list, max_length=6)
    recommended_status: str = Field(min_length=1, max_length=64)


class GeneratedReview(GeneratedReviewPayload):
    schema_version: str = "phase2.review.v1"
    review_id: str = Field(default_factory=lambda: f"review_{uuid4()}")
    task_id: str = Field(min_length=1, max_length=36)
    plan_id: str = Field(min_length=1, max_length=64)
    provider: dict[str, Any] | None = None


class PlanGenerationResult(BaseModel):
    plan: GeneratedPlan
    provider_name: str
    model_name: str | None = None
    used_fallback: bool = False
    fallback_reason: str | None = None


class SemanticTranslationResult(BaseModel):
    translation: GeneratedSemanticTranslation
    provider_name: str
    model_name: str | None = None
    used_fallback: bool = False
    fallback_reason: str | None = None


class ReviewGenerationResult(BaseModel):
    review: GeneratedReview
    provider_name: str
    model_name: str | None = None
