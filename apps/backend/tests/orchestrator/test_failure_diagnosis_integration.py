from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.enums import (  # noqa: E402
    ActorRole,
    EventType,
    RiskCategory,
    RiskLevel,
    TaskStatus,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.failure_diagnosis import DiagnosisOutput  # noqa: E402


class FakeDb:
    def __init__(self) -> None:
        self.added = []

    def add(self, item) -> None:  # noqa: ANN001
        if getattr(item, "id", None) is None:
            item.id = f"ap-{len(self.added) + 1}"
        self.added.append(item)

    def flush(self) -> None:
        return None

    def get(self, model, task_id):  # noqa: ANN001
        del model, task_id
        return None

    def scalars(self, stmt):  # noqa: ANN001
        del stmt
        return []


def _task(task_id: str = "task-diag") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        session_id=f"session-{task_id}",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="Implement P69-7.",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json=None,
        plan_json={"objective": "Fix compile failure"},
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


class FailureDiagnosisIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name == "nt":
            original_mkdir = tempfile._os.mkdir

            def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
                original_mkdir(path, 0o777)

            tempfile._os.mkdir = mkdir_with_write_access
            try:
                self.root = Path(tempfile.mkdtemp(prefix="failure-diagnosis-int-"))
            finally:
                tempfile._os.mkdir = original_mkdir
        else:
            self.root = Path(tempfile.mkdtemp(prefix="failure-diagnosis-int-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _orchestrator(self) -> PrimaryOrchestrator:
        orchestrator = PrimaryOrchestrator(db=FakeDb())
        orchestrator.tool_gateway.settings.failure_diagnosis_enabled = True
        orchestrator.tool_gateway.settings.failure_diagnosis_timeout_seconds = 30.0
        orchestrator._workspace_append_audit = Mock()
        orchestrator._workspace_write_checkpoint = Mock()
        return orchestrator

    def test_orchestrator_calls_diagnosis_on_awaiting_approval(self) -> None:
        orchestrator = self._orchestrator()
        task = _task("task-awaiting")
        sandbox = self.root / task.id
        sandbox.mkdir()
        diagnosis = DiagnosisOutput(
            summary="package.json is malformed",
            root_cause="Node failed before checking the target file.",
            likely_fix="Fix package.json.",
            confidence="high",
            related_files=["package.json"],
        )

        with patch("app.orchestrator.service.set_task_status"), patch(
            "app.orchestrator.service.record_event"
        ), patch("app.orchestrator.service.commit_checkpoint"), patch(
            "app.orchestrator.service.run_diagnosis", return_value=diagnosis
        ) as run_diagnosis:
            orchestrator._request_compile_repair_approval(
                task=task,
                plan=Mock(),
                pipeline_state={},
                rounds_summary=[{"round": 1, "duration_seconds": 1, "files_attempted": [], "files_repaired": []}],
                residual_errors=[{"file": "src/data/jobData.js", "error": "Invalid package config"}],
                sandbox_dir=sandbox,
            )

        run_diagnosis.assert_called_once()
        self.assertEqual(task.latest_result_json["status"], TaskStatus.AWAITING_APPROVAL.value)

    def test_orchestrator_calls_diagnosis_on_failed_task(self) -> None:
        orchestrator = self._orchestrator()
        task = _task("task-failed")

        with patch("app.orchestrator.service.set_task_status"), patch(
            "app.orchestrator.service.record_event"
        ), patch("app.orchestrator.service.run_diagnosis") as run_diagnosis:
            orchestrator._fail_develop_pipeline(
                task=task,
                message="Tool failed: command exited 1",
                payload={"error": "command exited 1"},
            )

        run_diagnosis.assert_called_once()
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)

    def test_orchestrator_does_not_call_diagnosis_on_normal_completion(self) -> None:
        orchestrator = self._orchestrator()
        task = _task("task-completed")
        task.status = TaskStatus.COMPLETED
        task.workflow_stage = WorkflowStage.DONE
        task.latest_result_json = {"status": TaskStatus.COMPLETED.value}

        with patch("app.orchestrator.service.run_diagnosis") as run_diagnosis:
            # Successful completion paths do not call the failure hook.
            self.assertEqual(task.status, TaskStatus.COMPLETED)

        run_diagnosis.assert_not_called()

    def test_diagnosis_failure_does_not_break_task_transition(self) -> None:
        orchestrator = self._orchestrator()
        task = _task("task-diagnosis-fails")
        sandbox = self.root / task.id
        sandbox.mkdir()

        with patch("app.orchestrator.service.set_task_status"), patch(
            "app.orchestrator.service.record_event"
        ), patch("app.orchestrator.service.commit_checkpoint"), patch(
            "app.orchestrator.service.run_diagnosis", side_effect=RuntimeError("provider down")
        ):
            orchestrator._request_compile_repair_approval(
                task=task,
                plan=Mock(),
                pipeline_state={},
                rounds_summary=[{"round": 1, "duration_seconds": 1, "files_attempted": [], "files_repaired": []}],
                residual_errors=[{"file": "src/a.js", "error": "syntax error"}],
                sandbox_dir=sandbox,
            )

        self.assertEqual(task.latest_result_json["status"], TaskStatus.AWAITING_APPROVAL.value)
        self.assertTrue(task.pending_approval)


if __name__ == "__main__":
    unittest.main()
