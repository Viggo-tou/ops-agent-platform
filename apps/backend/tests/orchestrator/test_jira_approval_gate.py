"""T-039 Jira-transition approval gate tests.

Covers three behaviors:

1. Gate path: after conformance + attestation pass, the develop pipeline
   creates a pending Approval, parks the task in AWAITING_APPROVAL, and
   does NOT invoke ``jira.transition_issue``.
2. Grant path: ``resume_after_approval`` on a develop task flips
   ``pipeline_state.jira_approval_granted`` and re-enters the pipeline.
   Cached pipeline_state (codegen, review, conformance) short-circuits
   the earlier stages; only the Jira transition runs.
3. Reject path: ``ApprovalService.reject`` on a ``jira.transition_issue``
   approval keeps the task COMPLETED (code preserved) with
   ``jira_transitioned=false`` — the old FAILED behavior is reserved for
   non-jira-transition approvals.
"""

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

from app.agents.schemas import (  # noqa: E402
    FinalOutputContract,
    GeneratedPlan,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
)
from app.core.enums import (  # noqa: E402
    ActorRole,
    ApprovalStatus,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="approval-gate-", dir=str(BACKEND_ROOT)))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="approval-gate-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-gate",
        objective='Remove "Minij" anchor.',
        request_summary='Remove "Minij".',
        scenario="jira_issue_develop",
        change_summary="Delete anchor.",
        change_explanation="Anchor must be removed from src/a.py.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[
            PlanCodeLocation(
                source_name="repo",
                relative_path="src/a.py",
                reason="Holds the anchor.",
            )
        ],
        must_touch_files=["src/a.py"],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Modify src/a.py.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Generate patch",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=[],
                tool_name="codegen.generate_patch",
                expected_output="Unified diff.",
                success_criteria="A patch is generated.",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _task() -> SimpleNamespace:
    plan = _plan()
    return SimpleNamespace(
        id="task-gate",
        session_id="session-gate",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text='Remove "Minij" from src/a.py.',
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={
            "issue_key": "TEST-1",
            "normalized_request": 'Delete "Minij" from src/a.py.',
        },
        plan_json=plan.model_dump(mode="json"),
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


MODIFY_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index aaa..bbb 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,1 @@\n"
    '-user = "Minij"\n'
    ' other = "ok"\n'
)


def _codegen_result() -> dict[str, object]:
    return {
        "diff": MODIFY_DIFF,
        "summary": "Removed Minij anchor.",
        "files_changed": ["src/a.py"],
        "provider_name": "mock",
        "model_name": "mock",
    }


class JiraApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = _writable_mkdtemp()
        self.source_tree = self.temp_dir / "source"
        (self.source_tree / "src").mkdir(parents=True)
        (self.source_tree / "src" / "a.py").write_text(
            'user = "Minij"\nother = "ok"\n', encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _orchestrator(self) -> PrimaryOrchestrator:
        orchestrator = PrimaryOrchestrator(db=Mock())
        orchestrator.tool_gateway.settings.sandbox_base_dir = str(self.temp_dir / "sandbox")
        orchestrator.tool_gateway.settings.knowledge_source_path = str(self.source_tree)
        orchestrator.tool_gateway.settings.knowledge_max_file_bytes = 120_000
        orchestrator.tool_gateway.settings.develop_require_jira_approval = True
        orchestrator._sync_retry_count = Mock()
        orchestrator._gather_codegen_context = Mock(
            return_value={"src/a.py": 'user = "Minij"\nother = "ok"\n'}
        )
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        # db.add + db.flush used when creating Approval; make flush a no-op
        orchestrator.db.add = Mock()
        orchestrator.db.flush = Mock()
        return orchestrator

    # --- Gate path --------------------------------------------------------

    def test_gate_parks_task_awaiting_approval_and_skips_transition(self) -> None:
        plan = _plan()
        task = _task()
        orchestrator = self._orchestrator()
        review_pass = {"verdict": "pass", "violations": [], "rules_checked": 4, "duration_ms": 1}
        test_pass = {
            "status": "passed",
            "overall_passed": True,
            "failed_count": 0,
            "passed_count": 1,
            "total_steps": 1,
        }
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(),
                {"status": "patched", "method": "git_apply"},
                test_pass,
                review_pass,
            ]
        )

        added: list[object] = []
        orchestrator.db.add = Mock(side_effect=lambda obj: (added.append(obj), setattr(obj, "id", "approval-gate-1"))[0])

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ):
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        # jira.transition_issue was NOT called
        for call in orchestrator.tool_gateway.execute.call_args_list:
            tool = call.kwargs.get("tool_name") or (call.args[0] if call.args else "")
            self.assertNotEqual(tool, "jira.transition_issue")

        self.assertEqual(task.latest_result_json["status"], TaskStatus.AWAITING_APPROVAL.value)
        self.assertEqual(task.pending_approval, True)
        result_preview = task.latest_result_json["result"]
        self.assertEqual(result_preview["jira_transitioned"], False)
        self.assertIn("goal_attestation", result_preview)
        self.assertIn('-user = "Minij"', result_preview["diff"])

        # Approval was enqueued
        self.assertEqual(len(added), 1)
        approval = added[0]
        self.assertEqual(approval.action_name, "jira.transition_issue")
        self.assertEqual(approval.status, ApprovalStatus.PENDING)
        self.assertEqual(approval.approver_role, ActorRole.TEAM_LEAD.value)
        payload = approval.request_payload_json
        self.assertEqual(payload["stage"], "post_codegen_pre_jira_transition")
        self.assertEqual(payload["issue_key"], "TEST-1")
        self.assertIn('-user = "Minij"', payload["diff"])

        # pipeline_state has pending_jira_approval_id
        self.assertEqual(
            task.latest_result_json["pipeline_state"]["pending_jira_approval_id"],
            "approval-gate-1",
        )

    # --- Grant path -------------------------------------------------------

    def test_resume_after_approval_flips_granted_and_reenters_pipeline(self) -> None:
        """resume_after_approval on a develop task should:
          * Mark pipeline_state.jira_approval_granted = True
          * Clear pending_jira_approval_id
          * Re-enter _execute_develop_pipeline (where cached stages
            short-circuit down to jira writeback).
        We mock _execute_develop_pipeline here to keep the test focused
        on the resume routing; the full-pipeline short-circuit is
        already exercised in test_conformance_retry.py.
        """
        task = _task()
        orchestrator = self._orchestrator()

        primed_state = {
            "codegen_result": _codegen_result(),
            "diff": MODIFY_DIFF,
            "files_changed": ["src/a.py"],
            "pending_jira_approval_id": "approval-gate-1",
        }
        task.latest_result_json = {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "pipeline_state": primed_state,
        }
        task.status = TaskStatus.AWAITING_APPROVAL
        task.workflow_stage = WorkflowStage.REVIEW

        orchestrator._execute_develop_pipeline = Mock()

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ):
            orchestrator.resume_after_approval(
                task=task, actor_name="tester", approval_id="approval-gate-1"
            )

        orchestrator._execute_develop_pipeline.assert_called_once()
        ps = task.latest_result_json["pipeline_state"]
        self.assertTrue(ps.get("jira_approval_granted"))
        self.assertNotIn("pending_jira_approval_id", ps)


