"""Tests for the multi-round compile_gate repair loop (T-PIPELINE-REPAIR-CAP).

Each test mocks ``run_compile_gate`` and ``_attempt_compile_repair`` to
drive the loop deterministically without spinning up a real sandbox or
codegen subprocess.
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
from app.orchestrator.service import (  # noqa: E402
    PrimaryOrchestrator,
    RepairRoundTimeout,
)
from app.services.compile_gate import CompileResult  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="compile-repair-loop-"))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="compile-repair-loop-"))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-cap",
        objective="Implement OPS-CAP.",
        request_summary="Implement OPS-CAP.",
        scenario="jira_issue_develop",
        change_summary="Edit listed files.",
        change_explanation="Edit listed files.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[],
        must_touch_files=[],
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


def _task(task_id: str = "task-cap") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        session_id=f"session-{task_id}",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="Implement OPS-CAP.",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json=None,
        plan_json=None,
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


def _err(file: str, n: int = 1) -> dict:
    return {"file": file, "type": "js", "error": f"[stdin]:{n} syntax error"}


def _make_orchestrator(root: Path, **overrides) -> PrimaryOrchestrator:
    sandbox_root = root / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    orchestrator = PrimaryOrchestrator(db=Mock())
    settings = orchestrator.tool_gateway.settings
    settings.sandbox_base_dir = str(sandbox_root)
    settings.agent_workspace_root = str(root / "workspace")
    settings.codegen_max_repair_rounds = overrides.get("max_rounds", 3)
    settings.codegen_repair_files_per_round = overrides.get("files_per_round", 5)
    settings.codegen_repair_round_timeout_seconds = overrides.get("round_timeout", 180.0)
    settings.codegen_repair_cap_exceeded_to_approval = overrides.get(
        "to_approval", True
    )
    settings.evidence_chain_gate_enabled = False
    settings.develop_require_jira_approval = False
    settings.minimax_api_key = None
    orchestrator.db.flush = Mock()
    return orchestrator


def _ensure_sandbox(orchestrator: PrimaryOrchestrator, task: SimpleNamespace) -> None:
    sandbox_dir = orchestrator._develop_sandbox_dir(task)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    # touch one file so the sandbox-exists check passes
    (sandbox_dir / "src").mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "src" / "a.js").write_text("// stub\n", encoding="utf-8")


def _drive(
    orchestrator: PrimaryOrchestrator,
    task: SimpleNamespace,
    plan: GeneratedPlan,
    pipeline_state: dict,
    compile_results: list[CompileResult],
    repair_side_effect,
    record_event_calls: list | None = None,
) -> tuple[str, list]:
    """Invoke the loop with patched compile_gate + repair, return (outcome, added_approvals)."""
    added: list = []
    orchestrator.db.add = Mock(
        side_effect=lambda obj: (added.append(obj), setattr(obj, "id", f"ap-{len(added)}"))[0]
    )
    orchestrator._attempt_compile_repair = Mock(side_effect=repair_side_effect)

    with patch(
        "app.services.compile_gate.run_compile_gate",
        side_effect=compile_results,
    ), patch("app.orchestrator.service.record_event") as record_event_mock, patch(
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
    if record_event_calls is not None:
        record_event_calls.extend(record_event_mock.call_args_list)
    return outcome, added


class CompileRepairLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _writable_mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # --- 1. Zero errors → no repair rounds run --------------------------- #

    def test_zero_errors_no_rounds_run(self) -> None:
        orchestrator = _make_orchestrator(self.root)
        task = _task()
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js"]}

        outcome, added = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=[CompileResult(passed=True, errors=[])],
            repair_side_effect=lambda **kw: (False, []),
        )

        self.assertEqual(outcome, "passed")
        self.assertEqual(added, [])
        # No round summary entries.
        self.assertEqual(pipeline_state.get("compile_repair_rounds"), [])
        orchestrator._attempt_compile_repair.assert_not_called()

    # --- 2. 3 errors → 1 round → pass (single-round happy path) ---------- #

    def test_three_errors_one_round_then_pass(self) -> None:
        orchestrator = _make_orchestrator(self.root)
        task = _task()
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js", "src/b.js", "src/c.js"]}

        compile_calls = [
            CompileResult(
                passed=False,
                errors=[_err("src/a.js"), _err("src/b.js"), _err("src/c.js")],
            ),
            CompileResult(passed=True, errors=[]),
        ]

        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (
                True,
                ["src/a.js", "src/b.js", "src/c.js"],
            ),
        )

        self.assertEqual(outcome, "passed")
        self.assertEqual(orchestrator._attempt_compile_repair.call_count, 1)
        rounds = pipeline_state["compile_repair_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["round"], 1)
        self.assertEqual(
            sorted(rounds[0]["files_repaired"]),
            ["src/a.js", "src/b.js", "src/c.js"],
        )
        self.assertFalse(rounds[0]["timed_out"])

    # --- 3. 7 errors → 2 rounds → pass (multi-round, fits in budget) ----- #

    def test_seven_errors_two_rounds_then_pass(self) -> None:
        orchestrator = _make_orchestrator(self.root, files_per_round=5)
        task = _task()
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {
            "files_changed": [f"src/f{i}.js" for i in range(7)],
        }

        first_errors = [_err(f"src/f{i}.js") for i in range(7)]
        # After round 1 (fixes first 5), 2 errors remain (f5, f6).
        second_errors = [_err("src/f5.js"), _err("src/f6.js")]

        compile_calls = [
            CompileResult(passed=False, errors=first_errors),
            CompileResult(passed=False, errors=second_errors),
            CompileResult(passed=True, errors=[]),
        ]

        repair_returns = iter(
            [
                (True, [f"src/f{i}.js" for i in range(5)]),
                (True, ["src/f5.js", "src/f6.js"]),
            ]
        )
        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: next(repair_returns),
        )

        self.assertEqual(outcome, "passed")
        self.assertEqual(orchestrator._attempt_compile_repair.call_count, 2)
        rounds = pipeline_state["compile_repair_rounds"]
        self.assertEqual([r["round"] for r in rounds], [1, 2])
        self.assertEqual(len(rounds[0]["files_attempted"]), 5)
        self.assertEqual(len(rounds[1]["files_attempted"]), 2)

    # --- 4. 16 errors → cap exceeded → approval requested ---------------- #

    def test_cap_exceeded_transitions_to_approval(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=3, files_per_round=5)
        task = _task("task-cap-fail")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {
            "files_changed": [f"src/f{i}.js" for i in range(16)],
        }

        # Each round repairs 5 files, leaving the rest.
        all_errors = [_err(f"src/f{i}.js") for i in range(16)]
        compile_calls = [
            CompileResult(passed=False, errors=all_errors[:16]),
            CompileResult(passed=False, errors=all_errors[5:16]),
            CompileResult(passed=False, errors=all_errors[10:16]),
            CompileResult(passed=False, errors=all_errors[15:16]),
        ]
        repair_returns = iter(
            [
                (True, [f"src/f{i}.js" for i in range(5)]),
                (True, [f"src/f{i}.js" for i in range(5, 10)]),
                (True, [f"src/f{i}.js" for i in range(10, 15)]),
            ]
        )

        outcome, added = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: next(repair_returns),
        )

        self.assertEqual(outcome, "approval_requested")
        self.assertEqual(orchestrator._attempt_compile_repair.call_count, 3)
        # One Approval was created.
        self.assertEqual(len(added), 1)
        approval = added[0]
        self.assertEqual(approval.action_name, "compile_repair_cap_exceeded")
        self.assertEqual(approval.status, ApprovalStatus.PENDING)
        payload = approval.request_payload_json
        self.assertEqual(payload["decision"], "compile_repair_cap_exceeded")
        self.assertEqual(payload["rounds_attempted"], 3)
        rs = payload["rounds_summary"]
        # Three repair rounds + one "cap reached" record (4 entries total)
        self.assertGreaterEqual(len(rs), 3)
        self.assertEqual([r["round"] for r in rs[:3]], [1, 2, 3])
        # Residual errors include the un-fixed file.
        residual_files = {e["file"] for e in payload["residual_compile_errors"]}
        self.assertIn("src/f15.js", residual_files)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.AWAITING_APPROVAL.value)
        self.assertTrue(pipeline_state.get("compile_repair_cap_exceeded"))

    # --- 5. Round 1 fixes 5 but introduces 2 NEW errors ------------------ #

    def test_repair_introduces_new_errors_then_resolves(self) -> None:
        orchestrator = _make_orchestrator(self.root)
        task = _task("task-new-errors")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": [f"src/o{i}.js" for i in range(5)]}

        first_errors = [_err(f"src/o{i}.js") for i in range(5)]
        # Round 1 "fixes" the original 5 but introduces two NEW broken files.
        second_errors = [_err("src/new1.js"), _err("src/new2.js")]
        compile_calls = [
            CompileResult(passed=False, errors=first_errors),
            CompileResult(passed=False, errors=second_errors),
            CompileResult(passed=True, errors=[]),
        ]
        repair_returns = iter(
            [
                (True, [f"src/o{i}.js" for i in range(5)] + ["src/new1.js", "src/new2.js"]),
                (True, ["src/new1.js", "src/new2.js"]),
            ]
        )

        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: next(repair_returns),
        )

        self.assertEqual(outcome, "passed")
        self.assertEqual(orchestrator._attempt_compile_repair.call_count, 2)
        rounds = pipeline_state["compile_repair_rounds"]
        self.assertEqual(len(rounds), 2)
        # Round 2 must see the NEW broken files.
        self.assertEqual(
            sorted(rounds[1]["files_attempted"]),
            ["src/new1.js", "src/new2.js"],
        )

    # --- 6. Round 1 hits timeout → counted as failed → round 2 passes ---- #

    def test_round_timeout_counts_as_failed_round(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=3)
        task = _task("task-timeout")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js", "src/b.js"]}

        errors = [_err("src/a.js"), _err("src/b.js")]
        # Round 1 raises; gate still failing afterwards.
        # Round 2 attempts repair successfully → next compile passes.
        compile_calls = [
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=True, errors=[]),
        ]
        sequence = iter(
            [
                RepairRoundTimeout("simulated stall"),
                (True, ["src/a.js", "src/b.js"]),
            ]
        )

        def _side_effect(**kw):
            value = next(sequence)
            if isinstance(value, BaseException):
                raise value
            return value

        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=_side_effect,
        )

        self.assertEqual(outcome, "passed")
        rounds = pipeline_state["compile_repair_rounds"]
        self.assertEqual(len(rounds), 2)
        self.assertTrue(rounds[0]["timed_out"])
        self.assertFalse(rounds[1]["timed_out"])

    # --- 7. Cap exceeded with fail-fast (legacy) behavior ---------------- #

    def test_cap_exceeded_legacy_fail_fast(self) -> None:
        orchestrator = _make_orchestrator(
            self.root, max_rounds=2, to_approval=False
        )
        task = _task("task-legacy")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js"]}

        errors = [_err("src/a.js")]
        compile_calls = [
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
        ]

        outcome, added = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (False, []),
        )

        self.assertEqual(outcome, "failed")
        self.assertEqual(added, [])
        self.assertEqual(task.latest_result_json["status"], TaskStatus.FAILED.value)

    # --- 8. Cap exceeded with default config → AWAITING_APPROVAL --------- #

    def test_cap_exceeded_default_to_approval(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=2)
        task = _task("task-default-approval")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js"]}

        errors = [_err("src/a.js")]
        compile_calls = [
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
        ]

        outcome, added = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (False, []),
        )

        self.assertEqual(outcome, "approval_requested")
        self.assertEqual(len(added), 1)
        self.assertEqual(task.latest_result_json["status"], TaskStatus.AWAITING_APPROVAL.value)
        self.assertEqual(task.latest_result_json["approval_id"], added[0].id)

    # --- 9. Approval payload contains rounds_summary with timings -------- #

    def test_approval_payload_contains_rounds_summary_with_timings(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=2)
        task = _task("task-timings")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js", "src/b.js"]}

        errors = [_err("src/a.js"), _err("src/b.js")]
        compile_calls = [
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
            CompileResult(passed=False, errors=errors),
        ]

        outcome, added = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (False, []),
        )

        self.assertEqual(outcome, "approval_requested")
        approval = added[0]
        payload = approval.request_payload_json
        rs = payload["rounds_summary"]
        for entry in rs:
            self.assertIn("round", entry)
            self.assertIn("duration_seconds", entry)
            self.assertIn("files_attempted", entry)
            self.assertIn("files_repaired", entry)
        # Diff path is the sandbox dir.
        self.assertTrue(payload["diff_path"].endswith(task.id))
        self.assertIn("compile cleanly", payload["message"])

    # --- 10. Workspace audit log records round_started + round_completed - #

    def test_audit_log_records_round_lifecycle(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=1)
        task = _task("task-audit")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js"]}

        # Track audit calls
        audit_calls: list[tuple[str, dict]] = []
        original_audit = orchestrator._workspace_append_audit

        def _capture_audit(task_arg, event_name, payload_arg):
            audit_calls.append((event_name, payload_arg))
            # Skip writing to disk — db is Mock, workspace is unreliable.

        orchestrator._workspace_append_audit = _capture_audit
        # Bypass workspace compile attempt write (touches disk).
        orchestrator._workspace_write_attempt_compile = Mock()

        compile_calls = [
            CompileResult(passed=False, errors=[_err("src/a.js")]),
            CompileResult(passed=True, errors=[]),
        ]
        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (True, ["src/a.js"]),
        )

        self.assertEqual(outcome, "passed")
        names = [c[0] for c in audit_calls]
        self.assertIn("compile_repair.round_started", names)
        self.assertIn("compile_repair.round_completed", names)
        # Restore (paranoia)
        orchestrator._workspace_append_audit = original_audit

    # --- Bonus: emits compile_repair.round_started/completed events ------ #

    def test_round_lifecycle_events_emitted(self) -> None:
        orchestrator = _make_orchestrator(self.root, max_rounds=1)
        task = _task("task-events")
        _ensure_sandbox(orchestrator, task)
        plan = _plan()
        pipeline_state: dict = {"files_changed": ["src/a.js"]}

        compile_calls = [
            CompileResult(passed=False, errors=[_err("src/a.js")]),
            CompileResult(passed=True, errors=[]),
        ]
        record_event_calls: list = []
        outcome, _ = _drive(
            orchestrator,
            task,
            plan,
            pipeline_state,
            compile_results=compile_calls,
            repair_side_effect=lambda **kw: (True, ["src/a.js"]),
            record_event_calls=record_event_calls,
        )
        self.assertEqual(outcome, "passed")
        tool_names = [
            c.kwargs.get("tool_name") for c in record_event_calls if c.kwargs
        ]
        self.assertIn("compile_repair.round_started", tool_names)
        self.assertIn("compile_repair.round_completed", tool_names)


if __name__ == "__main__":
    unittest.main()
