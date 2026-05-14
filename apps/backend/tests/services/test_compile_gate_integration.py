from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import FinalOutputContract, GeneratedPlan, PlanStep, PlanTool  # noqa: E402
from app.core.enums import (  # noqa: E402
    ActorRole,
    EventType,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.verification_profile import (  # noqa: E402
    CompileCheckResult,
    parse_compiler_errors,
)


CUSTOMER_FILE = "app/src/main/java/com/example/CustomerSignup.kt"
CUSTOMER_SOURCE = (
    "package com.example\n"
    "\n"
    "fun screen() {\n"
    "    ImeAction.Search\n"
    "}\n"
)
CUSTOMER_DIFF = (
    f"diff --git a/{CUSTOMER_FILE} b/{CUSTOMER_FILE}\n"
    f"--- a/{CUSTOMER_FILE}\n"
    f"+++ b/{CUSTOMER_FILE}\n"
    "@@ -1,4 +1,5 @@\n"
    " package com.example\n"
    " \n"
    "+import androidx.compose.ui.text.input.ImeAction\n"
    "+\n"
    " fun screen() {\n"
)


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="compile-gate-integration-"))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="compile-gate-integration-"))
    finally:
        tempfile._os.mkdir = original_mkdir


@pytest.fixture()
def work_dir() -> Path:
    path = _writable_mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-compile-profile",
        objective="Fix CustomerSignup compile error.",
        request_summary="Fix CustomerSignup compile error.",
        scenario="jira_issue_develop",
        change_summary="Update CustomerSignup.",
        change_explanation="Update CustomerSignup.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[],
        must_touch_files=[CUSTOMER_FILE],
        expected_new_files=[],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate patch.",
            )
        ],
        steps=[
            PlanStep(
                step_id="s1",
                title="Generate patch",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=[],
                tool_name="codegen.generate_patch",
                expected_output="Unified diff.",
                success_criteria="Diff generated.",
            )
        ],
        final_output_contract=FinalOutputContract(type="jira_issue_develop", required_fields=["status"]),
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-compile-profile",
        session_id="session-task-compile-profile",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="Fix CustomerSignup compile error.",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json=None,
        plan_json=None,
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


def _make_orchestrator(tmp_path: Path) -> PrimaryOrchestrator:
    orchestrator = PrimaryOrchestrator(db=Mock())
    settings = orchestrator.tool_gateway.settings
    settings.sandbox_base_dir = str(tmp_path / "sandboxes")
    settings.sandbox_external_root = ""
    settings.agent_workspace_root = str(tmp_path / "workspace")
    settings.knowledge_source_path = ""
    settings.verification_profile_enabled = True
    settings.verification_compile_timeout_seconds = 17
    settings.verification_max_repair_rounds = 1
    settings.codegen_repair_round_timeout_seconds = 30.0
    settings.codegen_repair_cap_exceeded_to_approval = False
    settings.develop_require_jira_approval = False
    settings.evidence_chain_gate_enabled = False
    orchestrator.db.flush = Mock()
    return orchestrator


