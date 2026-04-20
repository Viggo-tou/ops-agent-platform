from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

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
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.runtime_validation import (  # noqa: E402
    ValidationFinding,
    ValidationReport,
)


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="gate-repair-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="gate-repair-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


CONTEXT_FILES = {
    "src/a.js": 'const role = "Master Admin";\n',
    "src/b.js": 'const role = "Master Admin";\n',
    "src/c.js": 'const role = "Master Admin";\n',
}

INITIAL_DIFF = (
    "diff --git a/src/a.js b/src/a.js\n"
    "--- a/src/a.js\n"
    "+++ b/src/a.js\n"
    "@@ -1 +1 @@\n"
    '-const role = "Master Admin";\n'
    '+const role = "Admin";\n'
)

OLD_B_DIFF = (
    "diff --git a/src/b.js b/src/b.js\n"
    "--- a/src/b.js\n"
    "+++ b/src/b.js\n"
    "@@ -1 +1 @@\n"
    '-const role = "Master Admin";\n'
    '+const role = "Broken Admin";\n'
)

REPAIR_B_DIFF = (
    "diff --git a/src/b.js b/src/b.js\n"
    "--- a/src/b.js\n"
    "+++ b/src/b.js\n"
    "@@ -1 +1 @@\n"
    '-const role = "Master Admin";\n'
    '+const role = "Admin";\n'
)


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-repair",
        objective="Update role labels.",
        request_summary="Update role labels.",
        scenario="jira_issue_develop",
        change_summary="Update role label usage.",
        change_explanation="Replace legacy role labels in source files.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[
            PlanCodeLocation(
                source_name="repo",
                relative_path="src/a.js",
                reason="Initial changed source file.",
            ),
            PlanCodeLocation(
                source_name="repo",
                relative_path="src/b.js",
                reason="Runtime validation repair target.",
            ),
        ],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate source diff.",
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


def _task(plan: GeneratedPlan, *, state: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-repair",
        session_id="session-repair",
        actor_name="tester",
        actor_role=ActorRole.ADMIN,
        request_text="Update role labels.",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={"issue_key": None, "normalized_request": "Update role labels."},
        plan_json=plan.model_dump(mode="json"),
        latest_result_json={"pipeline_state": state or {}},
        pending_approval=False,
        retry_count=0,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskLevel.MEDIUM,
    )


def _codegen_result(diff: str, files_changed: list[str]) -> dict[str, object]:
    return {
        "diff": diff,
        "summary": "mock codegen",
        "files_changed": files_changed,
        "provider_name": "mock",
        "model_name": "mock",
    }


def _primed_state(diff: str = INITIAL_DIFF) -> dict[str, object]:
    return {
        "evidence_bundle_done": True,
        "codegen_result": _codegen_result(diff, ["src/a.js"]),
        "diff": diff,
        "files_changed": ["src/a.js"],
        "sandbox_result": {"status": "patched", "method": "git_apply"},
        "patch_method": "git_apply",
        "completeness_check": {
            "complete": True,
            "remaining_files": 0,
            "remaining_hits": 0,
        },
        "retry_done": True,
        "test_result": {
            "status": "passed",
            "overall_passed": True,
            "failed_count": 0,
            "passed_count": 1,
            "total_steps": 1,
        },
        "test_skipped": False,
        "diff_shape_done": True,
        "compile_gate_done": True,
        "semantic_review_done": True,
        "review_result": {
            "verdict": "pass",
            "violations": [],
            "rules_checked": 4,
            "duration_ms": 1,
        },
        "review_verdict": "pass",
        "failing_test_gate_done": True,
        "goal_decomp_done": True,
        "symbol_ref_done": True,
        "evidence_chain_validated": True,
    }


def _runtime_fail(file_path: str = "src/b.js") -> ValidationReport:
    return ValidationReport(
        passed=False,
        findings=[
            ValidationFinding(
                file=file_path,
                line=None,
                severity="block",
                rule="incomplete_replacement",
                message='String "Master Admin" still appears in this file.',
            )
        ],
    )


def _runtime_pass() -> ValidationReport:
    return ValidationReport(passed=True, findings=[])


class GateRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = _writable_mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _orchestrator(self) -> PrimaryOrchestrator:
        orchestrator = PrimaryOrchestrator(db=Mock())
        orchestrator.tool_gateway.settings.sandbox_base_dir = str(self.temp_dir / "sandbox")
        orchestrator.tool_gateway.settings.knowledge_source_path = None
        orchestrator.tool_gateway.settings.knowledge_max_file_bytes = 120_000
        orchestrator.tool_gateway.settings.develop_require_jira_approval = False
        orchestrator.tool_gateway.settings.gate_repair_max_attempts = 1
        orchestrator.tool_gateway.settings.gate_repair_timeout_seconds = 300.0
        orchestrator.tool_gateway.settings.claude_code_command = "npx"
        orchestrator.tool_gateway.settings.claude_code_args = "--yes @anthropic-ai/claude-code"
        orchestrator.tool_gateway.settings.claude_code_git_bash_path = None
        orchestrator._sync_retry_count = Mock()
        orchestrator._gather_codegen_context = Mock(return_value=dict(CONTEXT_FILES))
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        return orchestrator

    def test_runtime_validation_failure_triggers_repair_and_pipeline_continues(self) -> None:
        plan = _plan()
        repaired_diff = INITIAL_DIFF + "\n" + REPAIR_B_DIFF
        task = _task(plan, state=_primed_state())
        orchestrator = self._orchestrator()

        with patch("app.orchestrator.service.record_event") as record, patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            side_effect=[_runtime_fail(), _runtime_pass()],
        ), patch.object(
            orchestrator, "_run_targeted_repair", return_value=repaired_diff,
        ) as mock_repair:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(task.latest_result_json["status"], TaskStatus.COMPLETED.value)
        mock_repair.assert_called_once()
        repair_kwargs = mock_repair.call_args.kwargs
        self.assertEqual(repair_kwargs["gate_name"], "runtime_validation")
        self.assertEqual(repair_kwargs["failing_files"], ["src/b.js"])

        repair_events = [
            call for call in record.call_args_list
            if call.kwargs.get("tool_name") == "runtime_validation.repair"
        ]
        self.assertTrue(repair_events)

    def test_repair_codegen_failure_fails_pipeline_gracefully(self) -> None:
        plan = _plan()
        task = _task(plan, state=_primed_state())
        orchestrator = self._orchestrator()

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            return_value=_runtime_fail(),
        ), patch.object(
            orchestrator, "_run_targeted_repair", return_value=None,
        ) as mock_repair:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)
        self.assertIn("Runtime validation", task.latest_result_json["message"])
        mock_repair.assert_called_once()

    def test_repair_diff_replaces_old_target_hunks_and_preserves_other_hunks(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        pipeline_state = {
            "context_files": dict(CONTEXT_FILES),
            "diff": INITIAL_DIFF + "\n" + OLD_B_DIFF,
            "files_changed": ["src/a.js", "src/b.js"],
            "codegen_result": _codegen_result(INITIAL_DIFF + "\n" + OLD_B_DIFF, ["src/a.js", "src/b.js"]),
        }
        # Mock sandbox.apply_patch (still goes through tool_gateway)
        orchestrator.tool_gateway.execute = Mock(
            return_value={"status": "patched", "method": "git_apply"},
        )

        # Mock the Claude CLI subprocess to return REPAIR_B_DIFF
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (REPAIR_B_DIFF, "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with patch("app.orchestrator.service.record_event"), \
             patch("shutil.which", return_value="/fake/claude"), \
             patch("subprocess.Popen", return_value=mock_proc):
            merged = orchestrator._run_targeted_repair(
                task=task,
                actor_name="tester",
                plan=plan,
                pipeline_state=pipeline_state,
                repair_prompt="Repair src/b.js",
                failing_files=["src/b.js"],
                approval_id=None,
                gate_name="runtime_validation",
            )

        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertIn("diff --git a/src/a.js b/src/a.js", merged)
        self.assertIn("diff --git a/src/b.js b/src/b.js", merged)
        self.assertIn('+const role = "Admin";', merged)
        self.assertNotIn("Broken Admin", merged)
        self.assertEqual(pipeline_state["files_changed"], ["src/a.js", "src/b.js"])
        self.assertEqual(pipeline_state["patch_method"], "git_apply")

    def test_max_repair_attempts_respected(self) -> None:
        plan = _plan()
        task = _task(plan, state=_primed_state())
        orchestrator = self._orchestrator()
        repaired_diff = INITIAL_DIFF + "\n" + REPAIR_B_DIFF
        orchestrator._run_targeted_repair = Mock(return_value=repaired_diff)

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            side_effect=[_runtime_fail(), _runtime_fail()],
        ):
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        orchestrator._run_targeted_repair.assert_called_once()
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)
        self.assertEqual(len(task.latest_result_json["findings"]), 1)

    def test_repair_disabled_by_zero_attempts(self) -> None:
        plan = _plan()
        task = _task(plan, state=_primed_state())
        orchestrator = self._orchestrator()
        orchestrator.tool_gateway.settings.gate_repair_max_attempts = 0
        orchestrator._run_targeted_repair = Mock()

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            return_value=_runtime_fail(),
        ):
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        orchestrator._run_targeted_repair.assert_not_called()
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)


if __name__ == "__main__":
    unittest.main()
