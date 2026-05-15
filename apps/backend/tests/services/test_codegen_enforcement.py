from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import (  # noqa: E402
    CodegenResult,
    FinalOutputContract,
    GeneratedPlan,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
)
from app.core.enums import ActorRole, EventType, RiskLevel, RoleName, TaskStatus, ToolPermissionCategory, WorkflowStage  # noqa: E402
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.codegen import CodeGenerator, CodegenError  # noqa: E402
from app.services.codegen_self_validate import validate_diff_applies  # noqa: E402


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        codegen_provider="codex",
        primary_agent_provider="mock",
    )


def _result(paths: list[str]) -> CodegenResult:
    first = paths[0] if paths else "generated.py"
    return CodegenResult(
        diff=(
            f"diff --git a/{first} b/{first}\n"
            f"--- a/{first}\n"
            f"+++ b/{first}\n"
            "@@ -1 +1,2 @@\n"
            " old\n"
            "+new\n"
        ),
        summary=f"Generated patch modifying {len(paths)} file(s).",
        files_changed=paths,
        provider_name="codex",
        model_name="codex-cli",
    )


def _generate_with_mocked_codex(
    *,
    plan_json: dict[str, object],
    files_changed: list[str],
    context_files: dict[str, str] | None = None,
) -> CodegenResult:
    generator = CodeGenerator(_settings())

    def fake_call(prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
        assert "=== ALLOWED FILES" in prompt if plan_json.get("must_touch_files") or plan_json.get("expected_new_files") else True
        return _result(files_changed)

    generator._call_codex = fake_call  # type: ignore[method-assign]
    return generator.generate_patch(
        task_id="task-1",
        plan_json=plan_json,
        context_files=context_files or {"good.py": "old\n"},
    )


def test_drift_rejected_with_clear_message() -> None:
    with pytest.raises(CodegenError) as exc:
        _generate_with_mocked_codex(
            plan_json={"objective": "Update good.py", "must_touch_files": ["good.py"]},
            files_changed=["evil.py"],
        )

    message = str(exc.value)
    assert "file_outside_allowed_set" in message
    assert "evil.py" in message
    assert "good.py" in message


def test_source_name_prefix_accepted_against_repo_relative_plan() -> None:
    """Codegen re-emits with repo-name prefix; plan has repo-relative path. Must accept."""
    result = _generate_with_mocked_codex(
        plan_json={
            "objective": "Update src/pages/UserVerification.js",
            "must_touch_files": ["src/pages/UserVerification.js"],
        },
        files_changed=["handyman-admin-dashboard/src/pages/UserVerification.js"],
        context_files={"src/pages/UserVerification.js": "old\n"},
    )
    assert result is not None


def test_repo_relative_accepted_against_prefix_plan() -> None:
    """Reverse direction: plan with prefix, codegen returns relative — must accept."""
    result = _generate_with_mocked_codex(
        plan_json={
            "objective": "Update foo",
            "must_touch_files": ["handyman-admin-dashboard/src/pages/UserVerification.js"],
        },
        files_changed=["src/pages/UserVerification.js"],
        context_files={"src/pages/UserVerification.js": "old\n"},
    )
    assert result is not None


def test_partial_segment_match_still_rejected() -> None:
    """Suffix tolerance must respect segment boundaries. 'rc/foo.js' MUST NOT match 'src/foo.js'."""
    with pytest.raises(CodegenError) as exc:
        _generate_with_mocked_codex(
            plan_json={"objective": "x", "must_touch_files": ["src/foo.js"]},
            files_changed=["rc/foo.js"],
            context_files={"src/foo.js": "old\n"},
        )
    assert "file_outside_allowed_set" in str(exc.value)


def test_valid_targets_pass() -> None:
    result = _generate_with_mocked_codex(
        plan_json={"objective": "Update good.py", "must_touch_files": ["good.py"]},
        files_changed=["good.py"],
    )

    assert result.files_changed == ["good.py"]


def test_expected_new_file_passes() -> None:
    result = _generate_with_mocked_codex(
        plan_json={"objective": "Create new.py", "expected_new_files": ["new.py"]},
        files_changed=["new.py"],
        context_files={"new.py": ""},
    )

    assert result.files_changed == ["new.py"]


def test_expected_new_wrong_header_normalized_to_create_file(tmp_path: Path) -> None:
    bad_diff = (
        "diff --git a/database.rules.json b/database.rules.json\n"
        "--- a/database.rules.json\n"
        "+++ b/database.rules.json\n"
        "@@ -0,0 +1,6 @@\n"
        "+{\n"
        "+  \"rules\": {\n"
        "+    \".read\": \"auth != null\",\n"
        "+    \".write\": \"auth != null\"\n"
        "+  }\n"
        "+}\n"
    )

    normalized, changed = CodeGenerator._normalize_expected_new_file_diff_headers(
        bad_diff,
        expected_new_files=["database.rules.json"],
        source_repo_path=str(tmp_path),
    )

    assert changed is True
    assert "new file mode 100644" in normalized
    assert "--- /dev/null" in normalized
    assert "--- a/database.rules.json" not in normalized
    ok, err = validate_diff_applies(normalized, tmp_path)
    assert ok, err


def test_expected_new_normalizer_ignores_unplanned_path() -> None:
    bad_diff = (
        "diff --git a/other.json b/other.json\n"
        "--- a/other.json\n"
        "+++ b/other.json\n"
        "@@ -0,0 +1 @@\n"
        "+{}\n"
    )

    normalized, changed = CodeGenerator._normalize_expected_new_file_diff_headers(
        bad_diff,
        expected_new_files=["database.rules.json"],
        source_repo_path=None,
    )

    assert changed is False
    assert normalized == bad_diff


def test_expected_new_normalizer_ignores_real_modifications() -> None:
    diff = (
        "diff --git a/database.rules.json b/database.rules.json\n"
        "--- a/database.rules.json\n"
        "+++ b/database.rules.json\n"
        "@@ -1,2 +1,2 @@\n"
        "-{\"rules\": true}\n"
        "+{\"rules\": {\".read\": \"auth != null\"}}\n"
    )

    normalized, changed = CodeGenerator._normalize_expected_new_file_diff_headers(
        diff,
        expected_new_files=["database.rules.json"],
        source_repo_path=None,
    )

    assert changed is False
    assert normalized == diff


def test_expected_new_normalizer_ignores_existing_file(tmp_path: Path) -> None:
    (tmp_path / "database.rules.json").write_text("{}\n", encoding="utf-8")
    bad_diff = (
        "diff --git a/database.rules.json b/database.rules.json\n"
        "--- a/database.rules.json\n"
        "+++ b/database.rules.json\n"
        "@@ -0,0 +1 @@\n"
        "+{}\n"
    )

    normalized, changed = CodeGenerator._normalize_expected_new_file_diff_headers(
        bad_diff,
        expected_new_files=["database.rules.json"],
        source_repo_path=str(tmp_path),
    )

    assert changed is False
    assert normalized == bad_diff


def test_empty_allowed_skips_enforcement() -> None:
    result = _generate_with_mocked_codex(
        plan_json={"objective": "Update whatever is needed."},
        files_changed=["evil.py"],
    )

    assert result.files_changed == ["evil.py"]


def test_partial_drift_rejected() -> None:
    with pytest.raises(CodegenError) as exc:
        _generate_with_mocked_codex(
            plan_json={"objective": "Update good.py", "must_touch_files": ["good.py"]},
            files_changed=["good.py", "evil.py"],
        )

    assert "codegen modified files not in plan: ['evil.py']" in str(exc.value)


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-1",
        objective="Implement OPS-123.",
        request_summary="Implement OPS-123.",
        scenario="jira_issue_develop",
        change_summary="Update target modules.",
        change_explanation="Update only the planner-selected files.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[
            PlanCodeLocation(source_name="repo", relative_path="good.py", reason="Target file."),
            PlanCodeLocation(source_name="repo", relative_path="also.py", reason="Target file."),
        ],
        must_touch_files=["good.py", "also.py"],
        expected_new_files=[],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate code changes.",
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
                success_criteria="Patch is generated.",
            )
        ],
        final_output_contract=FinalOutputContract(type="jira_issue_develop", required_fields=["status"]),
    )