def _ensure_android_sandbox(orchestrator: PrimaryOrchestrator, task: SimpleNamespace) -> Path:
    sandbox_dir = orchestrator._develop_sandbox_dir(task)
    (sandbox_dir / "app" / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (sandbox_dir / "app" / "src" / "main").mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "app" / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (sandbox_dir / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
        "<manifest />\n",
        encoding="utf-8",
    )
    (sandbox_dir / CUSTOMER_FILE).write_text(CUSTOMER_SOURCE, encoding="utf-8")
    return sandbox_dir


def _profile_result(*, sandbox_dir: Path, output: str, passed: bool) -> CompileCheckResult:
    errors = [
        error.to_dict("android_gradle")
        for error in parse_compiler_errors(output, "android_gradle", repo_root=sandbox_dir)
    ]
    return CompileCheckResult(
        passed=passed,
        status="passed" if passed else "failed",
        repo_type="android_gradle",
        command=["./gradlew", ":app:compileDebugKotlin", "--quiet", "--no-daemon"],
        output=output,
        errors=[] if passed else errors,
        timed_out=False,
        duration_ms=25,
    )


def test_compile_failure_triggers_repair_loop(work_dir: Path) -> None:
    orchestrator = _make_orchestrator(work_dir)
    task = _task()
    plan = _plan()
    sandbox_dir = _ensure_android_sandbox(orchestrator, task)
    kotlin_error = (
        f"e: {(sandbox_dir / CUSTOMER_FILE).as_uri()}:155:51 "
        "Unresolved reference: ImeAction\n"
    )
    pipeline_state: dict[str, object] = {
        "files_changed": [CUSTOMER_FILE],
        "verification_compile_pending": True,
    }
    orchestrator._execute_develop_tool = Mock(
        return_value={"diff": CUSTOMER_DIFF, "files_changed": [CUSTOMER_FILE]}
    )

    with patch(
        "app.services.verification_profile.run_compile_check",
        side_effect=[
            _profile_result(sandbox_dir=sandbox_dir, output=kotlin_error, passed=False),
            _profile_result(sandbox_dir=sandbox_dir, output="", passed=True),
        ],
    ) as compile_mock, patch("app.orchestrator.service.record_event") as record_event_mock, patch(
        "app.orchestrator.service.time.sleep"
    ), patch("app.orchestrator.service.commit_checkpoint"):
        outcome = orchestrator._run_compile_repair_loop(
            task=task,
            actor_name="tester",
            plan=plan,
            pipeline_state=pipeline_state,
            approval_id=None,
        )

    assert outcome == "passed"
    assert compile_mock.call_count == 2
    assert orchestrator._execute_develop_tool.call_count == 1
    repair_payload = orchestrator._execute_develop_tool.call_args.kwargs["payload"]
    prompt = repair_payload["task_description"]
    assert "Unresolved reference: ImeAction" in prompt
    assert "Modify only files in must_touch_files" in prompt
    assert CUSTOMER_FILE in prompt
    event_types = [call.kwargs.get("event_type") for call in record_event_mock.call_args_list]
    assert EventType.COMPILE_FAILED in event_types


def test_compile_success_proceeds_to_diff_reviewer(work_dir: Path) -> None:
    orchestrator = _make_orchestrator(work_dir)
    task = _task()
    plan = _plan()
    sandbox_dir = _ensure_android_sandbox(orchestrator, task)
    task.latest_result_json = {
        "pipeline_state": {
            "evidence_bundle_done": True,
            "codegen_result": {
                "diff": CUSTOMER_DIFF,
                "files_changed": [CUSTOMER_FILE],
                "provider_name": "mock",
            },
            "diff": CUSTOMER_DIFF,
            "files_changed": [CUSTOMER_FILE],
            "sandbox_result": {"status": "patched", "method": "test"},
            "completeness_check": {"complete": True},
            "retry_done": True,
            "test_result": {
                "status": "compile_pending",
                "overall_passed": True,
                "verified_by": "compile",
            },
            "verification_compile_pending": True,
            "diff_shape_done": True,
            "runtime_validation_done": True,
        }
    }
    orchestrator._gather_codegen_context = Mock(return_value={CUSTOMER_FILE: CUSTOMER_SOURCE})
    orchestrator._execute_develop_tool = Mock(return_value=None)

    with patch(
        "app.services.verification_profile.run_compile_check",
        return_value=_profile_result(sandbox_dir=sandbox_dir, output="", passed=True),
    ) as compile_mock, patch("app.orchestrator.service.record_event"), patch(
        "app.orchestrator.service.set_task_status"
    ), patch("app.orchestrator.service.commit_checkpoint"):
        orchestrator._execute_develop_pipeline(
            task=task,
            actor_name="tester",
            plan=plan,
            approval_id=None,
        )

    assert compile_mock.call_count == 1
    tool_names = [
        call.kwargs.get("tool_name")
        for call in orchestrator._execute_develop_tool.call_args_list
    ]
    assert "diff_reviewer.review" in tool_names


def test_compile_cap_exceeded_marks_task_failed_in_verification_path(work_dir: Path) -> None:
    orchestrator = _make_orchestrator(work_dir)
    settings = orchestrator.tool_gateway.settings
    settings.verification_max_repair_rounds = 2
    settings.codegen_repair_cap_exceeded_to_approval = True
    settings.verification_compile_fail_to_approval = False
    task = _task()
    plan = _plan()
    sandbox_dir = _ensure_android_sandbox(orchestrator, task)
    kotlin_error = (
        f"e: {(sandbox_dir / CUSTOMER_FILE).as_uri()}:155:51 "
        "Unresolved reference: ImeAction\n"
    )
    failed_result = _profile_result(sandbox_dir=sandbox_dir, output=kotlin_error, passed=False)
    pipeline_state: dict[str, object] = {
        "files_changed": [CUSTOMER_FILE],
        "verification_compile_pending": True,
        "verification_profile": {"repo_type": "android_gradle"},
    }

    with patch(
        "app.services.verification_profile.run_compile_check",
        side_effect=[failed_result, failed_result, failed_result],
    ) as compile_mock, patch.object(
        orchestrator, "_attempt_compile_repair", return_value=(False, [])
    ) as repair_mock, patch.object(
        orchestrator, "_request_compile_repair_approval"
    ) as approval_mock, patch.object(
        orchestrator, "_fail_develop_pipeline", wraps=orchestrator._fail_develop_pipeline
    ) as fail_mock, patch.object(
        orchestrator, "_run_failure_diagnosis"
    ) as diagnosis_mock, patch(
        "app.orchestrator.service.record_event"
    ), patch(
        "app.orchestrator.service.set_task_status"
    ), patch(
        "app.orchestrator.service.commit_checkpoint"
    ):
        outcome = orchestrator._run_compile_repair_loop(
            task=task,
            actor_name="tester",
            plan=plan,
            pipeline_state=pipeline_state,
            approval_id=None,
        )

    assert outcome == "failed"
    assert compile_mock.call_count == 3
    assert repair_mock.call_count == 2
    approval_mock.assert_not_called()
    fail_mock.assert_called_once()
    payload = fail_mock.call_args.kwargs["payload"]
    assert payload["reason"] == "compile_gate_exhausted"
    assert payload["rounds_attempted"] == 2
    assert task.latest_result_json["reason"] == "compile_gate_exhausted"
    diagnosis_mock.assert_called_once()


def test_compile_skipped_does_not_block_continues_passing(work_dir: Path) -> None:
    orchestrator = _make_orchestrator(work_dir)
    settings = orchestrator.tool_gateway.settings
    settings.codegen_repair_cap_exceeded_to_approval = True
    settings.verification_compile_fail_to_approval = False
    task = _task()
    plan = _plan()
    _ensure_android_sandbox(orchestrator, task)
    skipped_result = CompileCheckResult(
        passed=True,
        status="skipped",
        repo_type="android_gradle",
        command=["./gradlew", ":app:compileDebugKotlin", "--quiet", "--no-daemon"],
        output="",
        errors=[],
        timed_out=False,
        duration_ms=0,
        reason="toolchain_missing",
    )
    pipeline_state: dict[str, object] = {
        "files_changed": [CUSTOMER_FILE],
        "verification_compile_pending": True,
        "verification_profile": {"repo_type": "android_gradle"},
    }

    with patch(
        "app.services.verification_profile.run_compile_check",
        return_value=skipped_result,
    ) as compile_mock, patch.object(
        orchestrator, "_attempt_compile_repair"
    ) as repair_mock, patch(
        "app.orchestrator.service.record_event"
    ) as record_event_mock, patch(
        "app.orchestrator.service.commit_checkpoint"
    ):
        outcome = orchestrator._run_compile_repair_loop(
            task=task,
            actor_name="tester",
            plan=plan,
            pipeline_state=pipeline_state,
            approval_id=None,
        )

    assert outcome == "passed"
    assert compile_mock.call_count == 1
    repair_mock.assert_not_called()
    event_types = [call.kwargs.get("event_type") for call in record_event_mock.call_args_list]
    assert EventType.COMPILE_FAILED not in event_types
    assert pipeline_state["compile_gate"]["passed"] is True
    assert pipeline_state["compile_gate_done"] is True


def test_legacy_codegen_repair_cap_still_routes_to_approval(work_dir: Path) -> None:
    orchestrator = _make_orchestrator(work_dir)
    settings = orchestrator.tool_gateway.settings
    settings.codegen_max_repair_rounds = 2
    settings.codegen_repair_cap_exceeded_to_approval = True
    settings.verification_compile_fail_to_approval = False
    task = _task()
    plan = _plan()
    _ensure_android_sandbox(orchestrator, task)
    failed_result = CompileCheckResult(
        passed=False,
        status="failed",
        repo_type="legacy",
        command=None,
        output="legacy failure",
        errors=[{"file": CUSTOMER_FILE, "error": "legacy failure"}],
        timed_out=False,
        duration_ms=1,
    )
    pipeline_state: dict[str, object] = {
        "files_changed": [CUSTOMER_FILE],
    }

    with patch(
        "app.services.compile_gate.run_compile_gate",
        side_effect=[failed_result, failed_result, failed_result],
    ) as compile_mock, patch.object(
        orchestrator, "_attempt_compile_repair", return_value=(False, [])
    ) as repair_mock, patch.object(
        orchestrator, "_request_compile_repair_approval"
    ) as approval_mock, patch.object(
        orchestrator, "_fail_develop_pipeline"
    ) as fail_mock, patch(
        "app.orchestrator.service.record_event"
    ), patch(
        "app.orchestrator.service.commit_checkpoint"
    ):
        outcome = orchestrator._run_compile_repair_loop(
            task=task,
            actor_name="tester",
            plan=plan,
            pipeline_state=pipeline_state,
            approval_id=None,
        )

    assert outcome == "approval_requested"
    assert compile_mock.call_count == 3
    assert repair_mock.call_count == 2
    approval_mock.assert_called_once()
    fail_mock.assert_not_called()
