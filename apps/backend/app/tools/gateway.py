from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from time import perf_counter
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import (
    ActorRole,
    ApprovalStatus,
    EventSource,
    EventType,
    RiskCategory,
    RiskLevel,
    RoleName,
    ToolExecutionStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.core.telemetry import get_tracer
from app.core.timeouts import external_http_timeout
from app.models.approval import Approval
from app.models.tool_execution import ToolExecution
from app.models.base import utcnow
from app.schemas.tool import ToolRegistryEntryRead
from app.services.codegen import CodeGenerator, CodegenError
from app.services.events import record_event
from app.services.knowledge import KnowledgeService
from app.services.reviewer import DiffReviewer, ReviewContext
from app.services.sandbox import ExecutionSandbox, SandboxError
from app.services.test_pipeline import TestPipeline, TestPipelineError
from app.tools.registry import ToolDefinition, ToolRegistry


def _set_span_attribute(span: object, key: str, value: object | None) -> None:
    if value is None:
        return
    if hasattr(value, "value"):
        value = getattr(value, "value")
    span.set_attribute(key, value)


class ToolInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        timed_out: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.timed_out = timed_out
        self.http_status = http_status


class ToolApprovalRequired(Exception):
    """Raised when a tool requires approval before execution."""

    def __init__(self, tool_name: str, execution_id: str, approval_id: str):
        super().__init__(f"Tool '{tool_name}' requires approval (approval_id={approval_id})")
        self.tool_name = tool_name
        self.execution_id = execution_id
        self.approval_id = approval_id


class ToolGateway:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        self.registry = ToolRegistry(self.settings)
        self.knowledge_service = KnowledgeService(db)

    def list_registry_entries(self) -> list[ToolRegistryEntryRead]:
        return [
            ToolRegistryEntryRead(
                name=definition.name,
                display_name=definition.display_name,
                description=definition.description,
                provider_name=definition.provider_name,
                permission_category=definition.permission_category,
                enabled=definition.enabled,
                status_message=definition.status_message,
                missing_configuration=list(definition.missing_configuration),
                requires_network=definition.requires_network,
                timeout_seconds=definition.timeout_seconds,
                retry_count=definition.retry_count,
                tags=list(definition.tags),
            )
            for definition in self.registry.list_tools()
        ]

    def get_category(self, tool_name: str) -> ToolPermissionCategory:
        return self.registry.get_permission_category(tool_name)

    def execute(
        self,
        *,
        task_id: str,
        tool_name: str,
        payload: dict[str, object],
        actor_context: dict[str, object],
        session_id: str | None = None,
        stage: WorkflowStage | None = None,
        role: RoleName | None = None,
        approval_id: str | None = None,
    ) -> dict[str, object]:
        with get_tracer().start_as_current_span("tool.execute") as span:
            _set_span_attribute(span, "task.id", task_id)
            _set_span_attribute(span, "tool.name", tool_name)
            _set_span_attribute(span, "workflow.stage", stage)
            _set_span_attribute(span, "actor.role", role)
            _set_span_attribute(span, "approval.id", approval_id)
            return self._execute_with_span(
                task_id=task_id,
                tool_name=tool_name,
                payload=payload,
                actor_context=actor_context,
                session_id=session_id,
                stage=stage,
                role=role,
                approval_id=approval_id,
            )

    def _execute_with_span(
        self,
        *,
        task_id: str,
        tool_name: str,
        payload: dict[str, object],
        actor_context: dict[str, object],
        session_id: str | None = None,
        stage: WorkflowStage | None = None,
        role: RoleName | None = None,
        approval_id: str | None = None,
    ) -> dict[str, object]:
        definition = self.registry.get_definition(tool_name)
        execution = (
            self._resume_pending_execution(approval_id=approval_id, tool_name=tool_name)
            if approval_id is not None
            else None
        )
        if execution is None:
            execution = ToolExecution(
                task_id=task_id,
                session_id=session_id,
                approval_id=approval_id,
                tool_name=tool_name,
                provider_name=definition.provider_name,
                permission_category=definition.permission_category,
                status=ToolExecutionStatus.RUNNING,
                actor_name=str(actor_context.get("actor_name")) if actor_context.get("actor_name") else None,
                attempt_count=0,
                max_retries=definition.retry_count,
                timeout_seconds=definition.timeout_seconds,
                request_payload_json=payload,
                attempt_log_json=[],
            )
            self.db.add(execution)
            self.db.flush()

        if definition.permission_category == ToolPermissionCategory.APPROVAL_REQUIRED and approval_id is None:
            approval = Approval(
                task_id=task_id,
                action_name=tool_name,
                status=ApprovalStatus.PENDING,
                requested_by_role=role if role else RoleName.ACTION,
                approver_role=ActorRole.TEAM_LEAD.value,
                requested_by_actor_name=str(actor_context.get("actor_name") or ""),
                risk_level=RiskLevel.HIGH,
                risk_category=RiskCategory.CHANGE_MANAGEMENT,
                reason=f"Tool '{tool_name}' requires approval before execution.",
                request_payload_json=payload,
            )
            self.db.add(approval)
            self.db.flush()

            execution.status = ToolExecutionStatus.PENDING_APPROVAL
            execution.approval_id = approval.id
            self.db.flush()

            raise ToolApprovalRequired(
                tool_name=tool_name,
                execution_id=execution.id,
                approval_id=approval.id,
            )

        total_attempts = definition.retry_count + 1
        started = perf_counter()

        for attempt in range(1, total_attempts + 1):
            attempt_started = perf_counter()
            try:
                result = self._execute_once(
                    task_id=task_id,
                    definition=definition,
                    payload=payload,
                    actor_context=actor_context,
                )
                duration_ms = int((perf_counter() - started) * 1000)
                attempt_duration_ms = int((perf_counter() - attempt_started) * 1000)
                attempt_log = list(execution.attempt_log_json or [])
                attempt_log.append(
                    {
                        "attempt": attempt,
                        "status": ToolExecutionStatus.SUCCEEDED.value,
                        "duration_ms": attempt_duration_ms,
                    }
                )
                execution.attempt_log_json = attempt_log
                execution.attempt_count = attempt
                execution.status = ToolExecutionStatus.SUCCEEDED
                execution.duration_ms = duration_ms
                execution.response_payload_json = result
                execution.inverse_action_json = self._build_inverse_action(definition.name, payload, result)
                execution.finished_at = utcnow()
                self.db.flush()
                return result
            except ToolInvocationError as exc:
                attempt_duration_ms = int((perf_counter() - attempt_started) * 1000)
                attempt_log = list(execution.attempt_log_json or [])
                attempt_log.append(
                    {
                        "attempt": attempt,
                        "status": ToolExecutionStatus.TIMED_OUT.value if exc.timed_out else ToolExecutionStatus.FAILED.value,
                        "duration_ms": attempt_duration_ms,
                        "error": str(exc),
                        "retryable": exc.retryable,
                    }
                )
                execution.attempt_log_json = attempt_log
                execution.attempt_count = attempt

                should_retry = exc.retryable and attempt < total_attempts
                if should_retry:
                    record_event(
                        self.db,
                        task_id=task_id,
                        event_type=EventType.TOOL_RETRY_SCHEDULED,
                        source=EventSource.TOOL_GATEWAY,
                        stage=stage,
                        role=role,
                        tool_name=tool_name,
                        message="Tool execution attempt failed and will be retried.",
                        payload={
                            "tool_execution_id": execution.id,
                            "attempt": attempt,
                            "max_attempts": total_attempts,
                            "error": str(exc),
                        },
                    )
                    continue

                duration_ms = int((perf_counter() - started) * 1000)
                execution.status = ToolExecutionStatus.TIMED_OUT if exc.timed_out else ToolExecutionStatus.FAILED
                execution.duration_ms = duration_ms
                execution.error_message = str(exc)
                execution.finished_at = utcnow()
                self.db.flush()
                raise

        raise RuntimeError("Tool execution terminated unexpectedly without a result")

    def _resume_pending_execution(self, *, approval_id: str, tool_name: str) -> ToolExecution | None:
        stmt = (
            select(ToolExecution)
            .where(
                ToolExecution.approval_id == approval_id,
                ToolExecution.tool_name == tool_name,
                ToolExecution.status == ToolExecutionStatus.PENDING_APPROVAL,
            )
            .order_by(ToolExecution.started_at.desc())
            .limit(1)
        )
        execution = self.db.scalars(stmt).first()
        if not isinstance(execution, ToolExecution):
            return None

        execution.approval_id = approval_id
        execution.status = ToolExecutionStatus.RUNNING
        execution.finished_at = None
        execution.error_message = None
        self.db.flush()
        return execution

    def _execute_once(
        self,
        *,
        task_id: str,
        definition: ToolDefinition,
        payload: Mapping[str, object],
        actor_context: Mapping[str, object],
    ) -> dict[str, object]:
        with get_tracer().start_as_current_span("tool.execute_once") as span:
            _set_span_attribute(span, "tool.name", definition.name)
            _set_span_attribute(span, "tool.provider", definition.provider_name)
            _set_span_attribute(span, "tool.permission_category", definition.permission_category)
            return self._execute_once_impl(
                task_id=task_id,
                definition=definition,
                payload=payload,
                actor_context=actor_context,
            )

    def _execute_once_impl(
        self,
        *,
        task_id: str,
        definition: ToolDefinition,
        payload: Mapping[str, object],
        actor_context: Mapping[str, object],
    ) -> dict[str, object]:
        if not definition.enabled and definition.name != "knowledge.search":
            raise ToolInvocationError(
                f"{definition.name} is not enabled. Configure the required environment variables before using it."
            )

        if definition.name == "knowledge.search":
            return self._execute_knowledge_search(
                payload,
                task_id=task_id,
                actor_name=str(actor_context.get("actor_name") or "") or None,
            )
        if definition.name == "sandbox.run_command":
            return self._execute_sandbox_run_command(definition=definition, payload=payload)
        if definition.name == "sandbox.apply_patch":
            return self._execute_sandbox_apply_patch(definition=definition, payload=payload)
        if definition.name == "test_pipeline.run":
            return self._execute_test_pipeline_run(definition=definition, payload=payload)
        if definition.name == "diff_reviewer.review":
            return self._execute_diff_reviewer_review(payload)
        if definition.name == "codegen.generate_patch":
            return self._execute_codegen_generate_patch(
                task_id=task_id,
                payload=payload,
                actor_context=actor_context,
            )
        if definition.name == "jira.get_issue":
            return self._execute_jira_get_issue(definition=definition, payload=payload)
        if definition.name == "slack.post_message":
            return self._execute_slack_post_message(definition=definition, payload=payload)
        if definition.name == "jira.create_issue":
            return self._execute_jira_create_issue(definition=definition, payload=payload)
        if definition.name == "jira.transition_issue":
            return self._execute_jira_transition_issue(definition=definition, payload=payload)
        if definition.name == "jira.add_comment":
            return self._execute_jira_add_comment(definition=definition, payload=payload)
        if definition.name == "internal_api.request":
            return self._execute_internal_api_request(definition=definition, payload=payload)
        if definition.name == "internal_db.query":
            return self._execute_internal_db_query(payload)

        raise ToolInvocationError(f"Unsupported tool: {definition.name}")

    @staticmethod
    def _build_inverse_action(
        tool_name: str,
        payload: Mapping[str, object],
        result: Mapping[str, object],
    ) -> dict[str, object] | None:
        if tool_name == "sandbox.apply_patch":
            before_sha = str(result.get("before_sha") or "").strip()
            sandbox_dir = str(result.get("sandbox_dir") or "").strip()
            if not before_sha or not sandbox_dir:
                return None
            return {
                "type": "git_revert",
                "sandbox_dir": sandbox_dir,
                "before_sha": before_sha,
            }

        if tool_name == "jira.transition_issue":
            issue_key = str(result.get("issue_key") or payload.get("issue_key") or "").strip().upper()
            previous_status = str(result.get("from_status") or "").strip()
            current_status = str(result.get("to_status") or "").strip()
            if not issue_key or not previous_status or not current_status:
                return None
            return {
                "type": "jira_transition",
                "issue_key": issue_key,
                "from_status": current_status,
                "to_status": previous_status,
            }

        if tool_name == "jira.add_comment":
            issue_key = str(result.get("issue_key") or payload.get("issue_key") or "").strip().upper()
            comment_id = str(result.get("comment_id") or "").strip()
            if not issue_key or not comment_id:
                return None
            return {
                "type": "jira_delete_comment",
                "issue_key": issue_key,
                "comment_id": comment_id,
            }

        return None

    def _execute_knowledge_search(
        self,
        payload: Mapping[str, object],
        *,
        task_id: str | None,
        actor_name: str | None,
    ) -> dict[str, object]:
        query = str(payload.get("query", "")).strip()
        top_k = payload.get("top_k")
        source_name = payload.get("source_name")
        language = payload.get("language")
        result = self.knowledge_service.search_repositories(
            query=query,
            top_k=int(top_k) if isinstance(top_k, int) else None,
            source_name=str(source_name) if isinstance(source_name, str) and source_name else None,
            language=str(language) if isinstance(language, str) and language else None,
            task_id=task_id,
            actor_name=actor_name,
        )
        return result.model_dump(mode="json")

    def _execute_sandbox_run_command(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        task_id = str(payload.get("task_id") or "").strip()
        command = str(payload.get("command") or "").strip()
        cwd_value = payload.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None

        if not task_id or not command:
            raise ToolInvocationError(
                "sandbox.run_command requires 'task_id' and 'command' in payload.",
                retryable=False,
            )

        try:
            sandbox = ExecutionSandbox(
                task_id=task_id,
                base_dir=str(getattr(self.settings, "sandbox_base_dir", "data/sandboxes")),
            )
            if not sandbox.exists():
                raise ToolInvocationError(
                    f"No sandbox found for task {task_id}. Clone a repo first.",
                    retryable=False,
                )

            result = sandbox.run(
                command,
                cwd=cwd,
                timeout_seconds=float(
                    getattr(
                        self.settings,
                        "sandbox_command_timeout_seconds",
                        definition.timeout_seconds,
                    )
                ),
                max_output_bytes=int(getattr(self.settings, "sandbox_max_output_bytes", 65536)),
            )
        except SandboxError as exc:
            raise ToolInvocationError(str(exc), retryable=False) from exc

        return {
            "status": "executed",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            **result,
        }

    def _execute_sandbox_apply_patch(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        task_id = str(payload.get("task_id") or "").strip()
        patch_value = payload.get("patch")
        patch = patch_value if isinstance(patch_value, str) else ""
        if not task_id or not patch.strip():
            raise ToolInvocationError(
                "sandbox.apply_patch requires 'task_id' and 'patch' in payload.",
                retryable=False,
            )

        commit_value = payload.get("commit", True)
        commit = commit_value if isinstance(commit_value, bool) else True
        commit_message_value = payload.get("commit_message")
        commit_message = (
            commit_message_value.strip()
            if isinstance(commit_message_value, str) and commit_message_value.strip()
            else "Applied patch via sandbox"
        )
        context_files_value = payload.get("context_files")
        context_files = (
            {str(path): str(content) for path, content in context_files_value.items()}
            if isinstance(context_files_value, dict)
            and all(isinstance(path, str) and isinstance(content, str) for path, content in context_files_value.items())
            else None
        )

        try:
            sandbox = ExecutionSandbox(
                task_id=task_id,
                base_dir=str(getattr(self.settings, "sandbox_base_dir", "data/sandboxes")),
            )
            result = sandbox.apply_patch(
                patch,
                context_files=context_files,
                commit=commit,
                commit_message=commit_message,
                timeout_seconds=definition.timeout_seconds,
            )
        except SandboxError as exc:
            raise ToolInvocationError(str(exc), retryable=False) from exc

        return {
            "status": "patched",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            **result,
        }

    def _execute_test_pipeline_run(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        task_id = str(payload.get("task_id") or "").strip()
        config_path_value = payload.get("config_path")
        config_path = (
            config_path_value.strip()
            if isinstance(config_path_value, str) and config_path_value.strip()
            else "tests.yaml"
        )
        if not task_id:
            raise ToolInvocationError(
                "test_pipeline.run requires 'task_id' in payload.",
                retryable=False,
            )

        try:
            sandbox = ExecutionSandbox(
                task_id=task_id,
                base_dir=str(getattr(self.settings, "sandbox_base_dir", "data/sandboxes")),
            )
            result = TestPipeline(sandbox).run(
                config_path=config_path,
                max_output_bytes=int(getattr(self.settings, "sandbox_max_output_bytes", 65536)),
            )
        except (SandboxError, TestPipelineError) as exc:
            raise ToolInvocationError(str(exc), retryable=False) from exc

        return {
            "status": "passed" if result.overall_passed else "failed",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            **asdict(result),
        }

    def _execute_diff_reviewer_review(self, payload: Mapping[str, object]) -> dict[str, object]:
        diff_value = payload.get("diff")
        if not isinstance(diff_value, str):
            raise ToolInvocationError(
                "diff_reviewer.review requires 'diff' as a string in payload.",
                retryable=False,
            )

        test_result_value = payload.get("test_result")
        if test_result_value is None:
            test_result = None
        elif isinstance(test_result_value, dict):
            test_result = test_result_value
        else:
            raise ToolInvocationError(
                "diff_reviewer.review optional 'test_result' must be a dict.",
                retryable=False,
            )

        protected_paths_value = payload.get("protected_paths")
        if protected_paths_value is None:
            protected_paths = None
        elif isinstance(protected_paths_value, list) and all(
            isinstance(item, str) for item in protected_paths_value
        ):
            protected_paths = protected_paths_value
        else:
            raise ToolInvocationError(
                "diff_reviewer.review optional 'protected_paths' must be a list of strings.",
                retryable=False,
            )

        max_diff_size_value = payload.get("max_diff_size", 50_000)
        if isinstance(max_diff_size_value, bool) or not isinstance(max_diff_size_value, int):
            raise ToolInvocationError(
                "diff_reviewer.review optional 'max_diff_size' must be an integer.",
                retryable=False,
            )
        if max_diff_size_value <= 0:
            raise ToolInvocationError(
                "diff_reviewer.review optional 'max_diff_size' must be greater than zero.",
                retryable=False,
            )

        task_description_value = payload.get("task_description")
        task_description = task_description_value if isinstance(task_description_value, str) else ""

        result = DiffReviewer(
            protected_paths=protected_paths,
            max_diff_size=max_diff_size_value,
        ).review(
            ReviewContext(
                diff=diff_value,
                test_result=test_result,
                task_description=task_description,
            )
        )
        return asdict(result)

    def _execute_codegen_generate_patch(
        self,
        *,
        task_id: str,
        payload: Mapping[str, object],
        actor_context: Mapping[str, object],
    ) -> dict[str, object]:
        plan_json_value = payload.get("plan_json")
        if not isinstance(plan_json_value, dict):
            raise ToolInvocationError(
                "codegen.generate_patch requires 'plan_json' as a dict in payload.",
                retryable=False,
            )

        context_files_value = payload.get("context_files")
        if not isinstance(context_files_value, dict) or not all(
            isinstance(path, str) and isinstance(content, str)
            for path, content in context_files_value.items()
        ):
            raise ToolInvocationError(
                "codegen.generate_patch requires 'context_files' as a dict[str, str] in payload.",
                retryable=False,
            )

        task_description_value = payload.get("task_description")
        task_description = task_description_value if isinstance(task_description_value, str) else ""

        source_repo_path_value = payload.get("source_repo_path")
        source_repo_path = str(source_repo_path_value) if isinstance(source_repo_path_value, str) else None

        try:
            actor_name_value = actor_context.get("actor_name")
            result = CodeGenerator(self.settings, db=self.db).generate_patch(
                task_id=task_id,
                plan_json=dict(plan_json_value),
                context_files={str(path): str(content) for path, content in context_files_value.items()},
                task_description=task_description,
                source_repo_path=source_repo_path,
                actor_name=str(actor_name_value) if actor_name_value else None,
            )
        except CodegenError as exc:
            raise ToolInvocationError(str(exc), retryable=False) from exc

        return result.model_dump(mode="json")

    def _execute_slack_post_message(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        token = self.settings.slack_bot_token
        if not token:
            raise ToolInvocationError("Slack bot token is not configured.")

        channel = str(payload.get("channel") or self.settings.slack_default_channel or "").strip()
        text_value = str(payload.get("text") or "").strip()
        if not channel:
            raise ToolInvocationError("Slack payload did not include a target channel.")
        if not text_value:
            raise ToolInvocationError("Slack payload did not include message text.")

        response = self._request_json(
            method="POST",
            url=f"{self.settings.slack_base_url.rstrip('/')}/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json_body={"channel": channel, "text": text_value},
            timeout_seconds=definition.timeout_seconds,
        )
        response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
        if not response_data.get("ok"):
            raise ToolInvocationError(
                f"Slack rejected the request: {response_data.get('error', 'unknown_error')}",
                retryable=False,
            )

        return {
            "status": "sent",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "channel": str(response_data.get("channel") or channel),
            "message_ts": response_data.get("ts"),
            "text": text_value,
        }

    def _execute_jira_create_issue(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        base_url = self._resolve_jira_site_root()
        if not base_url:
            raise ToolInvocationError("Jira base URL is not configured.")

        project_key = str(payload.get("project_key") or self.settings.jira_project_key or "").strip()
        summary = str(payload.get("summary") or "").strip()
        description = str(payload.get("description") or summary).strip()
        issue_type = str(payload.get("issue_type") or self.settings.jira_issue_type).strip() or "Task"

        if not project_key:
            raise ToolInvocationError("Jira project key is not configured.")
        if not summary:
            raise ToolInvocationError("Jira payload did not include an issue summary.")

        headers: dict[str, str] = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if self.settings.jira_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.jira_bearer_token}"
        elif self.settings.jira_email and self.settings.jira_api_token:
            auth = (self.settings.jira_email, self.settings.jira_api_token)
        else:
            raise ToolInvocationError("Jira credentials are not configured.")

        response = self._request_json(
            method="POST",
            url=f"{base_url.rstrip('/')}/rest/api/3/issue",
            headers=headers,
            auth=auth,
            json_body={
                "fields": {
                    "project": {"key": project_key},
                    "summary": summary,
                    "description": description,
                    "issuetype": {"name": issue_type},
                }
            },
            timeout_seconds=definition.timeout_seconds,
        )
        response_data = response.get("data") if isinstance(response.get("data"), dict) else {}

        issue_key = str(response_data.get("key") or "").strip()
        issue_id = str(response_data.get("id") or "").strip()
        browse_url = f"{base_url.rstrip('/')}/browse/{issue_key}" if issue_key else None

        return {
            "status": "created",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "issue_key": issue_key,
            "issue_id": issue_id,
            "issue_url": browse_url,
            "summary": summary,
        }

    def _execute_jira_get_issue(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        base_url = self._resolve_jira_site_root()
        if not base_url:
            raise ToolInvocationError("Jira base URL is not configured.")

        issue_key = str(payload.get("issue_key") or "").strip().upper()
        if not issue_key:
            raise ToolInvocationError("Jira payload did not include an issue key.")

        headers: dict[str, str] = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if self.settings.jira_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.jira_bearer_token}"
        elif self.settings.jira_email and self.settings.jira_api_token:
            auth = (self.settings.jira_email, self.settings.jira_api_token)
        else:
            raise ToolInvocationError("Jira credentials are not configured.")

        response = self._request_json(
            method="GET",
            url=f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}",
            headers=headers,
            auth=auth,
            params={"fields": "summary,description,status,issuetype,priority,assignee,reporter,labels"},
            timeout_seconds=definition.timeout_seconds,
        )
        response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
        fields = response_data.get("fields") if isinstance(response_data.get("fields"), dict) else {}

        summary = str(fields.get("summary") or "").strip()
        if not str(response_data.get("key") or "").strip() or not summary:
            raise ToolInvocationError(
                "Jira returned an unexpected issue payload. Check OPS_AGENT_JIRA_BASE_URL and issue access permissions.",
                retryable=False,
            )
        issue_status = ""
        if isinstance(fields.get("status"), dict):
            issue_status = str(fields["status"].get("name") or "").strip()
        issue_type = ""
        if isinstance(fields.get("issuetype"), dict):
            issue_type = str(fields["issuetype"].get("name") or "").strip()
        priority = ""
        if isinstance(fields.get("priority"), dict):
            priority = str(fields["priority"].get("name") or "").strip()
        labels = fields.get("labels") if isinstance(fields.get("labels"), list) else []
        description_text = self._extract_jira_description(fields.get("description"))
        browse_url = f"{base_url.rstrip('/')}/browse/{issue_key}"

        return {
            "status": "retrieved",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "issue_key": str(response_data.get("key") or issue_key),
            "issue_id": str(response_data.get("id") or ""),
            "summary": summary,
            "description": description_text,
            "issue_status": issue_status,
            "issue_type": issue_type,
            "priority": priority,
            "labels": [str(label) for label in labels if isinstance(label, str)],
            "issue_url": browse_url,
        }

    def _execute_jira_transition_issue(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        base_url = self._resolve_jira_site_root()
        if not base_url:
            raise ToolInvocationError("Jira base URL is not configured.")

        issue_key = str(payload.get("issue_key") or "").strip().upper()
        transition_name = str(payload.get("transition_name") or "").strip()
        if not issue_key:
            raise ToolInvocationError("Jira payload did not include an issue key.")
        if not transition_name:
            raise ToolInvocationError("Jira payload did not include a transition name.")

        headers: dict[str, str] = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if self.settings.jira_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.jira_bearer_token}"
        elif self.settings.jira_email and self.settings.jira_api_token:
            auth = (self.settings.jira_email, self.settings.jira_api_token)
        else:
            raise ToolInvocationError("Jira credentials are not configured.")

        issue_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}"
        try:
            from_response = self._request_json(
                method="GET",
                url=issue_url,
                headers=headers,
                auth=auth,
                params={"fields": "status"},
                timeout_seconds=definition.timeout_seconds,
            )
        except ToolInvocationError as exc:
            raise ToolInvocationError(
                f"Jira issue status lookup failed for {issue_key}: {exc}",
                retryable=True,
                timed_out=exc.timed_out,
            ) from exc

        from_status = self._extract_jira_status_name(self._jira_response_data(from_response))

        transitions_response = self._request_json(
            method="GET",
            url=f"{issue_url}/transitions",
            headers=headers,
            auth=auth,
            timeout_seconds=definition.timeout_seconds,
        )
        transitions_data = self._jira_response_data(transitions_response)
        transitions = transitions_data.get("transitions") if isinstance(transitions_data.get("transitions"), list) else []
        available_names: list[str] = []
        matched_transition: Mapping[str, object] | None = None

        for transition in transitions:
            if not isinstance(transition, Mapping):
                continue
            name = str(transition.get("name") or "").strip()
            if not name:
                continue
            available_names.append(name)
            if name.casefold() == transition_name.casefold():
                matched_transition = transition

        if matched_transition is None:
            available_text = ", ".join(available_names) if available_names else "none"
            raise ToolInvocationError(
                f"Jira transition '{transition_name}' is not available for {issue_key}. "
                f"Available transitions: {available_text}.",
                retryable=False,
            )

        transition_id = str(matched_transition.get("id") or "").strip()
        matched_name = str(matched_transition.get("name") or transition_name).strip()
        if not transition_id:
            raise ToolInvocationError(
                f"Jira returned transition '{matched_name}' without an id.",
                retryable=False,
            )

        self._request_json(
            method="POST",
            url=f"{issue_url}/transitions",
            headers=headers,
            auth=auth,
            json_body={"transition": {"id": transition_id}},
            timeout_seconds=definition.timeout_seconds,
        )

        to_response = self._request_json(
            method="GET",
            url=issue_url,
            headers=headers,
            auth=auth,
            params={"fields": "status"},
            timeout_seconds=definition.timeout_seconds,
        )
        to_status = self._extract_jira_status_name(self._jira_response_data(to_response))

        return {
            "status": "transitioned",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "issue_key": issue_key,
            "transition_id": transition_id,
            "transition_name": matched_name,
            "from_status": from_status,
            "to_status": to_status,
            "issue_url": f"{base_url.rstrip('/')}/browse/{issue_key}",
        }

    def _execute_jira_add_comment(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        base_url = self._resolve_jira_site_root()
        if not base_url:
            raise ToolInvocationError("Jira base URL is not configured.")

        issue_key = str(payload.get("issue_key") or "").strip().upper()
        text_value = str(payload.get("text") or "").strip()
        if not issue_key:
            raise ToolInvocationError("Jira payload did not include an issue key.")
        if not text_value:
            raise ToolInvocationError("Jira payload did not include comment text.")

        headers: dict[str, str] = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if self.settings.jira_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.jira_bearer_token}"
        elif self.settings.jira_email and self.settings.jira_api_token:
            auth = (self.settings.jira_email, self.settings.jira_api_token)
        else:
            raise ToolInvocationError("Jira credentials are not configured.")

        comment_body: dict[str, object] = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": text_value}],
                    }
                ],
            }
        }
        response = self._request_json(
            method="POST",
            url=f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}/comment",
            headers=headers,
            auth=auth,
            json_body=comment_body,
            timeout_seconds=definition.timeout_seconds,
        )
        response_data = self._jira_response_data(response)

        return {
            "status": "commented",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "issue_key": issue_key,
            "comment_id": str(response_data.get("id") or "").strip(),
            "created": str(response_data.get("created") or "").strip(),
            "excerpt": text_value[:200],
            "issue_url": f"{base_url.rstrip('/')}/browse/{issue_key}",
        }

    def _resolve_jira_site_root(self) -> str | None:
        raw_value = (self.settings.jira_base_url or "").strip()
        if not raw_value:
            return None

        parsed = urlparse(raw_value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

        return raw_value.rstrip("/")

    def _execute_internal_api_request(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        base_url = self.settings.internal_api_base_url
        if not base_url:
            raise ToolInvocationError("Internal API base URL is not configured.")

        method = str(payload.get("method") or "GET").upper()
        path = str(payload.get("path") or "/").strip()
        if not path.startswith("/"):
            path = f"/{path}"

        headers: dict[str, str] = {"Accept": "application/json"}
        if self.settings.internal_api_token:
            headers[self.settings.internal_api_auth_header] = f"Bearer {self.settings.internal_api_token}"

        response = self._request_json(
            method=method,
            url=f"{base_url.rstrip('/')}{path}",
            headers=headers,
            json_body=payload.get("body") if isinstance(payload.get("body"), dict) else None,
            params=payload.get("query") if isinstance(payload.get("query"), dict) else None,
            timeout_seconds=definition.timeout_seconds,
        )

        return {
            "status": "completed",
            "tool_name": definition.name,
            "provider": definition.provider_name,
            "method": method,
            "path": path,
            "response_status": int(response.get("_status_code", 200)),
            "data": response.get("data"),
        }

    def _execute_internal_db_query(self, payload: Mapping[str, object]) -> dict[str, object]:
        internal_db_url = self.settings.internal_db_url
        if not internal_db_url:
            raise ToolInvocationError("Internal DB URL is not configured.")

        sql = str(payload.get("sql") or "").strip()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if not sql:
            raise ToolInvocationError("Internal DB payload did not include a SQL query.")

        normalized = sql.lower().lstrip()
        if not (normalized.startswith("select") or normalized.startswith("with")):
            raise ToolInvocationError("Only read-only SELECT or WITH queries are allowed for internal_db.query.")

        stripped = sql.rstrip()
        if ";" in stripped[:-1]:
            raise ToolInvocationError("Multiple SQL statements are not allowed for internal_db.query.")

        engine = create_engine(internal_db_url, future=True)
        try:
            with engine.connect() as connection:
                rows = connection.execute(text(sql), params).mappings().fetchmany(self.settings.internal_db_max_rows)
        except Exception as exc:  # pragma: no cover - driver-specific failures
            raise ToolInvocationError(f"Internal DB query failed: {exc}", retryable=False) from exc
        finally:
            engine.dispose()

        serialized_rows = [dict(row) for row in rows]
        return {
            "status": "completed",
            "tool_name": "internal_db.query",
            "provider": "internal_db",
            "row_count": len(serialized_rows),
            "rows": serialized_rows,
        }

    @staticmethod
    def _request_json(
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        timeout_seconds: float,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            with httpx.Client(timeout=external_http_timeout(timeout_seconds)) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    auth=auth,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ToolInvocationError(
                f"{method} {url} timed out after {timeout_seconds} seconds.",
                retryable=True,
                timed_out=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            response_text = exc.response.text[:600]
            raise ToolInvocationError(
                f"{method} {url} returned HTTP {status_code}: {response_text}",
                retryable=status_code in {408, 409, 425, 429, 500, 502, 503, 504},
                http_status=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolInvocationError(
                f"{method} {url} failed: {exc}",
                retryable=True,
            ) from exc

        try:
            parsed = response.json()
        except ValueError:
            parsed = {"text": response.text[:4000]}

        if isinstance(parsed, dict):
            return {"_status_code": response.status_code, "data": parsed}
        return {"_status_code": response.status_code, "data": parsed}

    @staticmethod
    def _jira_response_data(response: Mapping[str, object]) -> dict[str, object]:
        data = response.get("data")
        if isinstance(data, dict):
            return data
        if "data" not in response:
            return dict(response)
        return {}

    @staticmethod
    def _extract_jira_status_name(response_data: Mapping[str, object]) -> str:
        fields = response_data.get("fields") if isinstance(response_data.get("fields"), dict) else {}
        status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        return str(status.get("name") or "").strip()

    @staticmethod
    def _extract_jira_description(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, dict):
            return ""

        parts: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, str):
                stripped = node.strip()
                if stripped:
                    parts.append(stripped)
                return
            if isinstance(node, dict):
                text_value = node.get("text")
                if isinstance(text_value, str):
                    stripped = text_value.strip()
                    if stripped:
                        parts.append(stripped)
                content = node.get("content")
                if isinstance(content, list):
                    for item in content:
                        walk(item)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(value)
        return "\n".join(parts[:40]).strip()
