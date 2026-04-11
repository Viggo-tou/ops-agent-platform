from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class WorkflowStage(str, Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    KNOWLEDGE = "knowledge"
    ACTION = "action"
    REVIEW = "review"
    DONE = "done"


class RoleName(str, Enum):
    PRIMARY = "primary"
    PLANNER = "planner"
    KNOWLEDGE = "knowledge"
    ACTION = "action"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class ActorRole(str, Enum):
    EMPLOYEE = "employee"
    TEAM_LEAD = "team_lead"
    MANAGER = "manager"
    ADMIN = "admin"
    SYSTEM = "system"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskCategory(str, Enum):
    GENERAL = "general"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    EXTERNAL_BROADCAST = "external_broadcast"
    CHANGE_MANAGEMENT = "change_management"
    CROSS_TEAM_NOTIFICATION = "cross_team_notification"
    CONFIGURATION_CHANGE = "configuration_change"
    PRODUCTION_WRITE = "production_write"
    PRIVILEGED_DATA_ACCESS = "privileged_data_access"
    KNOWLEDGE_EXFILTRATION = "knowledge_exfiltration"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    ALLOW_WITH_CONSTRAINTS = "allow_with_constraints"


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    USER_REQUEST_RECEIVED = "user_request_received"
    TASK_STATUS_CHANGED = "task_status_changed"
    SEMANTIC_TRANSLATION_STARTED = "semantic_translation_started"
    SEMANTIC_TRANSLATION_COMPLETED = "semantic_translation_completed"
    SEMANTIC_TRANSLATION_FAILED = "semantic_translation_failed"
    PLANNING_STARTED = "planning_started"
    PLAN_GENERATED = "plan_generated"
    REVIEW_STARTED = "review_started"
    REVIEW_PASSED = "review_passed"
    REVIEW_FAILED = "review_failed"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    KNOWLEDGE_RETRIEVED = "knowledge_retrieved"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_RETRY_SCHEDULED = "tool_retry_scheduled"
    TOOL_TIMED_OUT = "tool_timed_out"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    POLICY_EVALUATION_STARTED = "policy_evaluation_started"
    POLICY_EVALUATION_COMPLETED = "policy_evaluation_completed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_ASSIGNED = "approval_assigned"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_CANCELLED = "approval_cancelled"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    FINAL_RESPONSE_EMITTED = "final_response_emitted"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLBACK_COMPLETED = "rollback_completed"


class EventSource(str, Enum):
    API = "api"
    ORCHESTRATOR = "orchestrator"
    TOOL_GATEWAY = "tool_gateway"
    APPROVAL = "approval"
    GOVERNANCE = "governance"
    SYSTEM = "system"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolPermissionCategory(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    APPROVAL_REQUIRED = "approval_required"


class ToolExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
