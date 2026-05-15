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
from app.orchestrator.service import (
    PrimaryOrchestrator,
    _backfill_plan_targets_from_candidate_mentions,
    _filter_reservations_for_verified_contracts,
)
from app.schemas.evidence import EvidenceItem
from app.services.semantic_review import SemanticReviewReport
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
        source_name="test",
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


def _semantic_review_pass() -> SemanticReviewReport:
    return SemanticReviewReport(
        passed=True,
        completeness_pct=100,
        summary="Mock semantic review passed.",
        findings=(),
        pass_threshold=80,
        total_findings_raw=0,
        findings_dropped_no_evidence=0,
        provider_name="test",
    )


def _semantic_review_fail() -> SimpleNamespace:
    finding = SimpleNamespace(
        severity="high",
        file="src/a.py",
        description="The patch does not implement the requested behavior.",
        evidence_quote="+new",
        suggested_fix="Add the missing executable behavior.",
    )
    return SimpleNamespace(
        passed=False,
        completeness_pct=20,
        pass_threshold=80,
        findings=[finding],
        findings_dropped_no_evidence=0,
        high_severity_count=lambda: 1,
        repair_prompt_lines=lambda: [
            "- [HIGH] src/a.py: add the missing executable behavior."
        ],
        to_payload=lambda: {
            "passed": False,
            "completeness_pct": 20,
            "high_count": 1,
            "findings": [
                {
                    "severity": "high",
                    "file": "src/a.py",
                    "description": finding.description,
                }
            ],
        },
    )


