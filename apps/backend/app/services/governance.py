from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.enums import ActorRole, PolicyDecision, RiskCategory, RiskLevel
from app.models.policy_rule import PolicyRule
from app.models.rbac_role import RbacRole


DEFAULT_RBAC_ROLES: list[dict[str, object]] = [
    {
        "role_key": ActorRole.EMPLOYEE,
        "display_name": "Employee",
        "description": "Default business user role with safe read and low-risk workflow access.",
        "is_human": True,
        "is_system": False,
        "is_active": True,
    },
    {
        "role_key": ActorRole.TEAM_LEAD,
        "display_name": "Team Lead",
        "description": "Approver role for team-scoped notifications and governed workflow execution.",
        "is_human": True,
        "is_system": False,
        "is_active": True,
    },
    {
        "role_key": ActorRole.MANAGER,
        "display_name": "Manager",
        "description": "Escalation approver for broader organizational actions and medium-risk changes.",
        "is_human": True,
        "is_system": False,
        "is_active": True,
    },
    {
        "role_key": ActorRole.ADMIN,
        "display_name": "Admin",
        "description": "Administrative operator with privileged configuration and production-control access.",
        "is_human": True,
        "is_system": False,
        "is_active": True,
    },
    {
        "role_key": ActorRole.SYSTEM,
        "display_name": "System",
        "description": "Internal service role used for orchestration and non-human workflow bookkeeping.",
        "is_human": False,
        "is_system": True,
        "is_active": True,
    },
]


