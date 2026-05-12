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
    classify_must_touch_incomplete_diff,
    classify_contract_coverage_failure,
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
