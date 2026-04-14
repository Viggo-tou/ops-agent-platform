from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.enums import (  # noqa: E402
    ActorRole,
    ApprovalStatus,
    RiskCategory,
    RiskLevel,
    RoleName,
    ToolExecutionStatus,
    ToolPermissionCategory,
)
from app.models.approval import Approval  # noqa: E402
from app.models.tool_execution import ToolExecution  # noqa: E402
from app.tools.gateway import ToolApprovalRequired, ToolGateway  # noqa: E402


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tool_permission_overrides=None,
        tool_default_timeout_seconds=15.0,
        slack_bot_token=None,
        slack_post_message_timeout_seconds=10.0,
        slack_post_message_retry_count=1,
        jira_base_url="https://example.atlassian.net/some/path",
        jira_email="jira@example.com",
        jira_api_token="secret",
        jira_bearer_token=None,
        jira_project_key="OPS",
        jira_issue_type="Task",
        jira_timeout_seconds=15.0,
        jira_retry_count=1,
        internal_api_base_url=None,
        internal_api_timeout_seconds=10.0,
        internal_api_retry_count=1,
        internal_db_url=None,
        internal_db_timeout_seconds=8.0,
        internal_db_retry_count=0,
    )


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None


class _FakeSession:
    def __init__(self, scalar_rows: list[object] | None = None) -> None:
        self.added: list[object] = []
        self.scalar_rows = scalar_rows or []
        self._tool_execution_count = 0
        self._approval_count = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, ToolExecution) and obj.id is None:
                self._tool_execution_count += 1
                obj.id = f"execution-{self._tool_execution_count}"
            if isinstance(obj, Approval) and obj.id is None:
                self._approval_count += 1
                obj.id = f"approval-{self._approval_count}"

    def scalars(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self.scalar_rows)


class ToolApprovalGateTests(unittest.TestCase):
    def _gateway(self, db: _FakeSession | None = None) -> ToolGateway:
        return ToolGateway(db=db or _FakeSession(), settings=_settings())

    def _execute_success(self, gateway: ToolGateway, **kwargs: object) -> dict[str, object]:
        payload = kwargs.pop("payload", {"query": "test"})
        with patch.object(gateway, "_execute_once", return_value={"status": "ok"}) as execute_once:
            result = gateway.execute(
                task_id="task-1",
                payload=payload,
                actor_context={"actor_name": "alice"},
                session_id="session-1",
                **kwargs,
            )

        execute_once.assert_called_once()
        return result

    def _tool_executions(self, db: _FakeSession) -> list[ToolExecution]:
        return [obj for obj in db.added if isinstance(obj, ToolExecution)]

    def _approvals(self, db: _FakeSession) -> list[Approval]:
        return [obj for obj in db.added if isinstance(obj, Approval)]

    def test_read_only_tool_executes_without_approval(self) -> None:
        db = _FakeSession()
        result = self._execute_success(
            self._gateway(db),
            tool_name="knowledge.search",
            role=RoleName.KNOWLEDGE,
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(self._approvals(db), [])

    def test_approval_required_tool_raises_without_approval_id(self) -> None:
        # internal_api.request is APPROVAL_REQUIRED under the current registry policy.
        # (sandbox.run_command was demoted to WRITE during the auto-approve policy change.)
        db = _FakeSession()
        gateway = self._gateway(db)

        with self.assertRaises(ToolApprovalRequired) as raised:
            gateway.execute(
                task_id="task-1",
                tool_name="internal_api.request",
                payload={"method": "POST", "path": "/v1/ping"},
                actor_context={"actor_name": "alice"},
                session_id="session-1",
                role=RoleName.ACTION,
            )

        self.assertEqual(raised.exception.tool_name, "internal_api.request")
        self.assertEqual(raised.exception.approval_id, "approval-1")

    def test_approval_required_tool_executes_with_approval_id(self) -> None:
        pending_execution = ToolExecution(
            id="execution-1",
            task_id="task-1",
            session_id="session-1",
            approval_id="test-approval",
            tool_name="sandbox.run_command",
            provider_name="sandbox",
            permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
            status=ToolExecutionStatus.PENDING_APPROVAL,
            actor_name="alice",
            attempt_count=0,
            max_retries=0,
            timeout_seconds=15.0,
            request_payload_json={"task_id": "task-1", "command": "pytest"},
            attempt_log_json=[],
        )
        db = _FakeSession(scalar_rows=[pending_execution])

        result = self._execute_success(
            self._gateway(db),
            tool_name="sandbox.run_command",
            payload={"task_id": "task-1", "command": "pytest"},
            role=RoleName.ACTION,
            approval_id="test-approval",
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(pending_execution.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(self._approvals(db), [])

    def test_write_tool_executes_without_approval(self) -> None:
        db = _FakeSession()
        result = self._execute_success(
            self._gateway(db),
            tool_name="jira.create_issue",
            payload={"summary": "Create issue"},
            role=RoleName.ACTION,
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(self._approvals(db), [])

    def test_pending_approval_status_set(self) -> None:
        db = _FakeSession()
        gateway = self._gateway(db)

        with self.assertRaises(ToolApprovalRequired):
            gateway.execute(
                task_id="task-1",
                tool_name="internal_api.request",
                payload={"method": "POST", "path": "/v1/ping"},
                actor_context={"actor_name": "alice"},
                session_id="session-1",
                role=RoleName.ACTION,
            )

        [execution] = self._tool_executions(db)
        self.assertEqual(execution.status, ToolExecutionStatus.PENDING_APPROVAL)
        self.assertEqual(execution.approval_id, "approval-1")

    def test_approval_row_created(self) -> None:
        db = _FakeSession()
        gateway = self._gateway(db)
        payload = {"method": "POST", "path": "/v1/ping"}

        with self.assertRaises(ToolApprovalRequired):
            gateway.execute(
                task_id="task-1",
                tool_name="internal_api.request",
                payload=payload,
                actor_context={"actor_name": "alice"},
                session_id="session-1",
                role=RoleName.ACTION,
            )

        [approval] = self._approvals(db)
        self.assertEqual(approval.task_id, "task-1")
        self.assertEqual(approval.action_name, "internal_api.request")
        self.assertEqual(approval.status, ApprovalStatus.PENDING)
        self.assertEqual(approval.requested_by_role, RoleName.ACTION)
        self.assertEqual(approval.approver_role, ActorRole.TEAM_LEAD.value)
        self.assertEqual(approval.requested_by_actor_name, "alice")
        self.assertEqual(approval.risk_level, RiskLevel.HIGH)
        self.assertEqual(approval.risk_category, RiskCategory.CHANGE_MANAGEMENT)
        self.assertEqual(approval.request_payload_json, payload)


if __name__ == "__main__":
    unittest.main()
