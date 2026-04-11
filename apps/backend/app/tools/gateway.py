from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import (
    EventSource,
    EventType,
    RoleName,
    ToolExecutionStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.models.tool_execution import ToolExecution
from app.models.base import utcnow
from app.schemas.tool import ToolRegistryEntryRead
from app.services.events import record_event
from app.services.knowledge import KnowledgeService
from app.tools.registry import ToolDefinition, ToolRegistry


class ToolInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.timed_out = timed_out


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
        definition = self.registry.get_definition(tool_name)
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

        total_attempts = definition.retry_count + 1
        started = perf_counter()

        for attempt in range(1, total_attempts + 1):
            attempt_started = perf_counter()
            try:
                result = self._execute_once(
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

    def _execute_once(
        self,
        *,
        definition: ToolDefinition,
        payload: Mapping[str, object],
        actor_context: Mapping[str, object],
    ) -> dict[str, object]:
        if not definition.enabled and definition.name != "knowledge.search":
            raise ToolInvocationError(
                f"{definition.name} is not enabled. Configure the required environment variables before using it."
            )

        if definition.name == "knowledge.search":
            return self._execute_knowledge_search(payload)
        if definition.name == "jira.get_issue":
            return self._execute_jira_get_issue(definition=definition, payload=payload)
        if definition.name == "slack.post_message":
            return self._execute_slack_post_message(definition=definition, payload=payload)
        if definition.name == "jira.create_issue":
            return self._execute_jira_create_issue(definition=definition, payload=payload)
        if definition.name == "internal_api.request":
            return self._execute_internal_api_request(definition=definition, payload=payload)
        if definition.name == "internal_db.query":
            return self._execute_internal_db_query(payload)

        raise ToolInvocationError(f"Unsupported tool: {definition.name}")

    def _execute_knowledge_search(self, payload: Mapping[str, object]) -> dict[str, object]:
        query = str(payload.get("query", "")).strip()
        top_k = payload.get("top_k")
        source_name = payload.get("source_name")
        result = self.knowledge_service.search_repositories(
            query=query,
            top_k=int(top_k) if isinstance(top_k, int) else None,
            source_name=str(source_name) if isinstance(source_name, str) and source_name else None,
        )
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
            with httpx.Client(timeout=timeout_seconds) as client:
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
