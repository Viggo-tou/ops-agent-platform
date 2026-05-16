"""T-LEARNING-LOOP-V1 Phase 3 — codegen failure-memory injection tests.

Covers:

1. Codegen retrieval helper returns the concrete missing_patterns from
   evidence_refs when present (the whole point of codegen-side
   injection — surface specific symbols, not just lessons).
2. prompt_context='codegen_warning' filter excludes rows the
   planner can see but codegen cannot (e.g. compile_repair rows whose
   whitelist is planner_warning-only).
3. Strict family filter: cross-family rows are NOT injected even if
   scope matches.
4. The retrieved directive appears in ``_build_codegen_task_description``
   output via ``pipeline_state['codegen_failure_warnings']``, threading
   through to the actual codegen prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

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
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.models.base import Base  # noqa: E402
import app.models  # noqa: F401, E402
from app.orchestrator.service import (  # noqa: E402
    PrimaryOrchestrator,
    _dedupe_compile_errors_by_file,
    _extract_protected_symbols,
)
from app.services.failure_classifier import (  # noqa: E402
    classify_acceptance_test_pattern_missing,
    classify_must_touch_incomplete_diff,
)
from app.services.memory import MemoryService  # noqa: E402


# ---- Fixtures -----------------------------------------------------------


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, future=True)
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="t",
        objective="Map picker for KYC",
        request_summary="map picker",
        scenario="jira_issue_develop",
        change_summary="Add map UI",
        change_explanation="Add map UI",
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
                purpose="patch",
            ),
        ],
        steps=[
            PlanStep(
                step_id="s1",
                title="Edit",
                kind="action",
                owner_role=RoleName.ACTION,
                expected_output="diff",
                success_criteria="ok",
            ),
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _task(family_hint: str = "map-based address picker with osmdroid MapView") -> SimpleNamespace:
    """Build a task whose request_text triggers the android_map_location
    family detector (which keys on 'map-based' / 'osmdroid' / 'mapview')."""
    return SimpleNamespace(
        id="task-c3",
        session_id="session-c3",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text=f"finish p69-19 — {family_hint}",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        translation_json=None,
        plan_json={
            "objective": "Add map-based address picker",
            "must_touch_files": [
                "app/.../CustomerSignup.kt",
                "app/.../CustomerKYCAddressForm.kt",
            ],
        },
        latest_result_json=None,
        governance_json=None,
        pending_approval=False,
        retry_count=0,
        request_source="jira",
        source_name="handymanapp",
    )


def _seed_acceptance_failure(
    db: Session, *, trust_level: str = "human_confirmed"
) -> object:
    """Seed the round-9 style acceptance_test_pattern_missing row."""
    cls = classify_acceptance_test_pattern_missing(
        pipeline_failed_message=(
            "Acceptance gate failed: "
            "[diff_contains_pattern] pattern 'singleTapConfirmedHelper|setOnMarkerDragListener' not in any added line "
            "| [diff_contains_pattern] pattern 'updateChildren|setValue' not in any added line "
            "| [diff_contains_pattern] pattern 'org\\.osmdroid\\.' not in any added line"
        ),
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-9-seed",
    )
    assert cls is not None
    return MemoryService(db).write_failure_observation(
        failure_class=cls.failure_class,
        scope=cls.scope,
        observation_text=f"task=round-9-seed family=android_map_location {cls.failure_class}",
        lesson=cls.lesson,
        task_family=cls.task_family,
        trust_level=trust_level,
        prompt_eligible=list(cls.prompt_eligible),
        evidence_refs=dict(cls.evidence_refs),
    )


def _orch(db: Session) -> PrimaryOrchestrator:
    orch = PrimaryOrchestrator(db=db)
    orch.db.flush = Mock()
    return orch


# ---- Test 1 — retrieval returns concrete patterns -----------------------


def test_codegen_warning_returns_concrete_patterns(db: Session) -> None:
    """When the seeded row carries evidence_refs.missing_patterns,
    the retrieval directive must surface those patterns as concrete
    bullet points the codegen LLM can emit verbatim."""
    seeded = _seed_acceptance_failure(db)
    db.commit()
    orch = _orch(db)
    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())
    assert directive, "codegen warning must be non-empty"
    assert "PRIOR FAILURE WARNING" in directive
    assert "singleTapConfirmedHelper" in directive
    assert "updateChildren" in directive
    assert "org\\.osmdroid\\." in directive or "osmdroid" in directive
    assert "ADDED LINES" in directive
    assert len(audit) == 1
    assert audit[0]["memory_id"] == seeded.id
    assert audit[0]["failure_class"] == "acceptance_test_pattern_missing"
    assert audit[0]["task_family"] == "android_map_location"
    assert "singleTapConfirmedHelper|setOnMarkerDragListener" in audit[0]["missing_patterns"]


# ---- Test 2 — prompt_context whitelist filter --------------------------


def test_planner_only_row_not_visible_to_codegen(db: Session) -> None:
    """A row whose prompt_eligible whitelist excludes 'codegen_warning'
    must NOT surface in the codegen-side retrieval, even when scope
    and family match."""
    svc = MemoryService(db)
    svc.write_failure_observation(
        failure_class="must_touch_incomplete_diff",
        scope="gate:must_touch",
        observation_text="planner-only row",
        lesson="planner instruction only",
        task_family="android_map_location",
        trust_level="human_confirmed",
        # Note: NO 'codegen_warning' in whitelist
        prompt_eligible=["planner_warning"],
        evidence_refs={},
    )
    db.commit()
    orch = _orch(db)
    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())
    assert directive == "", (
        "Row without 'codegen_warning' in prompt_eligible must not surface "
        "in the codegen-side retrieval. "
        f"Got: {directive[:100]!r}"
    )
    assert audit == []


# ---- Test 3 — strict family filter --------------------------------------


def test_cross_family_row_excluded_from_codegen(db: Session) -> None:
    """A failure_observation tagged with a different task_family must
    NOT bleed into codegen for the current family. Codegen prompts are
    too tight to absorb cross-family noise."""
    svc = MemoryService(db)
    svc.write_failure_observation(
        failure_class="acceptance_test_pattern_missing",
        scope="gate:acceptance_check",
        observation_text="python refactor row that codegen should not see",
        lesson="lesson for python refactor",
        task_family="python_refactor",  # different family
        trust_level="human_confirmed",
        prompt_eligible=["planner_warning", "codegen_warning"],
        evidence_refs={"missing_patterns": [{"kind": "x", "pattern": "extract_helper"}]},
    )
    db.commit()
    orch = _orch(db)
    # task = android_map_location family, but seeded row = python_refactor
    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())
    assert directive == "", (
        "Cross-family row must NOT inject. "
        f"Got: {directive[:120]!r}"
    )
    assert audit == []


# ---- Test 4 — warning flows into task_description ----------------------


def test_codegen_warning_flows_into_task_description(db: Session) -> None:
    """When pipeline_state carries codegen_failure_warnings (set by the
    orchestrator main-thread codegen entry), every call to
    _build_codegen_task_description from worker threads sees it
    appended to the directives list."""
    orch = _orch(db)
    pipeline_state = {
        "codegen_failure_warnings": (
            "PRIOR FAILURE WARNING (auto-retrieved from agent_memory):\n"
            "Concrete missing patterns:\n  - singleTapConfirmedHelper\n"
            "Your diff MUST introduce these as ADDED LINES."
        ),
    }
    task = _task()
    plan = _plan()
    body = orch._build_codegen_task_description(
        task=task,
        plan=plan,
        pipeline_state=pipeline_state,
        batch_files={"app/.../CustomerSignup.kt": "stub"},
    )
    assert "PRIOR FAILURE WARNING" in body
    assert "singleTapConfirmedHelper" in body
    assert "ADDED LINES" in body



# ---- Test 5 — no family inferred → no injection -----------------------


def test_memory_missing_patterns_extract_repair_protected_symbols() -> None:
    first_attempt = """diff --git a/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt b/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