def _runtime_report(*, passed: bool, findings: list[object] | None = None) -> SimpleNamespace:
    findings = findings or []
    return SimpleNamespace(
        passed=passed,
        findings=findings,
        summary=lambda: "Runtime validation passed" if passed else "Runtime validation failed",
        to_payload=lambda: {
            "passed": passed,
            "blocking_count": 0 if passed else len(findings),
            "findings": [
                {
                    "severity": getattr(item, "severity", "error"),
                    "file": getattr(item, "file", ""),
                    "message": getattr(item, "message", ""),
                }
                for item in findings
            ],
        },
    )


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
    settings.sandbox_external_root = ""
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
    reservations_report: object | None = None,
) -> list[object]:
    added: list[object] = []
    if reservations_report is None:
        reservations_report = SimpleNamespace(
            reservations=[],
            to_dicts=lambda: [],
            auto_fixable=[],
            blocking=[],
            provider="test",
            model="none",
        )
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
        return_value=reservations_report,
    ), patch(
        "app.services.semantic_review.evaluate_semantic_review",
        return_value=_semantic_review_pass(),
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


def test_plan_target_backfill_promotes_evidence_mentioned_files() -> None:
    plan = _plan()
    plan.must_touch_files = []
    plan.expected_new_files = []
    plan.steps[0].expected_output = (
        "Modify CustomerKYCPhoneNumber.kt and HandymanKYCPhoneNumber.kt."
    )
    plan.acceptance_tests = [
        {
            "kind": "diff_contains_pattern_in_file",
            "file": (
                "app/src/main/java/com/example/handyman/customer_pages/"
                "CustomerKYCPhoneNumber.kt"
            ),
            "pattern": "setValue",
        }
    ]
    candidates = [
        {
            "path": (
                "app/src/main/java/com/example/handyman/customer_pages/"
                "CustomerKYCPhoneNumber.kt"
            )
        },
        {
            "path": (
                "app/src/main/java/com/example/handyman/handyman_pages/"
                "HandymanKYCPhoneNumber.kt"
            )
        },
        {"path": "app/src/test/java/com/example/handyman/PhoneTest.kt"},
    ]

    assert _backfill_plan_targets_from_candidate_mentions(plan, candidates) == [
        (
            "app/src/main/java/com/example/handyman/customer_pages/"
            "CustomerKYCPhoneNumber.kt"
        ),
        (
            "app/src/main/java/com/example/handyman/handyman_pages/"
            "HandymanKYCPhoneNumber.kt"
        ),
    ]


def test_blocking_reservations_fail_before_jira_transition_approval() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        blocking_item = {
            "text": "No rate limit protects repeated OTP sends.",
            "severity": "security",
            "auto_fixable": False,
            "blocking": True,
        }
        report = SimpleNamespace(
            reservations=[blocking_item["text"]],
            to_dicts=lambda: [blocking_item],
            auto_fixable=[],
            blocking=[blocking_item],
            provider="test",
            model="none",
        )

        added = _run_pipeline(
            orchestrator,
            task,
            plan,
            _codegen_result("src/a.py"),
            reservations_report=report,
        )

        approvals = [obj for obj in added if hasattr(obj, "action_name")]
        assert approvals == []
        assert task.pending_approval is False
        assert task.latest_result_json["status"] == TaskStatus.FAILED.value
        assert task.latest_result_json["decision"] == "reservation_blocked"
        assert task.latest_result_json["blocking_reservations"] == [blocking_item]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_verified_phone_otp_contract_filters_contradictory_reservations() -> None:
    plan_json = {
        "domain_playbook_id": "android_phone_otp_reverification",
        "acceptance_tests": [
            {
                "kind": "final_file_forbids_pattern_in_file",
                "contract_id": "customer_no_preverification_phone_write",
                "file": (
                    "app/src/main/java/com/example/handyman/customer_pages/"
                    "CustomerKYCPhoneNumber.kt"
                ),
                "pattern": r'child\("phoneNumber"\)\.setValue\s*\(',
            }
        ],
    }
    pipeline_state = {
        "acceptance_check_done": True,
        "compile_gate": {"passed": True},
    }
    bug_item = {
        "text": (
            "Phone number is no longer saved to database before OTP screen - "
            "if OTP screen relies on DB value, this will cause missing data."
        ),
        "severity": "bug",
        "auto_fixable": True,
        "blocking": False,
    }
    policy_item = {
        "text": (
            "This change likely does not fix the stated problem. Firebase OTP "
            "rate limiting for the same phone number is enforced server-side "
            "by Firebase Auth; removing the DB write will not change Firebase's "
            "rate limit behavior."
        ),
        "severity": "policy",
        "auto_fixable": False,
        "blocking": True,
    }
    actual_rate_limit_policy_item = {
        "text": (
            "The removed pre-verification DB write likely triggered Firebase "
            "rate limiting (too many writes for same phone); ensure this fix "
            "truly unblocks resend vs. masking a deeper auth config issue."
        ),
        "severity": "policy",
        "auto_fixable": False,
        "blocking": True,
    }
    speculative_policy_item = {
        "text": (
            "If the real fix requires something else, such as configuring "
            "Firebase project settings, using different PhoneAuthOptions, or "
            "handling resend tokens, this diff silently discards data persistence."
        ),
        "severity": "policy",
        "auto_fixable": False,
        "blocking": True,
    }
    speculative_security_item = {
        "text": (
            "Error handling is removed: the old code navigated to OTP even on "
            "DB write failure. If the DB write had side effects, removing it "
            "silently changes the security surface."
        ),
        "severity": "security",
        "auto_fixable": False,
        "blocking": True,
    }
    verified_after_otp_policy_item = {
        "text": (
            "Phone number is only saved to DB after successful OTP verification. "
            "If user abandons flow after receiving code, the number is lost and "
            "they must re-enter it."
        ),
        "severity": "policy",
        "auto_fixable": False,
        "blocking": True,
    }
    preexisting_route_security_item = {
        "text": (
            "VerificationId and phoneNumber are passed as URL path parameters "
            "to the OTP screen - visible in app backstack and server logs."
        ),
        "severity": "security",
        "auto_fixable": False,
        "blocking": True,
    }
    missing_test_item = {
        "text": "No tests were added for the OTP flow.",
        "severity": "missing_test",
        "auto_fixable": True,
        "blocking": False,
    }
    style_item = {
        "text": "Indentation in CustomerKYCPhoneNumber.kt is uneven.",
        "severity": "style",
        "auto_fixable": True,
        "blocking": False,
    }

    kept, suppressed = _filter_reservations_for_verified_contracts(
        [
            bug_item,
            policy_item,
            actual_rate_limit_policy_item,
            speculative_policy_item,
            speculative_security_item,
            verified_after_otp_policy_item,
            preexisting_route_security_item,
            missing_test_item,
            style_item,
        ],
        plan_json=plan_json,
        pipeline_state=pipeline_state,
    )

    assert [item["text"] for item in suppressed] == [
        bug_item["text"],
        policy_item["text"],
        actual_rate_limit_policy_item["text"],
        speculative_policy_item["text"],
        speculative_security_item["text"],
        verified_after_otp_policy_item["text"],
        preexisting_route_security_item["text"],
    ]
    assert {item["suppressed_reason"] for item in suppressed} == {
        "contradicts_verified_phone_otp_contract"
    }
    assert [item["text"] for item in kept] == [
        missing_test_item["text"],
        style_item["text"],
    ]
    assert kept[0]["severity"] == "style"
    assert kept[0]["downgraded_reason"] == "structural_acceptance_tests_passed"


def test_goal_miss_blocking_reservation_runs_repair_before_approval() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        orchestrator._build_develop_sandbox = Mock(
            return_value=SimpleNamespace(snapshot_id=lambda: "git:abc1234")
        )
        task = _task(
            request_text=(
                "Remove phone OTP one-time-use limit while preserving the "
                "existing auth and database update flow."
            )
        )
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        sandbox_file = root / "sandbox" / task.id / "src" / "a.py"
        sandbox_file.parent.mkdir(parents=True, exist_ok=True)
        sandbox_file.write_text("old\n", encoding="utf-8")

        blocking_goal_miss = {
            "text": (
                "The diff description claims to remove OTP one-time-use limit "
                "but no such logic is visible in the diff. The changes add "
                "auth.signOut() and reorder navigation vs DB update - this "
                "does not clearly address the stated goal."
            ),
            "severity": "policy",
            "auto_fixable": False,
            "blocking": True,
        }
        first_report = SimpleNamespace(
            reservations=[blocking_goal_miss["text"]],
            to_dicts=lambda: [blocking_goal_miss],
            auto_fixable=[],
            blocking=[blocking_goal_miss],
            provider="test",
            model="none",
        )
        empty_report = SimpleNamespace(
            reservations=[],
            to_dicts=lambda: [],
            auto_fixable=[],
            blocking=[],
            provider="test",
            model="none",
        )
        repair_diff = _diff("src/a.py").replace("+new", "+newer")
        added: list[object] = []
        orchestrator.db.add = Mock(
            side_effect=lambda obj: (
                added.append(obj),
                setattr(obj, "id", f"approval-{len(added)}"),
            )[0]
        )
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result("src/a.py"),
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                _review_pass(),
                {"diff": repair_diff, "files_changed": ["src/a.py"]},
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                _review_pass(),
            ]
        )

        with patch("app.orchestrator.service.record_event") as record_event, patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.reservations.build_reservations",
            side_effect=[first_report, empty_report],
        ), patch(
            "app.services.semantic_review.evaluate_semantic_review",
            return_value=_semantic_review_pass(),
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            return_value=_runtime_report(passed=True),
        ):
            orchestrator._execute_develop_pipeline(
                task=task,
                actor_name="tester",
                plan=plan,
            )

        apply_calls = [
            call
            for call in orchestrator.tool_gateway.execute.call_args_list
            if call.kwargs.get("tool_name") == "sandbox.apply_patch"
        ]
        tool_names = [
            call.kwargs.get("tool_name")
            for call in record_event.call_args_list
            if "tool_name" in call.kwargs
        ]
        approvals = [obj for obj in added if hasattr(obj, "action_name")]

        assert len(apply_calls) == 2
        assert "reservations.repair" in tool_names
        assert approvals
        assert task.latest_result_json["status"] == TaskStatus.AWAITING_APPROVAL.value
        assert task.latest_result_json["pipeline_state"][
            "reservation_repair_applied"
        ] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repairable_reservations_fail_when_repair_budget_disabled() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        orchestrator.tool_gateway.settings.reservation_repair_enabled = False
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        repairable_item = {
            "text": "The diff only adds a comment and no behavior change.",
            "severity": "bug",
            "auto_fixable": True,
            "blocking": False,
        }
        report = SimpleNamespace(
            reservations=[repairable_item["text"]],
            to_dicts=lambda: [repairable_item],
            auto_fixable=[repairable_item],
            blocking=[],
            provider="test",
            model="none",
        )

        added = _run_pipeline(
            orchestrator,
            task,
            plan,
            _codegen_result("src/a.py"),
            reservations_report=report,
        )

        approvals = [obj for obj in added if hasattr(obj, "action_name")]
        assert approvals == []
        assert task.pending_approval is False
        assert task.latest_result_json["status"] == TaskStatus.FAILED.value
        assert task.latest_result_json["decision"] == "reservation_repair_unresolved"
        assert task.latest_result_json["repairable_reservations"] == [repairable_item]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_nonblocking_executable_reservation_does_not_auto_repair() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        advisory_item = {
            "text": (
                "BUG: The removed DB write may have served a secondary purpose; "
                "verify downstream logic still handles any dependent state."
            ),
            "severity": "bug",
            "auto_fixable": True,
            "blocking": False,
        }
        report = SimpleNamespace(
            reservations=[advisory_item["text"]],
            to_dicts=lambda: [advisory_item],
            auto_fixable=[advisory_item],
            blocking=[],
            provider="test",
            model="none",
        )

        added = _run_pipeline(
            orchestrator,
            task,
            plan,
            _codegen_result("src/a.py"),
            reservations_report=report,
        )

        tool_names = [
            call.kwargs.get("tool_name")
            for call in task._record_event_calls
            if "tool_name" in call.kwargs
        ]
        approvals = [obj for obj in added if hasattr(obj, "action_name")]

        assert "reservations.repair" not in tool_names
        assert approvals
        assert task.latest_result_json["status"] == TaskStatus.AWAITING_APPROVAL.value
        gate = task.latest_result_json["pipeline_state"]["reservation_gate"]
        assert gate["repairable_count"] == 1
        assert gate["required_repair_count"] == 0
        assert gate["advisory_repairable_count"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_runtime_validation_repair_is_applied_before_approval() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        orchestrator.tool_gateway.settings.runtime_validation_repair_cooldown_seconds = 0
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        added: list[object] = []
        orchestrator.db.add = Mock(
            side_effect=lambda obj: (
                added.append(obj),
                setattr(obj, "id", f"approval-{len(added)}"),
            )[0]
        )
        repair_diff = _diff("src/a.py").replace("+new", "+newer")
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result("src/a.py"),
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                {"diff": repair_diff, "files_changed": ["src/a.py"]},
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                _review_pass(),
            ]
        )
        finding = SimpleNamespace(
            severity="error",
            file="src/a.py",
            message="Generated patch does not satisfy runtime semantics.",
        )
        with patch("app.orchestrator.service.record_event") as record_event, patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.reservations.build_reservations",
            return_value=SimpleNamespace(
                reservations=[],
                to_dicts=lambda: [],
                auto_fixable=[],
                blocking=[],
                provider="test",
                model="none",
            ),
        ), patch(
            "app.services.semantic_review.evaluate_semantic_review",
            return_value=_semantic_review_pass(),
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            side_effect=[
                _runtime_report(passed=False, findings=[finding]),
                _runtime_report(passed=True),
            ],
        ), patch(
            "app.services.runtime_validation.build_repair_prompt",
            return_value="Fix runtime validation issue in src/a.py.",
        ):
            orchestrator._execute_develop_pipeline(
                task=task,
                actor_name="tester",
                plan=plan,
            )

        tool_names = [
            call.kwargs.get("tool_name")
            for call in record_event.call_args_list
            if "tool_name" in call.kwargs
        ]
        apply_calls = [
            call
            for call in orchestrator.tool_gateway.execute.call_args_list
            if call.kwargs.get("tool_name") == "sandbox.apply_patch"
        ]
        approvals = [obj for obj in added if hasattr(obj, "action_name")]

        assert len(apply_calls) == 2
        assert "runtime_validation.repair" in tool_names
        assert task.latest_result_json["pipeline_state"][
            "runtime_validation_repair_applied"
        ] is True
        assert task.latest_result_json["pipeline_state"]["runtime_validation"][
            "passed"
        ] is True
        assert approvals
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_semantic_review_repair_is_applied_before_reverification() -> None:
    root = _writable_mkdtemp()
    try:
        orchestrator = _orchestrator(root)
        orchestrator.tool_gateway.settings.semantic_review_repair_cooldown_seconds = 0
        task = _task()
        plan = _plan(task.id)
        _write_workspace(orchestrator, task, evidence_path="src/a.py")
        added: list[object] = []
        orchestrator.db.add = Mock(
            side_effect=lambda obj: (
                added.append(obj),
                setattr(obj, "id", f"approval-{len(added)}"),
            )[0]
        )
        repair_diff = _diff("src/a.py").replace("+new", "+newer")
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                _codegen_result("src/a.py"),
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                {"diff": repair_diff, "files_changed": ["src/a.py"]},
                {"status": "patched", "method": "git_apply"},
                _test_pass(),
                _review_pass(),
            ]
        )
        with patch("app.orchestrator.service.record_event") as record_event, patch(
            "app.orchestrator.service.set_task_status"
        ), patch(
            "app.services.reservations.build_reservations",
            return_value=SimpleNamespace(
                reservations=[],
                to_dicts=lambda: [],
                auto_fixable=[],
                blocking=[],
                provider="test",
                model="none",
            ),
        ), patch(
            "app.services.semantic_review.evaluate_semantic_review",
            side_effect=[_semantic_review_fail(), _semantic_review_pass()],
        ), patch(
            "app.services.runtime_validation.validate_diff_semantics",
            return_value=_runtime_report(passed=True),
        ), patch(
            "app.orchestrator.service._semantic_review_verified_gates_passed",
            return_value=False,
        ):
            orchestrator._execute_develop_pipeline(
                task=task,
                actor_name="tester",
                plan=plan,
            )

        tool_names = [
            call.kwargs.get("tool_name")
            for call in record_event.call_args_list
            if "tool_name" in call.kwargs
        ]
        apply_calls = [
            call
            for call in orchestrator.tool_gateway.execute.call_args_list
            if call.kwargs.get("tool_name") == "sandbox.apply_patch"
        ]
        approvals = [obj for obj in added if hasattr(obj, "action_name")]

        assert len(apply_calls) == 2
        assert "semantic_review.repair" in tool_names
        assert task.latest_result_json["pipeline_state"][
            "semantic_review_repair_applied"
        ] is True
        assert task.latest_result_json["pipeline_state"]["semantic_review"][
            "passed"
        ] is True
        assert approvals
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

        approvals = [obj for obj in added if hasattr(obj, "action_name")]
        assert approvals == []
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
