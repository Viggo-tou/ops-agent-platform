from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import (  # noqa: E402
    FinalOutputContract,
    GeneratedPlan,
    GeneratedSemanticTranslation,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
)
from app.core.enums import ActorRole, EventType, RiskLevel, RoleName, TaskStatus, ToolPermissionCategory, WorkflowStage  # noqa: E402
from app.orchestrator.service import PrimaryOrchestrator, classify_request  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="develop-pipeline-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="develop-pipeline-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan(path: str = "app/example.py") -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-1",
        objective="Implement OPS-123.",
        request_summary="Implement OPS-123.",
        scenario="jira_issue_develop",
        change_summary="Update example module.",
        change_explanation="Update the affected source file.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        approval_reasons=["Code changes require approval."],
        affected_code_locations=[
            PlanCodeLocation(
                source_name="repo",
                relative_path=path,
                reason="Target source file.",
            )
        ],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
                purpose="Generate the implementation diff.",
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


def _semantic_translation(issue_key: str | None = "OPS-123") -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id="task-1",
        provider={"name": "test"},
        normalized_request="implement P69-10",
        intent="develop_jira_issue",
        work_type="feature",
        objective="Implement P69-10.",
        issue_key=issue_key,
        issue_url=None,
        candidate_modules=[],
        search_queries=[],
        constraints=[],
        requested_outputs=["jira_issue_develop"],
        grounding_terms=[],
        missing_information=[],
        confidence=0.9,
    )


def _task(plan: GeneratedPlan) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        session_id="session-1",
        actor_name="tester",
        actor_role=ActorRole.ADMIN,
        request_text="\u628a OPS-123 \u505a\u4e86",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={"issue_key": "OPS-123"},
        plan_json=plan.model_dump(mode="json"),
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskLevel.MEDIUM,
    )


def _codegen_result() -> dict[str, object]:
    return {
        "diff": "diff --git a/app/example.py b/app/example.py\n--- a/app/example.py\n+++ b/app/example.py\n@@ -1 +1,2 @@\n old\n+new\n",
        "summary": "Updated app/example.py.",
        "files_changed": ["app/example.py"],
        "provider_name": "mock",
        "model_name": "mock",
    }


class DevelopPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = _writable_mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _orchestrator(self) -> PrimaryOrchestrator:
        orchestrator = PrimaryOrchestrator(db=Mock())
        orchestrator.tool_gateway.settings.sandbox_base_dir = str(self.temp_dir)
        orchestrator.tool_gateway.settings.knowledge_source_path = None
        orchestrator.tool_gateway.settings.knowledge_max_file_bytes = 120_000
        orchestrator.tool_gateway.settings.develop_require_jira_approval = False
        orchestrator.tool_gateway.settings.evidence_chain_gate_enabled = False
        # Legacy single-round compile-repair behaviour for these contract tests.
        # Multi-round behaviour is exercised in test_compile_repair_loop.
        orchestrator.tool_gateway.settings.codegen_max_repair_rounds = 1
        orchestrator._sync_retry_count = Mock()
        return orchestrator

    def test_classify_request_develop(self) -> None:
        self.assertEqual(classify_request("\u628a OPS-123 \u505a\u4e86"), "jira_issue_develop")
        self.assertEqual(classify_request("implement OPS-123"), "jira_issue_develop")
        self.assertEqual(classify_request("fix OPS-123"), "jira_issue_develop")

    def test_classify_request_plan_not_develop(self) -> None:
        self.assertEqual(classify_request("plan OPS-123"), "jira_issue_plan")

    def test_jira_develop_fallback_extraction(self) -> None:
        plan = _plan()
        task = SimpleNamespace(
            id="task-1",
            session_id="session-1",
            actor_name="tester",
            request_text="implement P69-10",
            scenario="jira_issue_develop",
            status=TaskStatus.QUEUED,
            workflow_stage=WorkflowStage.INTAKE,
            translation_json=None,
            plan_json=None,
            latest_result_json=None,
            pending_approval=False,
            retry_count=0,
        )
        orchestrator = self._orchestrator()
        orchestrator._translate_request = Mock(
            side_effect=[
                _semantic_translation(issue_key=None),
                _semantic_translation(issue_key=None),
            ]
        )
        orchestrator._prefetch_jira_issue_context = Mock(
            return_value={"issue_key": "P69-10", "summary": "Implement feature.", "issue_status": "To Do"}
        )
        orchestrator._prefetch_planning_repository_context = Mock(return_value=None)
        orchestrator.primary_agent.generate_plan = Mock(
            return_value=SimpleNamespace(
                plan=plan,
                provider_name="mock",
                model_name="mock",
                used_fallback=False,
                fallback_reason=None,
            )
        )
        orchestrator.reviewer_agent.review_plan = Mock(
            return_value=SimpleNamespace(
                review=SimpleNamespace(
                    verdict="rejected",
                    summary="stop after fallback assertion",
                    review_id="review-1",
                    model_dump=Mock(return_value={"verdict": "rejected"}),
                )
            )
        )

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._bootstrap_task_impl(task=task, actor_name="tester")

        orchestrator._prefetch_jira_issue_context.assert_called_once_with(
            task=task,
            actor_name="tester",
            issue_key="P69-10",
        )
        self.assertEqual(task.translation_json["issue_key"], "P69-10")
        self.assertNotEqual(
            task.latest_result_json.get("message"),
            "No Jira issue key was found in the planning request.",
        )

    def test_jira_error_classification_kinds(self) -> None:
        """Each known HTTP failure mode maps to a distinct error_kind + user message."""
        from app.tools.gateway import ToolInvocationError

        cases = [
            (ToolInvocationError("401", http_status=401), "auth_expired", "expired or been revoked"),
            (ToolInvocationError("403", http_status=403), "permission_denied", "lacks permission"),
            (ToolInvocationError("404", http_status=404), "not_found_or_invisible", "may be deleted"),
            (ToolInvocationError("500", http_status=500), "transient_server_error", "transient"),
            (ToolInvocationError("502", http_status=502), "transient_server_error", "transient"),
            (ToolInvocationError("429", http_status=429), "rate_limited", "rate-limited"),
            (ToolInvocationError("timeout", timed_out=True), "transient_timeout", "timed out"),
            (ToolInvocationError("garbage"), "unknown", "Failed to load"),
        ]
        for exc, expected_kind, expected_substring in cases:
            kind, message = PrimaryOrchestrator._classify_jira_error(exc, "P69-7")
            self.assertEqual(kind, expected_kind, f"exc={exc} status={getattr(exc, 'http_status', None)}")
            self.assertIn(expected_substring, message, f"kind={kind} message={message!r}")

    def test_probe_upgrades_404_to_auth_expired_when_token_dead(self) -> None:
        """When issue lookup fails 404 AND /myself returns 401, surface auth_expired.

        Without the probe, P69-7's real failure mode (token expired, but
        per-issue endpoint returns 404 because Jira hides issue existence
        from unauthenticated callers) gets misreported as
        not_found_or_invisible — wrong remediation in the user message.
        """
        from app.tools.gateway import ToolInvocationError

        task = SimpleNamespace(
            id="task-probe",
            session_id="session-probe",
            actor_name="tester",
            request_text="完成Jira上的P69-7",
            scenario="jira_issue_develop",
            status=TaskStatus.QUEUED,
            workflow_stage=WorkflowStage.INTAKE,
            translation_json=None,
            plan_json=None,
            latest_result_json=None,
            pending_approval=False,
            retry_count=0,
        )
        orchestrator = self._orchestrator()
        orchestrator.tool_gateway.execute = Mock(
            side_effect=ToolInvocationError("404 not found", http_status=404)
        )
        # Probe says token is dead.
        orchestrator._probe_jira_auth_health = Mock(return_value=401)

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            result = orchestrator._prefetch_jira_issue_context(
                task=task,
                actor_name="tester",
                issue_key="P69-7",
            )

        self.assertIsNone(result)
        # The probe was consulted because the initial classification was 404.
        orchestrator._probe_jira_auth_health.assert_called_once()
        # Final error_kind upgraded to auth_expired.
        self.assertEqual(task.latest_result_json["error_kind"], "auth_expired")
        self.assertIn("expired or been revoked", task.latest_result_json["message"])

    def test_probe_keeps_404_when_token_healthy(self) -> None:
        """If /myself returns 200, the original 404 is genuine — don't upgrade."""
        from app.tools.gateway import ToolInvocationError

        task = SimpleNamespace(
            id="task-probe-2",
            session_id="session-probe-2",
            actor_name="tester",
            request_text="implement P69-999",
            scenario="jira_issue_develop",
            status=TaskStatus.QUEUED,
            workflow_stage=WorkflowStage.INTAKE,
            translation_json=None,
            plan_json=None,
            latest_result_json=None,
            pending_approval=False,
            retry_count=0,
        )
        orchestrator = self._orchestrator()
        orchestrator.tool_gateway.execute = Mock(
            side_effect=ToolInvocationError("404 not found", http_status=404)
        )
        orchestrator._probe_jira_auth_health = Mock(return_value=200)

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._prefetch_jira_issue_context(
                task=task,
                actor_name="tester",
                issue_key="P69-999",
            )

        orchestrator._probe_jira_auth_health.assert_called_once()
        self.assertEqual(task.latest_result_json["error_kind"], "not_found_or_invisible")
        self.assertNotIn("expired or been revoked", task.latest_result_json["message"])

    def test_jira_develop_aborts_when_issue_unfetchable(self) -> None:
        """Regression for the P69-7 incident.

        When the Jira issue can't be fetched (deleted ticket, wrong project,
        permission error), the orchestrator must NOT fall back to a synthetic
        issue context and continue. Continuing causes codegen to invent
        requirements for a ghost ticket — exactly what produced the bogus
        password-toggle change for non-existent P69-7.
        """
        task = SimpleNamespace(
            id="task-ghost",
            session_id="session-ghost",
            actor_name="tester",
            request_text="完成Jira上的P69-7",
            scenario="jira_issue_develop",
            status=TaskStatus.QUEUED,
            workflow_stage=WorkflowStage.INTAKE,
            translation_json=None,
            plan_json=None,
            latest_result_json=None,
            pending_approval=False,
            retry_count=0,
        )
        orchestrator = self._orchestrator()
        orchestrator._translate_request = Mock(
            return_value=_semantic_translation(issue_key="P69-7")
        )
        # Jira lookup fails: returns None (mirrors the real failure path
        # where _prefetch_jira_issue_context marks the task FAILED and returns).
        orchestrator._prefetch_jira_issue_context = Mock(return_value=None)
        # The pipeline must NOT reach planning, codegen, or any tool that
        # would have invented requirements.
        orchestrator._prefetch_planning_repository_context = Mock()
        orchestrator.primary_agent.generate_plan = Mock()
        orchestrator.reviewer_agent.review_plan = Mock()
        orchestrator._gather_codegen_context = Mock()

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._bootstrap_task_impl(task=task, actor_name="tester")

        orchestrator._prefetch_jira_issue_context.assert_called_once()
        # Hard guard: nothing downstream should fire when the Jira issue
        # can't be fetched.
        orchestrator._prefetch_planning_repository_context.assert_not_called()
        orchestrator.primary_agent.generate_plan.assert_not_called()
        orchestrator.reviewer_agent.review_plan.assert_not_called()
        orchestrator._gather_codegen_context.assert_not_called()

    def test_gather_codegen_context_from_sandbox(self) -> None:
        plan = _plan()
        task = _task(plan)
        sandbox_file = self.temp_dir / task.id / "app" / "example.py"
        sandbox_file.parent.mkdir(parents=True)
        sandbox_file.write_text("print('from sandbox')\n", encoding="utf-8")
        orchestrator = self._orchestrator()
        orchestrator.knowledge_service = Mock()

        context = orchestrator._gather_codegen_context(task=task, plan=plan)

        self.assertEqual(context, {"app/example.py": "print('from sandbox')\n"})

    def test_gather_context_reads_full_file(self) -> None:
        plan = _plan()
        task = _task(plan)
        source_file = self.temp_dir / "source" / "app" / "example.py"
        source_file.parent.mkdir(parents=True)
        full_content = "".join(f"line {index}\n" for index in range(1, 51))
        source_file.write_text(full_content, encoding="utf-8")
        orchestrator = self._orchestrator()
        orchestrator.tool_gateway.settings.knowledge_source_path = str(self.temp_dir / "source")
        orchestrator.knowledge_service = Mock()
        orchestrator.knowledge_service.search.return_value = [{"snippet": "line 1\nline 2\nline 3\n"}]

        context = orchestrator._gather_codegen_context(task=task, plan=plan)

        self.assertEqual(context, {"app/example.py": full_content})
        orchestrator.knowledge_service.search.assert_not_called()

    def test_gather_context_truncates_large_file(self) -> None:
        plan = _plan()
        task = _task(plan)
        source_file = self.temp_dir / "source" / "app" / "example.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("0123456789abcdef", encoding="utf-8")
        orchestrator = self._orchestrator()
        orchestrator.tool_gateway.settings.knowledge_source_path = str(self.temp_dir / "source")
        orchestrator.tool_gateway.settings.knowledge_max_file_bytes = 10

        context = orchestrator._gather_codegen_context(task=task, plan=plan)

        self.assertEqual(context, {"app/example.py": "0123456789\n... (truncated)"})

    def test_gather_codegen_context_empty(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator.knowledge_service = Mock()
        orchestrator.knowledge_service.search.return_value = []

        context = orchestrator._gather_codegen_context(task=task, plan=plan)

        self.assertEqual(context, {})

    def test_develop_pipeline_codegen_failure_sets_failed(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator._gather_codegen_context = Mock(return_value={"app/example.py": "old\n"})
        orchestrator.tool_gateway.execute = Mock(side_effect=RuntimeError("codegen boom"))

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ) as set_status:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)
        self.assertIn("\u4ee3\u7801\u751f\u6210\u5931\u8d25", task.latest_result_json["message"])
        set_status.assert_any_call(
            orchestrator.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.ACTION,
            source=ANY,
            message=ANY,
        )

    def test_develop_pipeline_test_failure_sets_failed(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator._gather_codegen_context = Mock(return_value={"app/example.py": "old\n"})
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(),
                {"status": "patched"},
                {
                    "status": "failed",
                    "overall_passed": False,
                    "failed_count": 2,
                    "passed_count": 1,
                    "total_steps": 3,
                },
            ]
        )

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ) as set_status:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(orchestrator.tool_gateway.execute.call_count, 3)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)
        self.assertIn("\u6d4b\u8bd5\u672a\u901a\u8fc7\uff1a2", task.latest_result_json["message"])
        set_status.assert_any_call(
            orchestrator.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.ACTION,
            source=ANY,
            message=ANY,
        )

    def test_develop_pipeline_skips_test_when_no_config(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator._gather_codegen_context = Mock(return_value={"app/example.py": "old\n"})
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(),
                {"status": "patched"},
                RuntimeError("Test pipeline config not found: tests.yaml"),
                {"verdict": "pass", "violations": [], "rules_checked": 4, "duration_ms": 1},
                {"status": "transitioned"},
            ]
        )

        with patch("app.orchestrator.service.record_event") as record, patch(
            "app.orchestrator.service.set_task_status"
        ) as set_status:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(orchestrator.tool_gateway.execute.call_count, 5)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(task.latest_result_json["test_result"]["status"], "skipped")
        self.assertTrue(task.latest_result_json["test_result"]["overall_passed"])
        record.assert_any_call(
            orchestrator.db,
            task_id=task.id,
            event_type=EventType.TOOL_SKIPPED,
            source=ANY,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            tool_name="test_pipeline.run",
            message="Test pipeline skipped: Test pipeline config not found: tests.yaml",
            payload={"error": "Test pipeline config not found: tests.yaml", "plan_id": plan.plan_id},
        )
        skipped_calls = [
            call for call in record.call_args_list if call.kwargs.get("event_type").value == "tool_skipped"
        ]
        self.assertEqual(len(skipped_calls), 1)
        set_status.assert_any_call(
            orchestrator.db,
            task=task,
            new_status=TaskStatus.COMPLETED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            source=ANY,
            message=ANY,
        )

    def test_develop_summary_includes_diff(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator._gather_codegen_context = Mock(return_value={"app/example.py": "old\n"})
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(),
                {"status": "patched", "method": "git_apply"},
                {
                    "status": "passed",
                    "overall_passed": True,
                    "failed_count": 0,
                    "passed_count": 1,
                    "total_steps": 1,
                },
                {"verdict": "pass", "violations": [], "rules_checked": 4, "duration_ms": 1},
                {"status": "transitioned", "issue_key": "OPS-123"},
            ]
        )

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        message = task.latest_result_json["message"]
        self.assertEqual(task.latest_result_json["status"], TaskStatus.COMPLETED.value)
        self.assertIn("## OPS-123 Development completed", message)
        self.assertIn("Modified in this run", message)
        self.assertIn("- `app/example.py`", message)
        self.assertIn("```diff\ndiff --git a/app/example.py b/app/example.py", message)
        self.assertIn("Codegen: mock", message)
        self.assertIn("Patch apply method: git_apply", message)
        self.assertIn("Tests: passed", message)
        self.assertIn("Review: pass", message)
        self.assertIn("Jira: status transitioned", message)
        self.assertNotIn("Answer the question with grounded evidence from the repository", message)
        self.assertEqual(task.latest_result_json["result"]["issue_key"], "OPS-123")
        self.assertEqual(task.latest_result_json["result"]["files_changed"], ["app/example.py"])
        self.assertEqual(task.latest_result_json["result"]["patch_method"], "git_apply")
        self.assertTrue(task.latest_result_json["result"]["jira_transitioned"])

    def test_develop_pipeline_reviewer_blocks(self) -> None:
        plan = _plan()
        task = _task(plan)
        orchestrator = self._orchestrator()
        orchestrator._gather_codegen_context = Mock(return_value={"app/example.py": "old\n"})
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result(),
                {"status": "patched"},
                {
                    "status": "passed",
                    "overall_passed": True,
                    "failed_count": 0,
                    "passed_count": 1,
                    "total_steps": 1,
                },
                {
                    "verdict": "block",
                    "violations": [{"rule_name": "no-secrets", "message": "Diff adds a secret."}],
                    "rules_checked": 4,
                    "duration_ms": 1,
                },
            ]
        )

        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.set_task_status"
        ) as set_status:
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        self.assertEqual(orchestrator.tool_gateway.execute.call_count, 4)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)
        self.assertIn("\u4ee3\u7801\u5ba1\u67e5\u672a\u901a\u8fc7", task.latest_result_json["message"])
        self.assertIn("Diff adds a secret.", task.latest_result_json["message"])
        set_status.assert_any_call(
            orchestrator.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.REVIEWER,
            source=ANY,
            message=ANY,
        )


if __name__ == "__main__":
    unittest.main()
