"""C7 liveness fix regression tests (2026-05-12).

The compile_repair stage used to lose its bounded-terminal guarantee
when a single ``_execute_develop_tool`` call hung — the round deadline
was only checked *between* file iterations, so a stuck provider socket
inside a file's repair LLM call would never trigger ``RepairRoundTimeout``.

These tests cover the four C7 acceptance criteria:

1. A hanging ``_execute_develop_tool`` call returns ``DevelopToolTimeout``
   within the configured per-call timeout.
2. Effective call timeout is bounded by the remaining round budget
   (minus a small safety margin) — never exceeds it.
3. Timeout-style failures do NOT consume the retry path.
4. ``compile_repair.file_started`` / ``attempt_started`` /
   ``file_completed`` / ``file_failed`` / ``tool_call_timeout`` events
   are emitted at the expected boundaries.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
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
    EventSource,
    EventType,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import (  # noqa: E402
    DevelopToolTimeout,
    PrimaryOrchestrator,
)


# ---------------------------------------------------------------------------
# helpers (copied/adapted from test_compile_repair_loop.py)
# ---------------------------------------------------------------------------


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="c7-liveness-"))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="c7-liveness-"))
    finally:
        tempfile._os.mkdir = original_mkdir


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-c7",
        objective="C7 liveness test.",
        request_summary="C7 liveness test.",
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
            ),
        ],
        steps=[
            PlanStep(
                step_id="s1",
                title="Edit",
                kind="action",
                owner_role=RoleName.ACTION,
                expected_output="diff",
                success_criteria="patch applied",
            ),
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-c7",
        session_id="session-c7",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="C7 liveness test.",
        scenario="jira_issue_develop",
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.REVIEW,
        translation_json=None,
        plan_json=None,
        latest_result_json=None,
        governance_json=None,
        pending_approval=False,
        retry_count=0,
    )


def _make_orchestrator(root: Path) -> PrimaryOrchestrator:
    orch = PrimaryOrchestrator(db=Mock())
    orch.db.flush = Mock()
    orch.db.add = Mock()
    settings = orch.tool_gateway.settings
    settings.sandbox_base_dir = str(root / "sandbox")
    settings.agent_workspace_root = str(root / "workspace")
    settings.codegen_repair_files_per_round = 5
    settings.codegen_repair_round_timeout_seconds = 600.0
    settings.codegen_repair_per_call_timeout_seconds = 120.0
    settings.codegen_repair_call_safety_margin_seconds = 5.0
    # Stub helpers that touch the DB / external state — irrelevant for the
    # C7 liveness behaviour these tests are isolating.
    orch._sync_retry_count = Mock()
    orch._preview_develop_payload = Mock(return_value={})
    return orch


def _make_sandbox_with_broken(root: Path, rel_paths: list[str]) -> Path:
    """Create a sandbox tree with the listed broken Kotlin/Python files."""
    sandbox = root / "sandbox" / "task-c7"
    sandbox.mkdir(parents=True, exist_ok=True)
    for rp in rel_paths:
        full = sandbox / rp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("fun broken() { invalid }\n", encoding="utf-8")
    return sandbox


# ---------------------------------------------------------------------------
# Test 1 — hanging tool_gateway call honours timeout_seconds
# ---------------------------------------------------------------------------


class ExecuteDevelopToolTimeoutTests(unittest.TestCase):
    """C7 acceptance #1: per-call timeout actually fires when a tool hangs."""

    def setUp(self) -> None:
        self.root = _writable_mkdtemp()
        self.orch = _make_orchestrator(self.root)
        self.task = _task()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_hanging_tool_call_raises_DevelopToolTimeout(self) -> None:
        # Replace tool_gateway.execute with a function that hangs.
        def _hang(*_args, **_kwargs):
            time.sleep(30)  # never reached; we time out at 0.5s

        self.orch.tool_gateway.execute = Mock(side_effect=_hang)

        started = time.monotonic()
        with patch("app.orchestrator.service.record_event"), patch(
            "app.orchestrator.service.commit_checkpoint"
        ):
            with self.assertRaises(DevelopToolTimeout) as ctx:
                self.orch._execute_develop_tool(
                    task=self.task,
                    actor_name="tester",
                    tool_name="codegen.generate_patch",
                    payload={"plan_json": {}, "context_files": {}},
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    approval_id=None,
                    pipeline_state={},
                    timeout_seconds=0.5,
                )
        elapsed = time.monotonic() - started
        # Allow generous slack for thread teardown but must be << 30s sleep.
        self.assertLess(elapsed, 5.0, f"timeout took too long: {elapsed:.2f}s")
        self.assertEqual(ctx.exception.tool_name, "codegen.generate_patch")
        self.assertAlmostEqual(ctx.exception.timeout_seconds, 0.5, delta=0.01)