def test_jira_develop_drift_triggers_tool_failed_and_failure_diagnosis() -> None:
    plan = _plan()
    task = SimpleNamespace(
        id="task-1",
        session_id="session-1",
        actor_name="tester",
        actor_role=ActorRole.ADMIN,
        request_text="implement OPS-123",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={},
        plan_json=plan.model_dump(mode="json"),
        latest_result_json={"pipeline_state": {"evidence_bundle_done": True}},
        pending_approval=False,
        retry_count=0,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskLevel.MEDIUM,
        source_name="repo",
    )
    orchestrator = PrimaryOrchestrator(db=Mock())
    orchestrator.tool_gateway.settings.sandbox_base_dir = str(BACKEND_ROOT)
    orchestrator._gather_codegen_context = Mock(return_value={"good.py": "old\n", "also.py": "old\n"})
    orchestrator._resolve_knowledge_source_path = Mock(return_value=BACKEND_ROOT)
    orchestrator._sync_retry_count = Mock()

    with (
        patch(
            "app.services.codegen.CodeGenerator.generate_patch",
            side_effect=CodegenError("file_outside_allowed_set: codegen modified files not in plan: ['evil.py']"),
        ),
        patch("app.orchestrator.service.record_event") as record,
        patch("app.orchestrator.service.set_task_status") as set_status,
        patch("app.orchestrator.service.run_diagnosis") as run_diagnosis,
    ):
        orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

    tool_failed_calls = [
        call for call in record.call_args_list
        if call.kwargs.get("event_type") == EventType.TOOL_FAILED
        and call.kwargs.get("tool_name") == "codegen.generate_patch"
    ]
    assert tool_failed_calls
    assert "file_outside_allowed_set" in tool_failed_calls[0].kwargs["payload"]["error"]
    assert task.latest_result_json["status"] == TaskStatus.FAILED.value
    set_status.assert_any_call(
        orchestrator.db,
        task=task,
        new_status=TaskStatus.FAILED,
        new_stage=WorkflowStage.DONE,
        role=RoleName.ACTION,
        source=ANY,
        message=ANY,
    )
    run_diagnosis.assert_called_once()