DEFAULT_POLICY_RULES: list[dict[str, object]] = [
    {
        "rule_key": "knowledge.search.employee.allow.v1",
        "title": "Employee knowledge lookup",
        "description": "Knowledge search is allowed by default for standard employees.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "knowledge",
        "action_key": "search",
        "tool_name": "knowledge.search",
        "scope_selector": "default",
        "decision": PolicyDecision.ALLOW,
        "risk_level": RiskLevel.LOW,
        "risk_category": RiskCategory.KNOWLEDGE_LOOKUP,
        "required_approver_role": None,
        "constraints_json": {"citation_required": True},
        "metadata_json": {"phase": "phase5"},
        "priority": 10,
        "is_active": True,
    },
    {
        "rule_key": "slack.post_message.public.employee.approval.v1",
        "title": "Employee public Slack broadcast",
        "description": "Public Slack broadcasts require team lead approval for standard employees.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "slack",
        "action_key": "post_message",
        "tool_name": "slack.post_message",
        "scope_selector": "public_broadcast",
        "decision": PolicyDecision.REQUIRE_APPROVAL,
        "risk_level": RiskLevel.MEDIUM,
        "risk_category": RiskCategory.EXTERNAL_BROADCAST,
        "required_approver_role": ActorRole.TEAM_LEAD,
        "constraints_json": {"channel_scope": "public"},
        "metadata_json": {"phase": "phase5"},
        "priority": 20,
        "is_active": True,
    },
    {
        "rule_key": "slack.post_message.public.team_lead.approval.v1",
        "title": "Team lead public Slack broadcast",
        "description": "Team leads can send public Slack broadcasts only with manager approval.",
        "subject_role": ActorRole.TEAM_LEAD,
        "resource_type": "slack",
        "action_key": "post_message",
        "tool_name": "slack.post_message",
        "scope_selector": "public_broadcast",
        "decision": PolicyDecision.REQUIRE_APPROVAL,
        "risk_level": RiskLevel.MEDIUM,
        "risk_category": RiskCategory.CROSS_TEAM_NOTIFICATION,
        "required_approver_role": ActorRole.MANAGER,
        "constraints_json": {"channel_scope": "public"},
        "metadata_json": {"phase": "phase5"},
        "priority": 20,
        "is_active": True,
    },
    {
        "rule_key": "slack.post_message.public.manager.allow.v1",
        "title": "Manager public Slack broadcast",
        "description": "Managers can send public operational broadcasts without extra approval.",
        "subject_role": ActorRole.MANAGER,
        "resource_type": "slack",
        "action_key": "post_message",
        "tool_name": "slack.post_message",
        "scope_selector": "public_broadcast",
        "decision": PolicyDecision.ALLOW,
        "risk_level": RiskLevel.MEDIUM,
        "risk_category": RiskCategory.CROSS_TEAM_NOTIFICATION,
        "required_approver_role": None,
        "constraints_json": {"channel_scope": "public"},
        "metadata_json": {"phase": "phase5"},
        "priority": 30,
        "is_active": True,
    },
    {
        "rule_key": "jira.create_issue.employee.allow.v1",
        "title": "Employee Jira creation",
        "description": "Creating Jira work items is allowed by default.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "jira",
        "action_key": "create_issue",
        "tool_name": "jira.create_issue",
        "scope_selector": "default",
        "decision": PolicyDecision.ALLOW,
        "risk_level": RiskLevel.LOW,
        "risk_category": RiskCategory.CHANGE_MANAGEMENT,
        "required_approver_role": None,
        "constraints_json": {"project_scope": "assigned"},
        "metadata_json": {"phase": "phase5"},
        "priority": 10,
        "is_active": True,
    },
    {
        "rule_key": "jira.get_issue.employee.allow.v1",
        "title": "Employee Jira read",
        "description": "Reading Jira issues is allowed by default.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "jira",
        "action_key": "get_issue",
        "tool_name": "jira.get_issue",
        "scope_selector": "default",
        "decision": PolicyDecision.ALLOW,
        "risk_level": RiskLevel.LOW,
        "risk_category": RiskCategory.CHANGE_MANAGEMENT,
        "required_approver_role": None,
        "constraints_json": {"access": "read_only"},
        "metadata_json": {"phase": "phase5"},
        "priority": 10,
        "is_active": True,
    },
    {
        "rule_key": "notion.update_published_doc.team_lead.approval.v1",
        "title": "Team lead published Notion edits",
        "description": "Editing published Notion documentation requires approval.",
        "subject_role": ActorRole.TEAM_LEAD,
        "resource_type": "notion",
        "action_key": "update_published_doc",
        "tool_name": "notion.update_published_doc",
        "scope_selector": "published_doc",
        "decision": PolicyDecision.REQUIRE_APPROVAL,
        "risk_level": RiskLevel.MEDIUM,
        "risk_category": RiskCategory.CONFIGURATION_CHANGE,
        "required_approver_role": ActorRole.MANAGER,
        "constraints_json": {"document_state": "published"},
        "metadata_json": {"phase": "phase5"},
        "priority": 40,
        "is_active": True,
    },
    {
        "rule_key": "internal_api.request.employee.approval.v1",
        "title": "Employee internal API writes",
        "description": "Employee-initiated internal API writes require approval.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "internal_api",
        "action_key": "request",
        "tool_name": "internal_api.request",
        "scope_selector": "write",
        "decision": PolicyDecision.REQUIRE_APPROVAL,
        "risk_level": RiskLevel.HIGH,
        "risk_category": RiskCategory.CONFIGURATION_CHANGE,
        "required_approver_role": ActorRole.TEAM_LEAD,
        "constraints_json": {"method_scope": ["POST", "PUT", "PATCH", "DELETE"]},
        "metadata_json": {"phase": "phase5"},
        "priority": 50,
        "is_active": True,
    },
    {
        "rule_key": "internal_db.query.employee.deny.v1",
        "title": "Employee internal DB access",
        "description": "Direct internal database access is denied for employees.",
        "subject_role": ActorRole.EMPLOYEE,
        "resource_type": "internal_db",
        "action_key": "query",
        "tool_name": "internal_db.query",
        "scope_selector": "default",
        "decision": PolicyDecision.DENY,
        "risk_level": RiskLevel.HIGH,
        "risk_category": RiskCategory.PRIVILEGED_DATA_ACCESS,
        "required_approver_role": None,
        "constraints_json": {"default_mode": "deny"},
        "metadata_json": {"phase": "phase5"},
        "priority": 60,
        "is_active": True,
    },
    {
        "rule_key": "internal_db.query.manager.approval.v1",
        "title": "Manager internal DB read",
        "description": "Managers need admin approval for governed internal database access.",
        "subject_role": ActorRole.MANAGER,
        "resource_type": "internal_db",
        "action_key": "query",
        "tool_name": "internal_db.query",
        "scope_selector": "read_only",
        "decision": PolicyDecision.REQUIRE_APPROVAL,
        "risk_level": RiskLevel.HIGH,
        "risk_category": RiskCategory.PRIVILEGED_DATA_ACCESS,
        "required_approver_role": ActorRole.ADMIN,
        "constraints_json": {"read_only": True},
        "metadata_json": {"phase": "phase5"},
        "priority": 60,
        "is_active": True,
    },
    {
        "rule_key": "prod_config.change.admin.allow.v1",
        "title": "Admin production config change",
        "description": "Only admins can change production configuration.",
        "subject_role": ActorRole.ADMIN,
        "resource_type": "prod_config",
        "action_key": "change",
        "tool_name": None,
        "scope_selector": "production",
        "decision": PolicyDecision.ALLOW_WITH_CONSTRAINTS,
        "risk_level": RiskLevel.HIGH,
        "risk_category": RiskCategory.PRODUCTION_WRITE,
        "required_approver_role": None,
        "constraints_json": {"requires_audit_note": True, "rollback_plan_required": True},
        "metadata_json": {"phase": "phase5"},
        "priority": 80,
        "is_active": True,
    },
]


