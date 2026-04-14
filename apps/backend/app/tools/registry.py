from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.enums import ToolPermissionCategory


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    display_name: str
    description: str
    provider_name: str
    permission_category: ToolPermissionCategory
    enabled: bool
    status_message: str
    missing_configuration: tuple[str, ...]
    requires_network: bool
    timeout_seconds: float
    retry_count: int
    tags: tuple[str, ...]


def _parse_permission_overrides(raw_value: str | None) -> dict[str, ToolPermissionCategory]:
    overrides: dict[str, ToolPermissionCategory] = {}
    if not raw_value:
        return overrides

    for raw_item in raw_value.split(";"):
        item = raw_item.strip()
        if not item or "=" not in item:
            continue
        tool_name, raw_category = item.split("=", 1)
        try:
            overrides[tool_name.strip()] = ToolPermissionCategory(raw_category.strip())
        except ValueError:
            continue
    return overrides


class ToolRegistry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.permission_overrides = _parse_permission_overrides(self.settings.tool_permission_overrides)
        self._definitions = self._build_definitions()

    def list_tools(self) -> list[ToolDefinition]:
        return [self.get_definition(name) for name in sorted(self._definitions)]

    def get_definition(self, tool_name: str) -> ToolDefinition:
        definition = self._definitions.get(tool_name)
        if definition is None:
            raise ValueError(f"Unsupported tool: {tool_name}")

        overridden_category = self.permission_overrides.get(tool_name)
        if overridden_category is None:
            return definition

        return ToolDefinition(
            name=definition.name,
            display_name=definition.display_name,
            description=definition.description,
            provider_name=definition.provider_name,
            permission_category=overridden_category,
            enabled=definition.enabled,
            status_message=definition.status_message,
            missing_configuration=definition.missing_configuration,
            requires_network=definition.requires_network,
            timeout_seconds=definition.timeout_seconds,
            retry_count=definition.retry_count,
            tags=definition.tags,
        )

    def get_permission_category(self, tool_name: str) -> ToolPermissionCategory:
        return self.get_definition(tool_name).permission_category

    @staticmethod
    def _status_message(*, enabled: bool, display_name: str, missing_configuration: tuple[str, ...]) -> str:
        if enabled:
            return f"{display_name} is ready."
        if missing_configuration:
            return f"{display_name} is disabled until {', '.join(missing_configuration)} is configured."
        return f"{display_name} is currently disabled."

    def _build_definitions(self) -> dict[str, ToolDefinition]:
        knowledge_missing: tuple[str, ...] = ()
        primary_agent_provider = str(getattr(self.settings, "primary_agent_provider", "auto"))
        openai_api_key = getattr(self.settings, "openai_api_key", None)
        minimax_api_key = getattr(self.settings, "minimax_api_key", None)
        deepseek_api_key = getattr(self.settings, "deepseek_api_key", None)
        codegen_missing = tuple(
            name
            for name, present in {
                "OPS_AGENT_OPENAI_API_KEY": primary_agent_provider != "openai" or bool(openai_api_key),
                "OPS_AGENT_MINIMAX_API_KEY": primary_agent_provider != "minimax" or bool(minimax_api_key),
                "OPS_AGENT_DEEPSEEK_API_KEY": primary_agent_provider != "deepseek" or bool(deepseek_api_key),
            }.items()
            if not present
        )
        codegen_requires_network = primary_agent_provider in {"openai", "minimax", "deepseek"} or (
            primary_agent_provider == "auto" and bool(openai_api_key or minimax_api_key or deepseek_api_key)
        )
        slack_missing = tuple(
            name
            for name, present in {
                "OPS_AGENT_SLACK_BOT_TOKEN": bool(self.settings.slack_bot_token),
            }.items()
            if not present
        )
        jira_missing = tuple(
            name
            for name, present in {
                "OPS_AGENT_JIRA_BASE_URL": bool(self.settings.jira_base_url),
                "Jira credentials": bool(self.settings.jira_api_token or self.settings.jira_bearer_token),
            }.items()
            if not present
        )
        internal_api_missing = tuple(
            name
            for name, present in {
                "OPS_AGENT_INTERNAL_API_BASE_URL": bool(self.settings.internal_api_base_url),
            }.items()
            if not present
        )
        internal_db_missing = tuple(
            name
            for name, present in {
                "OPS_AGENT_INTERNAL_DB_URL": bool(self.settings.internal_db_url),
            }.items()
            if not present
        )
        sandbox_command_timeout_seconds = float(
            getattr(
                self.settings,
                "sandbox_command_timeout_seconds",
                self.settings.tool_default_timeout_seconds,
            )
        )

        return {
            "knowledge.search": ToolDefinition(
                name="knowledge.search",
                display_name="Knowledge Search",
                description="Search indexed enterprise knowledge and code repositories.",
                provider_name="local_knowledge",
                permission_category=ToolPermissionCategory.READ_ONLY,
                enabled=True,
                status_message=self._status_message(
                    enabled=True,
                    display_name="Knowledge Search",
                    missing_configuration=knowledge_missing,
                ),
                missing_configuration=knowledge_missing,
                requires_network=False,
                timeout_seconds=self.settings.tool_default_timeout_seconds,
                retry_count=0,
                tags=("knowledge", "rag", "read"),
            ),
            "sandbox.run_command": ToolDefinition(
                name="sandbox.run_command",
                display_name="Sandbox Run Command",
                description="Execute a shell command inside an isolated sandbox directory for a task.",
                provider_name="sandbox",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=True,
                status_message="Sandbox execution is available.",
                missing_configuration=(),
                requires_network=False,
                timeout_seconds=sandbox_command_timeout_seconds,
                retry_count=0,
                tags=("sandbox", "execution", "shell"),
            ),
            "sandbox.apply_patch": ToolDefinition(
                name="sandbox.apply_patch",
                display_name="Sandbox Apply Patch",
                description="Apply a unified diff to the sandboxed repository, recording the pre-patch state for rollback.",
                provider_name="sandbox",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=True,
                status_message="Sandbox patch application is available.",
                missing_configuration=(),
                requires_network=False,
                timeout_seconds=30.0,
                retry_count=0,
                tags=("sandbox", "execution", "patch"),
            ),
            "test_pipeline.run": ToolDefinition(
                name="test_pipeline.run",
                display_name="Test Pipeline Run",
                description="Run the sandboxed repository's tests.yaml steps and return an aggregate pass/fail verdict.",
                provider_name="test_pipeline",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=True,
                status_message="Test pipeline execution is available.",
                missing_configuration=(),
                requires_network=False,
                timeout_seconds=sandbox_command_timeout_seconds,
                retry_count=0,
                tags=("sandbox", "execution", "tests", "pipeline"),
            ),
            "diff_reviewer.review": ToolDefinition(
                name="diff_reviewer.review",
                display_name="Diff Reviewer",
                description="Review a unified diff against deterministic safety and quality rules.",
                provider_name="diff_reviewer",
                permission_category=ToolPermissionCategory.READ_ONLY,
                enabled=True,
                status_message="Diff reviewer is available.",
                missing_configuration=(),
                requires_network=False,
                timeout_seconds=self.settings.tool_default_timeout_seconds,
                retry_count=0,
                tags=("review", "diff", "quality", "read"),
            ),
            "codegen.generate_patch": ToolDefinition(
                name="codegen.generate_patch",
                display_name="Codegen Generate Patch",
                description="Generate a unified diff from a plan document and source file context.",
                provider_name="codegen",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=not codegen_missing,
                status_message=self._status_message(
                    enabled=not codegen_missing,
                    display_name="Codegen Generate Patch",
                    missing_configuration=codegen_missing,
                ),
                missing_configuration=codegen_missing,
                requires_network=codegen_requires_network,
                timeout_seconds=float(
                    getattr(
                        self.settings,
                        "minimax_planner_timeout_seconds",
                        getattr(self.settings, "primary_agent_timeout_seconds", 90.0),
                    )
                ),
                retry_count=0,
                tags=("codegen", "llm", "code-change"),
            ),
            "slack.post_message": ToolDefinition(
                name="slack.post_message",
                display_name="Slack Post Message",
                description="Send a message to a configured Slack workspace channel.",
                provider_name="slack",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=not slack_missing,
                status_message=self._status_message(
                    enabled=not slack_missing,
                    display_name="Slack Post Message",
                    missing_configuration=slack_missing,
                ),
                missing_configuration=slack_missing,
                requires_network=True,
                timeout_seconds=self.settings.slack_post_message_timeout_seconds,
                retry_count=max(0, self.settings.slack_post_message_retry_count),
                tags=("slack", "messaging", "notification"),
            ),
            "jira.get_issue": ToolDefinition(
                name="jira.get_issue",
                display_name="Jira Get Issue",
                description="Read an existing Jira issue and return its current context.",
                provider_name="jira",
                permission_category=ToolPermissionCategory.READ_ONLY,
                enabled=not jira_missing,
                status_message=self._status_message(
                    enabled=not jira_missing,
                    display_name="Jira Get Issue",
                    missing_configuration=jira_missing,
                ),
                missing_configuration=jira_missing,
                requires_network=True,
                timeout_seconds=self.settings.jira_timeout_seconds,
                retry_count=max(0, self.settings.jira_retry_count),
                tags=("jira", "workflow", "read"),
            ),
            "jira.create_issue": ToolDefinition(
                name="jira.create_issue",
                display_name="Jira Create Issue",
                description="Create a Jira issue in the configured project.",
                provider_name="jira",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=not jira_missing,
                status_message=self._status_message(
                    enabled=not jira_missing,
                    display_name="Jira Create Issue",
                    missing_configuration=jira_missing,
                ),
                missing_configuration=jira_missing,
                requires_network=True,
                timeout_seconds=self.settings.jira_timeout_seconds,
                retry_count=max(0, self.settings.jira_retry_count),
                tags=("jira", "workflow", "issue"),
            ),
            "jira.transition_issue": ToolDefinition(
                name="jira.transition_issue",
                display_name="Jira Transition Issue",
                description="Move a Jira issue to a new workflow status via the transitions API.",
                provider_name="jira",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=not jira_missing,
                status_message=self._status_message(
                    enabled=not jira_missing,
                    display_name="Jira Transition Issue",
                    missing_configuration=jira_missing,
                ),
                missing_configuration=jira_missing,
                requires_network=True,
                timeout_seconds=self.settings.jira_timeout_seconds,
                retry_count=max(0, self.settings.jira_retry_count),
                tags=("jira", "workflow", "state-change"),
            ),
            "jira.add_comment": ToolDefinition(
                name="jira.add_comment",
                display_name="Jira Add Comment",
                description="Post a comment to a Jira issue, visible to all watchers.",
                provider_name="jira",
                permission_category=ToolPermissionCategory.WRITE,
                enabled=not jira_missing,
                status_message=self._status_message(
                    enabled=not jira_missing,
                    display_name="Jira Add Comment",
                    missing_configuration=jira_missing,
                ),
                missing_configuration=jira_missing,
                requires_network=True,
                timeout_seconds=self.settings.jira_timeout_seconds,
                retry_count=max(0, self.settings.jira_retry_count),
                tags=("jira", "workflow", "comment"),
            ),
            "internal_api.request": ToolDefinition(
                name="internal_api.request",
                display_name="Internal API Request",
                description="Call a configured internal enterprise API endpoint.",
                provider_name="internal_api",
                permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
                enabled=not internal_api_missing,
                status_message=self._status_message(
                    enabled=not internal_api_missing,
                    display_name="Internal API Request",
                    missing_configuration=internal_api_missing,
                ),
                missing_configuration=internal_api_missing,
                requires_network=True,
                timeout_seconds=self.settings.internal_api_timeout_seconds,
                retry_count=max(0, self.settings.internal_api_retry_count),
                tags=("internal", "api", "enterprise"),
            ),
            "internal_db.query": ToolDefinition(
                name="internal_db.query",
                display_name="Internal DB Query",
                description="Run a guarded read-only query against a configured internal database.",
                provider_name="internal_db",
                permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
                enabled=not internal_db_missing,
                status_message=self._status_message(
                    enabled=not internal_db_missing,
                    display_name="Internal DB Query",
                    missing_configuration=internal_db_missing,
                ),
                missing_configuration=internal_db_missing,
                requires_network=False,
                timeout_seconds=self.settings.internal_db_timeout_seconds,
                retry_count=max(0, self.settings.internal_db_retry_count),
                tags=("internal", "database", "read_only"),
            ),
        }