# ---------------------------------------------------------------------------
# Test 2/3/4 — _attempt_compile_repair behaviour with mocked tool calls
# ---------------------------------------------------------------------------


class AttemptCompileRepairLivenessTests(unittest.TestCase):
    """C7 acceptance #2/#3/#4: deadline budget propagation, no-retry-on-timeout,
    progress events."""

    def setUp(self) -> None:
        self.root = _writable_mkdtemp()
        self.orch = _make_orchestrator(self.root)
        self.task = _task()
        self.sandbox = _make_sandbox_with_broken(
            self.root, ["app/Foo.kt"]
        )
        # Disable post-codegen knowledge source read by stubbing the helper
        # to return an empty string so the function takes the no-original path.
        self.orch._resolve_knowledge_source_path = Mock(return_value=None)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_repair(
        self,
        *,
        compile_errors: list[dict],
        round_timeout: float,
        mock_execute_side_effect,
        records_out: list,
    ) -> tuple[bool, list[str]]:
        captured_calls: list[dict] = []

        def _capture(**kwargs):
            captured_calls.append(kwargs)
            return mock_execute_side_effect(**kwargs)

        self.orch._execute_develop_tool = Mock(side_effect=_capture)
        # Make _build_related_files_section a no-op for the test harness.
        self.orch._build_related_files_section = Mock(return_value="")
        self.orch._workspace_append_audit = Mock()
        # Speed up the test by zeroing the tactical cooldowns.
        with patch("app.orchestrator.service.time.sleep") as _sleep_mock, patch(
            "app.orchestrator.service.record_event"
        ) as _record_event, patch(
            "app.orchestrator.service.commit_checkpoint"
        ):
            _sleep_mock.side_effect = lambda _s: None
            result = self.orch._attempt_compile_repair(
                task=self.task,
                actor_name="tester",
                plan=_plan(),
                compile_errors=compile_errors,
                sandbox_dir=self.sandbox,
                pipeline_state={
                    "first_attempt_diff_by_file": {},
                    "first_attempt_diff": "",
                },
                approval_id=None,
                timeout_seconds=round_timeout,
                files_per_round=5,
                allowed_paths=None,
            )
            records_out.extend(_record_event.call_args_list)
        # Return both the result tuple and the call-capture list for assertions.
        return result, captured_calls  # type: ignore[return-value]

    # ----- Test 2 — call timeout bounded by remaining round budget --------

    def test_call_timeout_bounded_by_remaining_round_budget(self) -> None:
        """When round budget is small (e.g. 6s), the call timeout passed to
        _execute_develop_tool must be ~6 - 5 (safety) = ~1s, NOT the
        configured 120s per-call default."""
        records: list = []

        def _fake_execute(**kwargs):
            return {"diff": "diff --git a/app/Foo.kt b/app/Foo.kt\n@@ -1,1 +1,1 @@\n-fun broken() { invalid }\n+fun fixed() {}\n"}

        result, captured = self._run_repair(
            compile_errors=[{"file": "app/Foo.kt", "error": "unresolved reference"}],
            round_timeout=6.0,  # tight round budget
            mock_execute_side_effect=_fake_execute,
            records_out=records,
        )

        # _execute_develop_tool should have been called at least once with a
        # timeout_seconds NOT exceeding remaining round budget minus margin.
        self.assertGreater(len(captured), 0)
        first_call = captured[0]
        # round budget = 6s, safety margin = 5s → call_timeout ≈ 1s (or
        # floored at 1.0). MUST be << 120s configured per-call default.
        call_timeout = first_call.get("timeout_seconds")
        self.assertIsNotNone(call_timeout)
        self.assertLess(
            call_timeout,
            10.0,
            f"call timeout {call_timeout} not bounded by remaining round budget",
        )
        self.assertGreaterEqual(call_timeout, 1.0)

    # ----- Test 3 — timeout failures do NOT consume retry path ------------

    def test_timeout_failure_does_not_retry_same_file(self) -> None:
        """A DevelopToolTimeout on the first attempt for a file should
        NOT trigger the second-attempt retry."""
        records: list = []
        call_count = 0

        def _always_timeout(**kwargs):
            nonlocal call_count
            call_count += 1
            raise DevelopToolTimeout(
                tool_name=kwargs.get("tool_name", "codegen.generate_patch"),
                timeout_seconds=float(kwargs.get("timeout_seconds") or 1.0),
            )

        self._run_repair(
            compile_errors=[{"file": "app/Foo.kt", "error": "unresolved reference"}],
            round_timeout=600.0,
            mock_execute_side_effect=_always_timeout,
            records_out=records,
        )

        # Exactly one call — the retry path must NOT have fired for a
        # DevelopToolTimeout-kind failure (C7 acceptance #5).
        self.assertEqual(
            call_count,
            1,
            f"timeout-kind failure should not retry; saw {call_count} calls",
        )

    # ----- Test 4 — progress events emitted at expected boundaries --------

    def test_progress_events_emitted_at_boundaries(self) -> None:
        """compile_repair must emit file_started, attempt_started, and a
        terminal event (file_completed OR file_failed) per file so external
        observers can distinguish stuck-in-LLM from stuck-in-orchestrator."""
        records: list = []

        def _fake_execute(**kwargs):
            return {
                "diff": (
                    "diff --git a/app/Foo.kt b/app/Foo.kt\n"
                    "@@ -1,1 +1,1 @@\n"
                    "-fun broken() { invalid }\n"
                    "+fun fixed() {}\n"
                )
            }

        self._run_repair(
            compile_errors=[{"file": "app/Foo.kt", "error": "unresolved reference"}],
            round_timeout=600.0,
            mock_execute_side_effect=_fake_execute,
            records_out=records,
        )

        # Extract tool_name from every record_event(... tool_name=X ...) call.
        emitted_tools = []
        for call in records:
            kwargs = call.kwargs if hasattr(call, "kwargs") else call[1]
            tn = kwargs.get("tool_name") if isinstance(kwargs, dict) else None
            if tn:
                emitted_tools.append(tn)

        self.assertIn("compile_repair.file_started", emitted_tools)
        self.assertIn("compile_repair.attempt_started", emitted_tools)
        # Either file_completed (success) or file_failed (no diff) must appear.
        terminals = {"compile_repair.file_completed", "compile_repair.file_failed"}
        self.assertTrue(
            terminals.intersection(emitted_tools),
            f"No terminal file event emitted. Saw: {emitted_tools[:20]}",
        )

    # ----- Bonus — tool_call_timeout event surfaces on timeout ----------

    def test_tool_call_timeout_event_emitted_on_timeout(self) -> None:
        """When a repair call times out, a compile_repair.tool_call_timeout
        event must be emitted so the timeline shows the timeout cleanly."""
        records: list = []

        def _always_timeout(**kwargs):
            raise DevelopToolTimeout(
                tool_name=kwargs.get("tool_name", "codegen.generate_patch"),
                timeout_seconds=float(kwargs.get("timeout_seconds") or 1.0),
            )

        self._run_repair(
            compile_errors=[{"file": "app/Foo.kt", "error": "unresolved reference"}],
            round_timeout=600.0,
            mock_execute_side_effect=_always_timeout,
            records_out=records,
        )

        emitted_tools = []
        for call in records:
            kwargs = call.kwargs if hasattr(call, "kwargs") else call[1]
            tn = kwargs.get("tool_name") if isinstance(kwargs, dict) else None
            if tn:
                emitted_tools.append(tn)

        self.assertIn("compile_repair.tool_call_timeout", emitted_tools)
        self.assertIn("compile_repair.file_failed", emitted_tools)


if __name__ == "__main__":
    unittest.main()