class ApprovalServiceRejectTests(unittest.TestCase):
    """Reject-path branching in ApprovalService. Uses a mocked session —
    the goal is to pin the develop-vs-default task-status branch, not
    exercise SA relationship loading."""

    def _build_service_with_pending_approval(
        self, *, action_name: str, scenario: str
    ):
        from app.schemas.approval import ApprovalDecisionRequest  # noqa: F401
        from app.services.approvals import ApprovalService

        task = SimpleNamespace(
            id="task-reject",
            scenario=scenario,
            status=TaskStatus.AWAITING_APPROVAL,
            workflow_stage=WorkflowStage.REVIEW,
            latest_result_json={
                "status": TaskStatus.AWAITING_APPROVAL.value,
                "message": "## Pending\n\nDiff preview.",
                "result": {
                    "diff": MODIFY_DIFF,
                    "files_changed": ["src/a.py"],
                    "goal_attestation": {"all_goals_met": True, "anchors": []},
                },
            },
            pending_approval=True,
        )
        approval = SimpleNamespace(
            id="approval-reject-1",
            task_id=task.id,
            task=task,
            action_name=action_name,
            status=ApprovalStatus.PENDING,
            decided_at=None,
            decided_by_actor_name=None,
            decision_payload_json=None,
        )

        db = Mock()
        db.scalars.return_value.first.return_value = approval
        service = ApprovalService(db)
        service.orchestrator = Mock()
        return service, approval, task

    def test_reject_jira_transition_on_develop_keeps_completed(self) -> None:
        from app.schemas.approval import ApprovalDecisionRequest

        service, approval, task = self._build_service_with_pending_approval(
            action_name="jira.transition_issue", scenario="jira_issue_develop"
        )
        payload = ApprovalDecisionRequest(
            actor_name="lead",
            actor_role=ActorRole.TEAM_LEAD,
            notes="not ready to flip Jira yet",
        )

        with patch("app.services.approvals.record_event"), patch(
            "app.services.approvals.set_task_status"
        ) as set_status:
            service.reject(approval_id=approval.id, payload=payload)

        # ApprovalService ends the task as COMPLETED on jira-transition rejection
        set_calls = set_status.call_args_list
        self.assertTrue(
            any(call.kwargs.get("new_status") == TaskStatus.COMPLETED for call in set_calls),
            f"expected set_task_status(COMPLETED), got {set_calls!r}",
        )
        self.assertFalse(
            any(call.kwargs.get("new_status") == TaskStatus.FAILED for call in set_calls)
        )

        self.assertEqual(approval.status, ApprovalStatus.REJECTED)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.COMPLETED.value)
        preserved = task.latest_result_json["result"]
        self.assertEqual(preserved["jira_transitioned"], False)
        self.assertTrue(preserved["jira_transition_rejected"])
        self.assertEqual(preserved["diff"], MODIFY_DIFF)
        self.assertIn("not ready to flip Jira yet", task.latest_result_json["message"])

    def test_reject_non_jira_transition_still_fails_task(self) -> None:
        from app.schemas.approval import ApprovalDecisionRequest

        service, approval, task = self._build_service_with_pending_approval(
            action_name="other.action", scenario="jira_issue_develop"
        )
        payload = ApprovalDecisionRequest(
            actor_name="lead",
            actor_role=ActorRole.TEAM_LEAD,
            notes="nope",
        )

        with patch("app.services.approvals.record_event"), patch(
            "app.services.approvals.set_task_status"
        ) as set_status:
            service.reject(approval_id=approval.id, payload=payload)

        self.assertTrue(
            any(call.kwargs.get("new_status") == TaskStatus.FAILED for call in set_status.call_args_list)
        )
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)


if __name__ == "__main__":
    unittest.main()
