from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.core.enums import (  # noqa: E402
    ActorRole,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolExecutionStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.models.base import Base  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.tool_execution import ToolExecution  # noqa: E402
from app.services.rollback import RollbackExecutor, RollbackStepResult  # noqa: E402
from app.tools.gateway import ToolGateway  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="rollback-test-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    # Python's tempfile uses 0o700, which is not writable in this Windows sandbox.
    tempfile._os.mkdir = mkdir_with_write_access
    try:
        candidate_roots = []
        if os.environ.get("OPS_AGENT_TEST_SANDBOX_ROOT"):
            candidate_roots.append(Path(os.environ["OPS_AGENT_TEST_SANDBOX_ROOT"]))
        candidate_roots.extend([Path(tempfile.gettempdir()), Path.home() / ".ops-agent-rollback-tests", BACKEND_ROOT])

        for root in candidate_roots:
            try:
                str(root).encode("ascii")
            except UnicodeEncodeError:
                continue
            try:
                root.mkdir(parents=True, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="rollback-test-", dir=str(root)))
            except OSError:
                continue

        return Path(tempfile.mkdtemp(prefix="rollback-test-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class RollbackExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        self.db = session_factory()
        self.temp_dir = _writable_mkdtemp()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_task(self, task_id: str = "task-1") -> Task:
        task = Task(
            id=task_id,
            session_id="session-1",
            actor_name="tester",
            actor_role=ActorRole.EMPLOYEE,
            title="Rollback test",
            request_text="Rollback test",
            scenario="test",
            status=TaskStatus.COMPLETED,
            workflow_stage=WorkflowStage.DONE,
            current_role=RoleName.SYSTEM,
            risk_level=RiskLevel.LOW,
            risk_category=RiskCategory.GENERAL,
            pending_approval=False,
            governance_json={},
        )
        self.db.add(task)
        self.db.flush()
        return task

    def _add_execution(
        self,
        *,
        task_id: str = "task-1",
        execution_id: str = "execution-1",
        tool_name: str = "sandbox.apply_patch",
        inverse_action_json: dict[str, object] | None = None,
        started_at: datetime | None = None,
    ) -> ToolExecution:
        execution = ToolExecution(
            id=execution_id,
            task_id=task_id,
            session_id="session-1",
            approval_id=None,
            tool_name=tool_name,
            provider_name="test",
            permission_category=ToolPermissionCategory.WRITE,
            status=ToolExecutionStatus.SUCCEEDED,
            actor_name="tester",
            attempt_count=1,
            max_retries=0,
            timeout_seconds=15.0,
            duration_ms=10,
            request_payload_json={},
            response_payload_json={},
            inverse_action_json=inverse_action_json,
            attempt_log_json=[],
            started_at=started_at or datetime.now(timezone.utc),
            finished_at=started_at or datetime.now(timezone.utc),
        )
        self.db.add(execution)
        self.db.flush()
        return execution

    def _repo_with_two_commits(self) -> tuple[Path, str, str]:
        repo_dir = self.temp_dir / "repo"
        repo_dir.mkdir()
        _git(repo_dir, "init")
        _git(repo_dir, "config", "user.email", "rollback-tests@example.com")
        _git(repo_dir, "config", "user.name", "Rollback Tests")
        (repo_dir / "message.txt").write_text("before\n", encoding="utf-8")
        _git(repo_dir, "add", "message.txt")
        _git(repo_dir, "commit", "-m", "Initial commit")
        before_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()

        (repo_dir / "message.txt").write_text("after\n", encoding="utf-8")
        _git(repo_dir, "add", "message.txt")
        _git(repo_dir, "commit", "-m", "Mutating commit")
        after_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        return repo_dir, before_sha, after_sha

    def test_rollback_git_revert(self) -> None:
        self._add_task()
        repo_dir, before_sha, after_sha = self._repo_with_two_commits()
        self.assertNotEqual(before_sha, after_sha)
        self._add_execution(
            inverse_action_json={
                "type": "git_revert",
                "sandbox_dir": str(repo_dir),
                "before_sha": before_sha,
            }
        )
        self.db.commit()

        result = RollbackExecutor(self.db).execute_rollback("task-1")

        head = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head, before_sha)
        self.assertTrue(result.all_succeeded)
        self.assertEqual(result.total_steps, 1)
        self.assertEqual(result.succeeded_count, 1)

    def test_rollback_jira_transition_placeholder(self) -> None:
        self._add_task()
        self._add_execution(
            tool_name="jira.transition_issue",
            inverse_action_json={
                "type": "jira_transition",
                "issue_key": "OPS-123",
                "from_status": "In Progress",
                "to_status": "To Do",
            },
        )
        self.db.commit()

        result = RollbackExecutor(self.db).execute_rollback("task-1")

        self.assertTrue(result.all_succeeded)
        self.assertTrue(result.steps[0].success)
        self.assertIn("placeholder", result.steps[0].message.lower())

    def test_rollback_jira_comment_placeholder(self) -> None:
        self._add_task()
        self._add_execution(
            tool_name="jira.add_comment",
            inverse_action_json={
                "type": "jira_delete_comment",
                "issue_key": "OPS-123",
                "comment_id": "10001",
            },
        )
        self.db.commit()

        result = RollbackExecutor(self.db).execute_rollback("task-1")

        self.assertTrue(result.all_succeeded)
        self.assertTrue(result.steps[0].success)
        self.assertIn("placeholder", result.steps[0].message.lower())

    def test_rollback_no_inverse_skipped(self) -> None:
        self._add_task()
        self._add_execution(inverse_action_json=None)
        self.db.commit()

        result = RollbackExecutor(self.db).execute_rollback("task-1")

        self.assertTrue(result.all_succeeded)
        self.assertEqual(result.total_steps, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.steps, [])

    def test_rollback_multiple_in_reverse_order(self) -> None:
        class RecordingRollbackExecutor(RollbackExecutor):
            def _execute_inverse(  # type: ignore[override]
                self,
                inverse: dict[str, object],
            ) -> RollbackStepResult:
                execution_id = str(inverse["_execution_id"])
                tool_name = str(inverse["_tool_name"])
                seen.append(execution_id)
                return RollbackStepResult(
                    execution_id=execution_id,
                    tool_name=tool_name,
                    inverse_type=str(inverse["type"]),
                    success=True,
                    message="recorded",
                )

        seen: list[str] = []
        self._add_task()
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._add_execution(
            execution_id="execution-a",
            inverse_action_json={"type": "jira_transition"},
            started_at=base_time,
        )
        self._add_execution(
            execution_id="execution-b",
            inverse_action_json={"type": "jira_transition"},
            started_at=base_time + timedelta(seconds=1),
        )
        self.db.commit()

        result = RecordingRollbackExecutor(self.db).execute_rollback("task-1")

        self.assertEqual(seen, ["execution-b", "execution-a"])
        self.assertEqual(result.total_steps, 2)
        self.assertTrue(result.all_succeeded)

    def test_rollback_empty_task(self) -> None:
        self._add_task()
        self.db.commit()

        result = RollbackExecutor(self.db).execute_rollback("task-1")

        self.assertTrue(result.all_succeeded)
        self.assertEqual(result.total_steps, 0)
        self.assertEqual(result.skipped_count, 0)

    def test_build_inverse_action_sandbox_apply_patch(self) -> None:
        inverse = ToolGateway._build_inverse_action(
            "sandbox.apply_patch",
            {"task_id": "task-1"},
            {
                "sandbox_dir": "data/sandboxes/task-1",
                "before_sha": "abc1234",
                "after_sha": "def5678",
            },
        )

        self.assertEqual(
            inverse,
            {
                "type": "git_revert",
                "sandbox_dir": "data/sandboxes/task-1",
                "before_sha": "abc1234",
            },
        )

    def test_build_inverse_action_read_only_returns_none(self) -> None:
        inverse = ToolGateway._build_inverse_action(
            "diff_reviewer.review",
            {"diff": "diff --git a/a b/a"},
            {"verdict": "pass"},
        )

        self.assertIsNone(inverse)


if __name__ == "__main__":
    unittest.main()