def bootstrap_governance_data() -> None:
    with SessionLocal() as db:
        service = GovernanceService(db)
        service.seed_defaults()
        db.commit()


class GovernanceService:
    def __init__(self, db: Session):
        self.db = db

    def seed_defaults(self) -> None:
        for role_definition in DEFAULT_RBAC_ROLES:
            existing_role = self.db.get(RbacRole, role_definition["role_key"])
            if existing_role is None:
                self.db.add(RbacRole(**role_definition))
                continue

            existing_role.display_name = str(role_definition["display_name"])
            existing_role.description = str(role_definition["description"])
            existing_role.is_human = bool(role_definition["is_human"])
            existing_role.is_system = bool(role_definition["is_system"])
            existing_role.is_active = bool(role_definition["is_active"])

        for rule_definition in DEFAULT_POLICY_RULES:
            stmt = select(PolicyRule).where(PolicyRule.rule_key == str(rule_definition["rule_key"]))
            existing_rule = self.db.scalars(stmt).first()
            if existing_rule is None:
                self.db.add(PolicyRule(**rule_definition))
                continue

            existing_rule.title = str(rule_definition["title"])
            existing_rule.description = str(rule_definition["description"])
            existing_rule.subject_role = rule_definition["subject_role"]  # type: ignore[assignment]
            existing_rule.resource_type = str(rule_definition["resource_type"])
            existing_rule.action_key = str(rule_definition["action_key"])
            existing_rule.tool_name = (
                str(rule_definition["tool_name"])
                if rule_definition["tool_name"] is not None
                else None
            )
            existing_rule.scope_selector = (
                str(rule_definition["scope_selector"])
                if rule_definition["scope_selector"] is not None
                else None
            )
            existing_rule.decision = rule_definition["decision"]  # type: ignore[assignment]
            existing_rule.risk_level = rule_definition["risk_level"]  # type: ignore[assignment]
            existing_rule.risk_category = rule_definition["risk_category"]  # type: ignore[assignment]
            existing_rule.required_approver_role = rule_definition["required_approver_role"]  # type: ignore[assignment]
            existing_rule.constraints_json = (
                dict(rule_definition["constraints_json"])
                if isinstance(rule_definition["constraints_json"], dict)
                else None
            )
            existing_rule.metadata_json = (
                dict(rule_definition["metadata_json"])
                if isinstance(rule_definition["metadata_json"], dict)
                else None
            )
            existing_rule.priority = int(rule_definition["priority"])
            existing_rule.is_active = bool(rule_definition["is_active"])

        self.db.flush()

    def list_roles(self, *, active_only: bool = True) -> list[RbacRole]:
        stmt = select(RbacRole).order_by(RbacRole.role_key.asc())
        if active_only:
            stmt = stmt.where(RbacRole.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def list_policy_rules(
        self,
        *,
        subject_role: ActorRole | None = None,
        resource_type: str | None = None,
        decision: PolicyDecision | None = None,
        active_only: bool = True,
    ) -> list[PolicyRule]:
        stmt = select(PolicyRule).order_by(PolicyRule.priority.asc(), PolicyRule.rule_key.asc())
        if active_only:
            stmt = stmt.where(PolicyRule.is_active.is_(True))
        if subject_role is not None:
            stmt = stmt.where(PolicyRule.subject_role == subject_role)
        if resource_type:
            stmt = stmt.where(PolicyRule.resource_type == resource_type.strip())
        if decision is not None:
            stmt = stmt.where(PolicyRule.decision == decision)
        return list(self.db.scalars(stmt))
