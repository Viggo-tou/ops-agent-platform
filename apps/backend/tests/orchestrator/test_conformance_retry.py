"""Retry-with-feedback test for the spec-conformance gate (T-038-A).

Exercises the full shape: first codegen produces a shadow-implementation
diff (all new files, nothing modified) for a request that contains a
destructive verb and a quoted anchor that exists in the source tree. The
gate blocks, `_reset_for_conformance_retry` wipes pipeline state + the
on-disk sandbox, and the pipeline recurses into a second codegen call
that actually modifies the anchor-bearing file. The second pass passes
the gate and Jira transitions.

Verifies:
  * Two codegen tool-gateway calls happened (not one).
  * The second codegen call's ``task_description`` carries the RETRY
    FEEDBACK directive populated from the first attempt's block message.
  * ``pipeline_state.conformance_attempts`` ends at 1 (reset bump only,
    the second pass passes so the counter is not further incremented).
  * Final verdict is COMPLETED and goal_attestation records the anchor
    as achieved.
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
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="conformance-retry-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="conformance-retry-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan_with_must_touch() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-retry",
        objective="Remove Minij anchor from legacy module.",
        request_summary='Remove "Minij" from the legacy module.',
        scenario="jira_issue_develop",
        change_summary="Delete legacy Minij reference.",
        change_explanation="The anchor must be removed from src/a.py.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[
            PlanCodeLocation(
                source_name="repo",
                relative_path="src/a.py",
                reason="Holds the Minij anchor.",
            )
        ],
        must_touch_files=["src/a.py"],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Modify src/a.py to drop the Minij anchor.",
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


def _task_for_retry() -> SimpleNamespace:
    plan = _plan_with_must_touch()
    return SimpleNamespace(
        id="task-retry",
        session_id="session-retry",
        actor_name="tester",
        request_text='Remove "Minij" from the legacy module.',
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={
            "issue_key": "TEST-1",
            "normalized_request": 'Delete "Minij" anchor from src/a.py.',
        },
        plan_json=plan.model_dump(mode="json"),
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


SHADOW_DIFF = (
    "diff --git a/src/clean/new_module.py b/src/clean/new_module.py\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/src/clean/new_module.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+# clean rewrite\n"
    "+value = 1\n"
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


def _codegen_result(diff: str, files_changed: list[str]) -> dict[str, object]:
    return {
        "diff": diff,
        "summary": "mock codegen",
        "files_changed": files_changed,
        "provider_name": "mock",
        "model_name": "mock",
    }


class ConformanceRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = _writable_mkdtemp()
        # seed a source tree the gate will scan
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
        orchestrator.tool_gateway.settings.develop_require_jira_approval = False
        orchestrator._sync_retry_count = Mock()
        orchestrator._gather_codegen_context = Mock(
            return_value={"src/a.py": 'user = "Minij"\nother = "ok"\n'}
        )
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        return orchestrator

    def test_conformance_block_triggers_retry_and_passes(self) -> None:
        plan = _plan_with_must_touch()
        task = _task_for_retry()
        orchestrator = self._orchestrator()

        review_pass = {
            "verdict": "pass",
            "violations": [],
            "rules_checked": 4,
            "duration_ms": 1,
        }
        test_pass = {
            "status": "passed",
            "overall_passed": True,
            "failed_count": 0,
            "passed_count": 1,
            "total_steps": 1,
        }
        # Sequence across both pipeline passes:
        #   pass 1: codegen (shadow), apply, test, review_pass → gate blocks → recurse
        #   pass 2: codegen (modify), apply, test, review_pass → gate passes → jira
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(SHADOW_DIFF, ["src/clean/new_module.py"]),
                {"status": "patched", "method": "git_apply"},
                test_pass,
                review_pass,
                _codegen_result(MODIFY_DIFF, ["src/a.py"]),
                {"status": "patched", "method": "git_apply"},
                test_pass,
                review_pass,
                {"status": "transitioned", "issue_key": "TEST-1"},
            ]
        )

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ):
            orchestrator._execute_develop_pipeline(
                task=task, actor_name="tester", plan=plan
            )

        # Two codegen calls happened (first shadow, then modify).
        codegen_calls = [
            call
            for call in orchestrator.tool_gateway.execute.call_args_list
            if call.kwargs.get("tool_name") == "codegen.generate_patch"
            or (call.args and call.args[0] == "codegen.generate_patch")
        ]
        self.assertEqual(len(codegen_calls), 2, "expected two codegen passes")

        # Second codegen must carry the RETRY FEEDBACK directive.
        second_call = codegen_calls[1]
        payload = (
            second_call.kwargs.get("payload")
            or (second_call.args[1] if len(second_call.args) > 1 else {})
        )
        task_description = (
            payload.get("task_description")
            if isinstance(payload, dict)
            else ""
        )
        self.assertIn("RETRY FEEDBACK", task_description)

        # Final result: completed, conformance passed, attestation recorded.
        self.assertEqual(
            task.latest_result_json["status"], TaskStatus.COMPLETED.value
        )
        self.assertEqual(
            task.latest_result_json["pipeline_state"]["conformance_attempts"], 1
        )
        attestation = task.latest_result_json["result"]["goal_attestation"]
        self.assertIsNotNone(attestation)
        minij = next(
            entry for entry in attestation["anchors"] if entry["anchor"] == "Minij"
        )
        self.assertEqual(minij["status"], "achieved")
        self.assertEqual(minij["count_after"], 0)
        self.assertIn("src/a.py", minij["files_modified"])


if __name__ == "__main__":
    unittest.main()