--- a/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
+++ b/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
@@ -1,3 +1,20 @@
 package com.example.handyman.customer_pages
+import org.osmdroid.events.MapEventsOverlay
+import org.osmdroid.events.MapEventsReceiver
+import org.osmdroid.views.MapView
+val mapView = MapView(context)
+mapView.overlays.add(MapEventsOverlay(object : MapEventsReceiver {
+    override fun singleTapConfirmedHelper(p: GeoPoint?): Boolean {
+        val addresses = geocoder.getFromLocation(p.latitude, p.longitude, 1)
+        return true
+    }
+}))
"""
    protected = _extract_protected_symbols(
        first_attempt,
        "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
        acceptance_patterns=[],
        memory_patterns=[
            "singleTapConfirmedHelper|setOnMarkerDragListener",
            r"getFromLocation\s*\(",
            r"org\.osmdroid\.",
        ],
    )
    assert "singleTapConfirmedHelper" in protected
    assert "getFromLocation" in protected
    assert "MapEventsReceiver" in protected
    assert "MapView" in protected


def test_dedupe_compile_errors_by_file_merges_duplicate_file_queue() -> None:
    deduped = _dedupe_compile_errors_by_file(
        [
            {"file": "app/F.kt", "error": "Unresolved reference A"},
            {"file": "app/F.kt", "error": "Unresolved reference B"},
            {"file": "app/G.kt", "error": "Type mismatch"},
        ]
    )
    assert [e["file"] for e in deduped] == ["app/F.kt", "app/G.kt"]
    assert "Unresolved reference A" in deduped[0]["error"]
    assert "Unresolved reference B" in deduped[0]["error"]
    assert len(deduped[0]["related_errors"]) == 2


def test_no_family_match_returns_empty(db: Session) -> None:
    """When the task_family cannot be inferred from request_text /
    plan_json, codegen-side retrieval must return empty (codegen is
    too tight for cross-family broadcasting)."""
    _seed_acceptance_failure(db)
    db.commit()
    orch = _orch(db)
    # task with no recognizable family hint in request text or plan
    obscure_task = _task(family_hint="generic infrastructure tweak")
    obscure_task.plan_json = {"objective": "generic", "must_touch_files": []}
    directive, audit = orch._build_codegen_failure_warnings(
        task=obscure_task, plan=_plan()
    )
    assert directive == ""
    assert audit == []
