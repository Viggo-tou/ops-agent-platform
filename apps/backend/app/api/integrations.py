"""Integration connection status (read-only).

Reports which optional integrations are configured WITHOUT exposing
tokens or URLs. Frontend uses this to render the /integrations page
cards (configured / not configured / coming soon).

Real "configure via UI" (OAuth flows) ships in 1.1; for 1.0 the only
way to enable an integration is to set the relevant env vars and
restart the backend.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import ActorContext, require_permission

router = APIRouter(prefix="/integrations", tags=["integrations"])
ViewActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]


class IntegrationStatus(BaseModel):
    key: str
    label: str
    description: str
    category: str  # workflow / chat / data / vcs
    configured: bool
    status: str  # "connected" | "not_configured" | "coming_soon"
    config_hint: str = ""


class IntegrationsResponse(BaseModel):
    integrations: list[IntegrationStatus]


def _has_value(value: object) -> bool:
    """True when value is a non-empty string."""
    return bool(value and str(value).strip())


@router.get("/status", response_model=IntegrationsResponse)
def integrations_status(_actor: ViewActorCtx) -> IntegrationsResponse:
    s = get_settings()

    jira_configured = (
        _has_value(getattr(s, "jira_base_url", None))
        and _has_value(getattr(s, "jira_email", None))
        and (
            _has_value(getattr(s, "jira_api_token", None))
            or _has_value(getattr(s, "jira_bearer_token", None))
        )
    )
    slack_configured = _has_value(getattr(s, "slack_bot_token", None))
    internal_api_configured = _has_value(getattr(s, "internal_api_base_url", None))
    internal_db_configured = _has_value(getattr(s, "internal_db_url", None))

    integrations = [
        IntegrationStatus(
            key="github",
            label="GitHub",
            description=(
                "Connect a GitHub org so the agent can open PRs, comment on "
                "issues, and read repo metadata. Needed for 'Create PR' on "
                "completed tasks."
            ),
            category="vcs",
            configured=False,
            status="coming_soon",
            config_hint="OAuth flow ships in 1.1.",
        ),
        IntegrationStatus(
            key="jira",
            label="Jira",
            description=(
                "Read/write Jira issues. Used by the planner to fetch "
                "issue context and by the action stage to transition "
                "issues after approval."
            ),
            category="workflow",
            configured=jira_configured,
            status="connected" if jira_configured else "not_configured",
            config_hint="Set OPS_AGENT_JIRA_BASE_URL + OPS_AGENT_JIRA_EMAIL + OPS_AGENT_JIRA_API_TOKEN in .env, then restart.",
        ),
        IntegrationStatus(
            key="slack",
            label="Slack",
            description=(
                "Post task updates and approval notifications to channels. "
                "Includes a high-risk action 'slack.post_message' that "
                "requires approval per default policy rules."
            ),
            category="chat",
            configured=slack_configured,
            status="connected" if slack_configured else "not_configured",
            config_hint="Set OPS_AGENT_SLACK_BOT_TOKEN in .env, then restart.",
        ),
        IntegrationStatus(
            key="internal_api",
            label="Internal API",
            description=(
                "Forward arbitrary HTTP calls through the policy gate. "
                "Useful for connecting to in-house service endpoints "
                "without writing a custom tool."
            ),
            category="data",
            configured=internal_api_configured,
            status="connected" if internal_api_configured else "not_configured",
            config_hint="Set OPS_AGENT_INTERNAL_API_BASE_URL + OPS_AGENT_INTERNAL_API_TOKEN in .env, then restart.",
        ),
        IntegrationStatus(
            key="internal_db",
            label="Internal DB",
            description=(
                "Run read-only SQL through the policy gate. Useful when "
                "the agent needs to fetch recent rows for diagnosis."
            ),
            category="data",
            configured=internal_db_configured,
            status="connected" if internal_db_configured else "not_configured",
            config_hint="Set OPS_AGENT_INTERNAL_DB_URL in .env, then restart.",
        ),
        IntegrationStatus(
            key="teams",
            label="Microsoft Teams",
            description="Notification channel alternative to Slack.",
            category="chat",
            configured=False,
            status="coming_soon",
            config_hint="Ships in 1.2.",
        ),
    ]

    return IntegrationsResponse(integrations=integrations)
