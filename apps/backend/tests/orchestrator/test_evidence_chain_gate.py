from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agents.schemas import (
    FinalOutputContract,
    GeneratedPlan,
    PlanStep,
    PlanTool,
)
from app.core.enums import (
    ActorRole,
    ApprovalStatus,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator
from app.schemas.evidence import EvidenceItem
from app.services.task_workspace import TaskWorkspace


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="evidence-chain-gate-"))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="evidence-chain-gate-"))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan(task_id: str = "task-chain", *, expected_new_files: list[str] | None = None) -> GeneratedPlan:
    return GeneratedPlan(
        task_id=task_id,
        objective="Update implementation.",
        request_summary="Update implementation.",
        scenario="jira_issue_develop",
        change_summary="Modify the requested file.",
        change_explanation="The codegen step updates the requested file.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[],
        must_touch_files=[],
        expected_new_files=expected_new_files or [],
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
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _task(task_id: str = "task-chain", *, request_text: str = "Update src/a.py for TEST-1.") -> SimpleNamespace:
    plan = _plan(task_id)
    return SimpleNamespace(
        id=task_id,
        session_id=f"session-{task_id}",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text=request_text,
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={"issue_key": "TEST-1", "normalized_request": request_text},
        plan_json=plan.model_dump(mode="json"),
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


def _diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index aaa..bbb 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _codegen_result(path: str, *, claims: list[dict] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "diff": _diff(path),
        "summary": f"Updated {path}.",
        "files_changed": [path],
        "provider_name": "mock",
        "model_name": "mock",
    }
    if claims is not None:
        result["claims"] = claims
    return result


def _review_pass() -> dict[str, object]:
    return {"verdict": "pass", "violations": [], "rules_checked": 4, "duration_ms": 1}


def _test_pass() -> dict[str, object]:
    return {
        "status": "passed",
        "overall_passed": True,
        "failed_count": 0,
        "passed_count": 1,
        "total_steps": 1,
    }


def _write_workspace(
    orchestrator: PrimaryOrchestrator,
    task: SimpleNamespace,
    *,
    evidence_path: str,
) -> None:
    workspace = TaskWorkspace.for_task(task.id, orchestrator.tool_gateway.settings)
    workspace.write_intent(
        intent_text=task.request_text,
        request_text=task.request_text,
        jira_issue_body="Summary: update requested file\n\nDescription: edit source files",
        jira_issue_key="TEST-1",
        language="en",
        must_touch_files=[],
        scenario="jira_issue_develop",
    )
    workspace.add_evidence(
        [
            EvidenceItem(
                id="ev-1",
                source="cc_read",
                file_path=evidence_path,
                line_start=1,
                line_end=2,
                snippet="old",
                chunk_kind="line_window",
            )
        ]
    )


def _orchestrator(root: Path, *, gate_enabled: bool = True) -> PrimaryOrchestrator:
    source_tree = root / "source"
    (source_tree / "src").mkdir(parents=True)
    (source_tree / "src" / "a.py").write_text("old\n", encoding="utf-8")
    (source_tree / "src" / "b.py").write_text("old\n", encoding="utf-8")

    orchestrator = PrimaryOrchestrator(db=Mock())
    settings = orchestrator.tool_gateway.settings
    settings.agent_workspace_root = str(root / "workspace")
    settings.sandbox_base_dir = str(root / "sandbox")
    settings.knowledge_source_path = str(source_tree)
    settings.knowledge_max_file_bytes = 120_000
    settings.develop_require_jira_approval = True
    settings.evidence_chain_gate_enabled = gate_enabled
    settings.evidence_chain_min_confident_claims = 3
    settings.minimax_api_key = None
    orchestrator._sync_retry_count = Mock()
    orchestrator._gather_codegen_context = Mock(return_value={"src/a.py": "old\n", "src/b.py": "old\n"})
    orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
    orchestrator.db.flush = Mock()
    return orchestrator


def _run_pipeline(
    orchestrator: PrimaryOrchestrator,
    task: SimpleNamespace,
    plan: GeneratedPlan,
    codegen_result: dict[str, object],
) -> list[object]:
    added: list[object] = []
    orchestrator.db.add = Mock(
        side_effect=lambda obj: (added.append(obj), setattr(obj, "id", f"approval-{len(added)}"))[0]
    )
    orchestrator.tool_gateway.execute = Mock(
        side_effect=[
            codegen_result,
            {"status": "patched", "method": "git_apply"},
            _test_pass(),
            _review_pass(),
        ]
    )
    with patch("app.orchestrator.service.record_event") as record_event, patch(
        "app.orchestrator.service.set_task_status"
    ), patch(
        "app.services.reservations.build_reservations",
        return_value=SimpleNamespace(reservations=[], provider="test", model="none"),
    ):
        orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)
    task._record_event_calls = record_event.call_args_list
    return added


def test_chain_closed_task_proceeds_to_approval() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")

        added = _run_pipeline(orchestrator, task, plan, _codegen_result("src/a.py"))

        assert task.latest_result_json["status"] == TaskStatus.AWAITING_APPROVAL.value
        assert len(added) == 1
        approval = added[0]
        assert approval.action_name == "jira.transition_issue"
        assert approval.status == ApprovalStatus.PENDING
        payload = approval.request_payload_json
        assert payload["evidence_chain"]["closed"] is True
        assert payload["evidence_chain"]["warnings"] == []
        assert payload["evidence_chain"]["evidence_count"] >= 1
        assert "src/a.py" in payload["evidence_chain"]["modified_files_with_evidence"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_block_severity_fails_before_approval_or_jira_transition() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        task = _task(request_text="Apply the TEST-1 implementation update.")
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        orchestrator._request_jira_transition_approval = Mock()

        added = _run_pipeline(orchestrator, task, plan, _codegen_result("src/b.py"))

        assert added == []
        orchestrator._request_jira_transition_approval.assert_not_called()
        assert task.latest_result_json["status"] == TaskStatus.FAILED.value
        assert task.latest_result_json["evidence_chain"]["closed"] is False
        assert "Evidence chain broken: file src/b.py" in task.latest_result_json["message"]
        for call in orchestrator.tool_gateway.execute.call_args_list:
            tool = call.kwargs.get("tool_name") or (call.args[0] if call.args else "")
            assert tool != "jira.transition_issue"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_warn_only_findings_continue_to_approval_payload() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        claims = [
            {"text": "Supported 1", "citation_indices": [0], "confidence": "high"},
            {"text": "Supported 2", "citation_indices": [0], "confidence": "high"},
            {"text": "Supported 3", "citation_indices": [0], "confidence": "high"},
            {"text": "Unsupported aside", "citation_indices": [], "confidence": "low"},
        ]

        added = _run_pipeline(
            orchestrator,
            task,
            plan,
            _codegen_result("src/a.py", claims=claims),
        )

        assert task.latest_result_json["status"] == TaskStatus.AWAITING_APPROVAL.value
        payload = added[0].request_payload_json
        warnings = payload["evidence_chain"]["warnings"]
        assert payload["evidence_chain"]["closed"] is True
        assert any(warning["rule"] == "ungrounded_claims" for warning in warnings)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_disabled_gate_is_skipped_for_legacy_behavior() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root, gate_enabled=False)
        task = _task(request_text="Update src/b.py for TEST-1.")
        plan = _plan(task.id)

        added = _run_pipeline(orchestrator, task, plan, _codegen_result("src/b.py"))

        assert task.latest_result_json["status"] == TaskStatus.AWAITING_APPROVAL.value
        assert len(added) == 1
        assert added[0].request_payload_json["evidence_chain"]["closed"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)
