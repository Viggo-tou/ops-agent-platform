"""T-LEARNING-LOOP-V1 (2026-05-12) — failure-observation memory tests.

Covers the four acceptance criteria from the ticket:

1. Round-8-style must_touch failure writes a failure_observation row.
2. contract_coverage `lie` does NOT pollute the success-fact pool.
3. Planner retrieval surfaces failure_observation rows for a similar
   task_family.
4. Prompt-context whitelist excludes rows whose `prompt_eligible` does
   not include the requesting context (prevents failure observations
   from leaking into ``repair_hint`` prompts as a "fix here" recipe).

Tests use an in-memory SQLite engine so they're hermetic and fast.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.base import Base  # noqa: E402
from app.models.memory import AgentMemory  # noqa: E402
import app.models  # noqa: F401, E402   # ensures every model is registered
from app.services.failure_classifier import (  # noqa: E402
    classify,
    classify_acceptance_test_pattern_missing,
    classify_must_touch_incomplete_diff,
    classify_contract_coverage_failure,
    detect_memory_task_family,
    detect_task_family,
)
from app.services.memory import MemoryService  # noqa: E402


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    # FTS5 table is created lazily by MemoryService._ensure_fts_table
    # in __init__; nothing to do here.
    SessionFactory = sessionmaker(bind=engine, autoflush=False, future=True)
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Acceptance #1 — round-8 must_touch failure writes a failure_observation row
# ---------------------------------------------------------------------------


def test_must_touch_failure_writes_observation_row(db: Session) -> None:
    """Reproduce round-8 conditions: planner declared 2 must_touch but
    codegen only patched 1. The classifier must produce a
    `must_touch_incomplete_diff` classification and the memory service
    must persist it as a failure_observation row queryable by family.
    """
    classifications = classify(
        plan_must_touch=["CustomerSignup.kt", "CustomerKYCAddressForm.kt"],
        files_actually_patched=["CustomerSignup.kt"],
        pipeline_failed_message=(
            "2 must_touch file(s) were not successfully patched: "
            "CustomerKYCAddressForm.kt"
        ),
        codegen_provider="deepseek-v4-pro",
        diff_chars=5420,
        task_family=detect_task_family(
            "finish p69-19",
            {
                "objective": "Add map-based address selection to KYC signup",
                "must_touch_files": ["CustomerSignup.kt", "CustomerKYCAddressForm.kt"],
            },
        ),
        task_id="round-8",
    )

    assert classifications, "classifier should match round-8 signals"
    assert any(c.failure_class == "must_touch_incomplete_diff" for c in classifications)

    svc = MemoryService(db)
    rows_before = db.query(AgentMemory).count()
    for c in classifications:
        svc.write_failure_observation(
            failure_class=c.failure_class,
            scope=c.scope,
            observation_text=f"task=round-8 {c.failure_class}",
            lesson=c.lesson,
            task_family=c.task_family,
            provenance_task_id=None,
            trust_level=c.trust_level,
            prompt_eligible=list(c.prompt_eligible),
            evidence_refs=dict(c.evidence_refs),
        )
    db.commit()

    rows_after = db.query(AgentMemory).count()
    assert rows_after - rows_before == len(classifications)

    persisted = (
        db.query(AgentMemory)
        .filter(AgentMemory.memory_kind == "failure_observation")
        .all()
    )
    assert persisted, "failure_observation row must persist"
    row = persisted[0]
    assert row.failure_class == "must_touch_incomplete_diff"
    assert row.task_family == "android_map_location"
    assert row.trust_level == "auto_classified"  # default; not human-confirmed
    assert "CustomerKYCAddressForm.kt" in row.resolution  # lesson mentions missing file


# ---------------------------------------------------------------------------
# Acceptance #2 — contract_coverage lie does NOT pollute success pool
# ---------------------------------------------------------------------------


def test_contract_coverage_lie_does_not_pollute_success_pool(db: Session) -> None:
    """A contract_coverage gate failure (any verdict_kind other than
    'complete'/'incomplete') must write a failure_observation row.
    The existing success-fact pool must remain untouched — no row with
    memory_kind='success_fact' is produced from a failure signal.
    """
    # Seed a success_fact row so we can prove the failure path does NOT
    # add to that pool.
    success_seed = AgentMemory(
        scope="gate:semantic_review:hardcoded_stub",
        key="seed",
        kind="gate_failure_resolution",
        memory_kind="success_fact",
        observation="existing success memory",
        resolution="replace hardcoded stub with config-driven value",
        confidence=1.0,
        usage_count=0,
    )
    db.add(success_seed)
    db.commit()
    seed_id = success_seed.id

    coverage_verdict = {
        "ok": False,
        "verdict_kind": "lie",
        "verified_implemented": [],
        "verified_no_change": [],
        "missing": [],
        "lies": [{"contract_id": "map_ui_present", "claim": "implemented", "reason": "not in diff"}],
        "summary": "Model claimed coverage for 1 contract(s) but the artifact contains no supporting evidence.",
    }
    classifications = classify(
        coverage_verdict=coverage_verdict,
        codegen_provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-6-replay",
    )
    coverage_cls = [c for c in classifications if c.scope == "gate:contract_coverage"]
    assert coverage_cls, "contract_coverage lie must produce a classification"

    svc = MemoryService(db)
    for c in coverage_cls:
        svc.write_failure_observation(
            failure_class=c.failure_class,
            scope=c.scope,
            observation_text="task=round-6-replay contract_coverage_lie",
            lesson=c.lesson,
            task_family=c.task_family,
            trust_level=c.trust_level,
            prompt_eligible=list(c.prompt_eligible),
            evidence_refs=dict(c.evidence_refs),
        )
    db.commit()

    # Success seed unchanged.
    seed_after = db.get(AgentMemory, seed_id)
    assert seed_after is not None
    assert seed_after.memory_kind == "success_fact"
    assert seed_after.observation == "existing success memory"  # not mutated

    # No new success_fact rows were written.
    success_count = (
        db.query(AgentMemory).filter(AgentMemory.memory_kind == "success_fact").count()
    )
    assert success_count == 1, "failure path must not write to success pool"

    failure_count = (
        db.query(AgentMemory)
        .filter(AgentMemory.memory_kind == "failure_observation")
        .count()
    )
    assert failure_count == len(coverage_cls)
    failure_row = (
        db.query(AgentMemory)
        .filter(AgentMemory.memory_kind == "failure_observation")
        .first()
    )
    assert failure_row.failure_class == "contract_coverage_lie"


# ---------------------------------------------------------------------------
# Acceptance #3 — planner retrieves failure_observation for similar task
# ---------------------------------------------------------------------------


def test_planner_retrieves_failure_observation_for_similar_task(db: Session) -> None:
    """A planner running a new task in the same task_family must
    retrieve the earlier failure_observation row via the new query
    axes (memory_kind + task_family + prompt_context)."""
    svc = MemoryService(db)
    cls = classify_must_touch_incomplete_diff(
        plan_must_touch=["CustomerSignup.kt", "CustomerKYCAddressForm.kt"],
        files_actually_patched=["CustomerSignup.kt"],
        pipeline_failed_message="2 must_touch file(s) were not successfully patched",
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-8",
    )
    assert cls is not None
    svc.write_failure_observation(
        failure_class=cls.failure_class,
        scope=cls.scope,
        observation_text="task=round-8 must_touch under-patched",
        lesson=cls.lesson,
        task_family=cls.task_family,
        trust_level="human_confirmed",
        prompt_eligible=list(cls.prompt_eligible),
        evidence_refs=dict(cls.evidence_refs),
    )
    db.commit()

    # Simulate a NEW task in the same family calling the planner-side
    # retrieval.
    rows = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        task_family="android_map_location",
        prompt_context="planner_warning",
        top_n=3,
    )
    assert len(rows) == 1
    assert rows[0].failure_class == "must_touch_incomplete_diff"
    assert rows[0].trust_level == "human_confirmed"
    # The same retrieval for a DIFFERENT family must NOT return the row.
    rows_other = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        task_family="python_refactor",
        prompt_context="planner_warning",
        top_n=3,
    )
    assert rows_other == [], "family filter must exclude unrelated rows"


# ---------------------------------------------------------------------------
# Acceptance #4 — prompt_context whitelist excludes unauthorized injections
# ---------------------------------------------------------------------------


def test_prompt_injection_whitelist_excludes_repair_hint(db: Session) -> None:
    """A failure_observation row whose prompt_eligible whitelist is
    ['planner_warning', 'codegen_warning'] must NOT be returned when
    the query asks for prompt_context='repair_hint'. This prevents
    the lesson text from being injected into a repair prompt where
    it would be misread as 'the correct fix is X'."""
    svc = MemoryService(db)
    cls = classify_must_touch_incomplete_diff(
        plan_must_touch=["A.kt", "B.kt"],
        files_actually_patched=["A.kt"],
        pipeline_failed_message="must_touch missing B.kt",
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="test",
    )
    assert cls is not None
    # Sanity: classifier whitelists planner_warning and codegen_warning,
    # NOT repair_hint.
    assert "planner_warning" in cls.prompt_eligible
    assert "codegen_warning" in cls.prompt_eligible
    assert "repair_hint" not in cls.prompt_eligible

    svc.write_failure_observation(
        failure_class=cls.failure_class,
        scope=cls.scope,
        observation_text="injection-whitelist test row",
        lesson=cls.lesson,
        task_family=cls.task_family,
        trust_level="auto_classified",
        prompt_eligible=list(cls.prompt_eligible),
        evidence_refs=dict(cls.evidence_refs),
    )
    db.commit()

    planner_rows = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        prompt_context="planner_warning",
        top_n=3,
    )
    assert len(planner_rows) == 1

    codegen_rows = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        prompt_context="codegen_warning",
        top_n=3,
    )
    assert len(codegen_rows) == 1

    # The critical assertion: repair_hint context excludes this row.
    repair_rows = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        prompt_context="repair_hint",
        top_n=3,
    )
    assert repair_rows == [], (
        "failure_observation must NOT leak into repair_hint context "
        "(would be misread as 'fix recipe' by downstream agent)"
    )


# ---------------------------------------------------------------------------
# Acceptance #5 (post-round-9) — acceptance_test_pattern_missing classifier
# ---------------------------------------------------------------------------


def test_acceptance_test_pattern_missing_round_9_message(db: Session) -> None:
    """Reproduce the actual round-9 pipeline.failed message and verify
    classify_acceptance_test_pattern_missing extracts all 5 missing
    patterns into evidence_refs.missing_patterns."""
    round_9_msg = (
        "Acceptance gate failed: "
        "[diff_contains_pattern] pattern 'singleTapConfirmedHelper|setOnMarkerDragListener' not in any added line "
        "| [diff_contains_pattern] pattern 'getFromLocation\\\\s*\\\\(' not in any added line "
        "| [diff_contains_pattern] pattern 'updateChildren|setValue' not in any added line "
        "| [diff_contains_pattern] pattern 'org\\\\.osmdroid\\\\.' not in any added line "
        "| [diff_contains_pattern] pattern 'Geocoder\\\\s*\\\\([^,)]+,\\\\s*Locale' not in any added line"
    )
    cls = classify_acceptance_test_pattern_missing(
        pipeline_failed_message=round_9_msg,
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-9",
    )
    assert cls is not None
    assert cls.failure_class == "acceptance_test_pattern_missing"
    assert cls.scope == "gate:acceptance_check"
    assert cls.task_family == "android_map_location"
    # All 5 patterns extracted into evidence
    missing = cls.evidence_refs["missing_patterns"]
    assert len(missing) == 5
    patterns_found = [m["pattern"] for m in missing]
    assert "singleTapConfirmedHelper|setOnMarkerDragListener" in patterns_found
    assert "updateChildren|setValue" in patterns_found
    # Lesson body mentions concrete missing patterns
    assert "singleTapConfirmedHelper" in cls.lesson or "setOnMarkerDragListener" in cls.lesson


def test_must_touch_classifier_defers_to_acceptance_gate_message(db: Session) -> None:
    """When pipeline_failed_message says 'Acceptance gate failed',
    must_touch classifier must NOT fire — even if files_actually_patched
    happens to be empty (acceptance_check fires before
    sandbox.apply_patch writes pipeline_state.files_changed).

    This is the round-9 false-positive that motivated the fix.
    """
    msg = (
        "Acceptance gate failed: [diff_contains_pattern] pattern 'X' "
        "not in any added line"
    )
    # plan_must_touch non-empty, files_actually_patched empty — would
    # have wrongly fired must_touch_incomplete_diff before the fix.
    result = classify_must_touch_incomplete_diff(
        plan_must_touch=["A.kt", "B.kt"],
        files_actually_patched=[],  # acceptance fired pre-apply_patch
        pipeline_failed_message=msg,
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-9-replay",
    )
    assert result is None, (
        "must_touch_incomplete_diff must defer to acceptance_check when "
        "the failure message belongs to the acceptance gate"
    )


def test_must_touch_classifier_defers_to_runtime_validation_message(db: Session) -> None:
    """Runtime validation failures must not be re-labeled as must_touch misses."""
    result = classify_must_touch_incomplete_diff(
        plan_must_touch=["src/pages/AdminSettings.js", "src/context/UserContext.js"],
        files_actually_patched=[],
        pipeline_failed_message="Runtime validation: Runtime validation failed: 3 blocking issue(s)",
        provider="deepseek-v4-pro",
        task_family="react_dashboard_session_data_cleanup",
        task_id="runtime-gate-replay",
    )

    assert result is None


def test_dispatcher_prefers_acceptance_over_must_touch(db: Session) -> None:
    """End-to-end: feed round-9 conditions through classify() dispatcher
    and verify the output is acceptance_test_pattern_missing alone, not
    a false-positive must_touch row. This pins the bug we hit live on
    round 9 (auto-classified ``must_touch_incomplete_diff`` when the
    real failure was the acceptance gate).
    """
    round_9_msg = (
        "Acceptance gate failed: "
        "[diff_contains_pattern] pattern 'singleTapConfirmedHelper' not in any added line "
        "| [diff_contains_pattern] pattern 'updateChildren' not in any added line"
    )
    results = classify(
        plan_must_touch=[
            "app/.../CustomerSignup.kt",
            "app/.../CustomerKYCAddressForm.kt",
        ],
        files_actually_patched=[],  # acceptance fired pre-apply_patch
        pipeline_failed_message=round_9_msg,
        codegen_provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-9-replay",
    )
    failure_classes = [c.failure_class for c in results]
    assert "acceptance_test_pattern_missing" in failure_classes
    assert "must_touch_incomplete_diff" not in failure_classes, (
        f"Expected dispatcher to defer must_touch on acceptance failures. "
        f"Got: {failure_classes}"
    )


def test_dispatcher_does_not_record_provider_liveness_for_downstream_runtime_gate(
    db: Session,
) -> None:
    """Planner fallback is a metric, not a terminal failure class here."""
    results = classify(
        plan_must_touch=["src/pages/AdminSettings.js", "src/context/UserContext.js"],
        files_actually_patched=[],
        pipeline_failed_message="Runtime validation: Runtime validation failed: 3 blocking issue(s)",
        plan_provider_mode="fallback_after_all_providers_failed",
        plan_provider_name="mock",
        codegen_provider="deepseek",
        task_family="react_dashboard_session_data_cleanup",
        task_id="runtime-gate-replay",
    )

    assert [c.failure_class for c in results] == []


def test_memory_query_filters_misclassified_runtime_validation_rows(db: Session) -> None:
    svc = MemoryService(db)
    svc.write_failure_observation(
        failure_class="must_touch_incomplete_diff",
        scope="gate:must_touch",
        observation_text=(
            "[must_touch_incomplete_diff] task=runtime-gate family=react_dashboard "
            "Pipeline message: Runtime validation failed"
        ),
        lesson="Stale row from a runtime-validation misclassification.",
        task_family="react_dashboard_session_data_cleanup",
        provenance_task_id="runtime-gate",
        trust_level="auto_classified",
        prompt_eligible=["planner_warning", "codegen_warning"],
        evidence_refs={
            "task_id": "runtime-gate",
            "pipeline_message": "Runtime validation: Runtime validation failed: 3 blocking issue(s)",
        },
    )
    db.commit()

    rows = svc.query(
        scope="gate:must_touch",
        memory_kind="failure_observation",
        task_family="react_dashboard_session_data_cleanup",
        prompt_context="codegen_warning",
        top_n=3,
    )

    assert rows == []


def test_semantic_review_findings_write_failure_observation(db: Session) -> None:
    task = SimpleNamespace(
        id="task-semantic-memory",
        request_text="Develop P69-19 map-based address picker with OSMDroid",
        plan_json={
            "objective": "Add map-based address picker",
            "must_touch_files": ["CustomerSignup.kt"],
        },
    )
    review_payload = {
        "status": "failed",
        "provider_name": "deepseek",
        "completeness_pct": 70,
        "high_severity_count": 1,
        "findings": [
            {
                "file": "CustomerSignup.kt",
                "line_start": 120,
                "line_end": 130,
                "severity": "high",
                "category": "state_sync",
                "description": "Typed address lookup moves the marker but does not sync address fields.",
                "evidence_quote": "+                    map.invalidate()",
                "suggested_fix": "Call reverseGeocodeAddress(point, marker, map) after moving the marker.",
            },
            {
                "file": "CustomerSignup.kt",
                "severity": "low",
                "category": "style",
                "description": "Button copy could be clearer.",
            },
        ],
    }

    recorded = MemoryService(db).record_semantic_review_findings(
        task=task,  # type: ignore[arg-type]
        review_payload=review_payload,
        provenance_event_id="event-semantic",
    )
    db.commit()

    assert recorded == 1
    row = db.query(AgentMemory).filter(AgentMemory.scope == "review:semantic").one()
    assert row.memory_kind == "failure_observation"
    assert row.failure_class == "semantic_review_state_sync"
    assert row.task_family == "android_map_location"
    assert row.prompt_eligible == ["planner_warning", "codegen_warning"]
    assert "reverseGeocodeAddress" in row.evidence_refs["finding"]["suggested_fix"]
    assert "success_fact" not in row.memory_kind


def test_semantic_low_completeness_summary_writes_warning_memory(db: Session) -> None:
    task = SimpleNamespace(
        id="task-low-completeness",
        request_text="develop P69-10",
        plan_json={
            "objective": "Implement the referenced Jira issue.",
            "change_summary": "Data and Role Cleanup",
            "must_touch_files": [
                "app/src/main/java/com/example/handyman/HandymanJobBoardFragment.kt",
                "app/src/main/java/com/example/handyman/chatbox/MainActivity.kt",
            ],
        },
    )
    review_payload = {
        "status": "failed",
        "provider_name": "deepseek",
        "completeness_pct": 45,
        "pass_threshold": 80,
        "high_severity_count": 0,
        "findings_dropped_no_evidence": 1,
        "summary": (
            "Partial implementation: hardcoded username overridden but may still flash; "
            "caching issues partly addressed; analytics dummy data and role "
            "simplification remain completely unimplemented."
        ),
        "findings": [],
    }

    row = MemoryService(db).record_semantic_review_low_completeness(
        task=task,  # type: ignore[arg-type]
        review_payload=review_payload,
        provenance_event_id="event-low-completeness",
    )
    db.commit()

    assert row is not None
    assert row.memory_kind == "failure_observation"
    assert row.failure_class == "semantic_review_low_completeness"
    assert row.scope == "review:semantic"
    assert row.task_family == "android_session_data_cleanup"
    assert row.confidence == pytest.approx(0.65)
    assert row.prompt_eligible == ["planner_warning", "codegen_warning"]
    assert row.evidence_refs["finding"]["ungrounded_summary"] is True
    obligations = row.evidence_refs["finding"]["obligations"]
    assert any("analytics dummy data" in item for item in obligations)
    assert "not a recipe" in row.resolution


def test_detects_android_session_data_cleanup_family() -> None:
    assert (
        detect_task_family(
            "develop P69-10",
            {
                "change_explanation": (
                    "Hardcoded values, dummy analytics data, session cache "
                    "issues, and master admin role simplification."
                ),
                "must_touch_files": [
                    "app/src/main/java/com/example/handyman/chatbox/MainActivity.kt"
                ],
            },
        )
        == "android_session_data_cleanup"
    )


def test_detects_react_dashboard_session_data_cleanup_family() -> None:
    assert (
        detect_task_family(
            "develop P69-10",
            {
                "source_name": "hosteddashboard",
                "change_explanation": (
                    "Hardcoded username, dummy analytics data, previous "
                    "logged-in user cache, and master admin role simplification."
                ),
                "must_touch_files": [
                    "src/context/UserContext.js",
                    "src/pages/AdminSettings.js",
                    "src/pages/ServiceAnalytics.js",
                ],
            },
        )
        == "react_dashboard_session_data_cleanup"
    )


def test_memory_family_correction_prevents_dashboard_android_contamination() -> None:
    memory = SimpleNamespace(
        task_family="android_session_data_cleanup",
        failure_class="semantic_review_api_mismatch",
        observation=(
            "semantic_review high/api_mismatch in src/pages/AdminSettings.js: "
            "Role check uses Admin but mockUsers role is lowercase admin."
        ),
        resolution="Normalize dashboard role comparisons consistently.",
        evidence_refs={
            "finding": {
                "file": "src/pages/AdminSettings.js",
                "category": "api_mismatch",
                "description": (
                    "Role check uses string Admin but the mockUsers role is "
                    "lowercase admin."
                ),
            }
        },
    )

    assert detect_memory_task_family(memory) == "react_dashboard_session_data_cleanup"
