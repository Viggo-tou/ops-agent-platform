"""T-LEARNING-LOOP-V1 Phase 2 — planner failure-memory injection tests.

Covers the 5 acceptance criteria from the ticket:

1. Similar task retrieves the round-8 must_touch_incomplete_diff row.
2. Unrelated task (different task_family) does NOT retrieve it.
3. prompt_eligible whitelist enforcement — rows lacking
   'planner_warning' are excluded.
4. trust_level ranking: verified > human_confirmed > auto_classified.
5. planner.failure_memory_injected audit event is emitted when memory
   rows are injected into the planner prompt.

Tests target the helper ``PrimaryAgentPlanner._build_prior_failures_block``
directly (no external LLM call needed) and the orchestration in
``generate_plan`` for the event-emission test.
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

from app.agents.service import PrimaryAgentPlanner  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.memory import AgentMemory  # noqa: E402
import app.models  # noqa: F401, E402   # registers every model on Base
from app.services.failure_classifier import classify_must_touch_incomplete_diff  # noqa: E402
from app.services.memory import MemoryService  # noqa: E402


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


def _seed_round_8_failure(db: Session, *, trust_level: str = "human_confirmed") -> AgentMemory:
    """Seed the round-8 must_touch_incomplete_diff failure row."""
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
    row = svc.write_failure_observation(
        failure_class=cls.failure_class,
        scope=cls.scope,
        observation_text=f"task=round-8 family={cls.task_family} {cls.failure_class}",
        lesson=cls.lesson,
        task_family=cls.task_family,
        trust_level=trust_level,
        prompt_eligible=list(cls.prompt_eligible),  # planner + codegen
        evidence_refs=dict(cls.evidence_refs),
    )
    db.commit()
    return row


def _planner_with_db(db: Session) -> PrimaryAgentPlanner:
    """Build a planner instance wired to the in-memory DB."""
    return PrimaryAgentPlanner(settings=get_settings(), db=db)


# ---------------------------------------------------------------------------
# Acceptance #1 — similar task retrieves round-8 failure
# ---------------------------------------------------------------------------


def test_similar_task_retrieves_round_8_failure(db: Session) -> None:
    """A planner running a new Android map-location task must see the
    seeded round-8 failure_observation in its prior_failures_block."""
    _seed_round_8_failure(db)
    planner = _planner_with_db(db)

    rendered, audit = planner._build_prior_failures_block(
        request_text="finish p69-19 — map-based address picker with osmdroid MapView",
        scenario="jira_issue_develop",
        candidate_files=[
            {"path": "app/.../CustomerKYCAddressForm.kt"},
            {"path": "app/.../CustomerSignup.kt"},
        ],
    )
    assert rendered, "planner should produce a prior_failures_block"
    assert "Relevant prior failure observations" in rendered
    assert "must_touch_incomplete_diff" in rendered
    assert "android_map_location" in rendered
    assert "risk warnings only" in rendered  # planner instruction line
    assert len(audit) >= 1
    assert audit[0]["failure_class"] == "must_touch_incomplete_diff"
    assert audit[0]["task_family"] == "android_map_location"


# ---------------------------------------------------------------------------
# Acceptance #2 — unrelated task does NOT see the row
# ---------------------------------------------------------------------------


def test_unrelated_task_does_not_retrieve_failure(db: Session) -> None:
    """A planner running an unrelated Python bugfix task must NOT see
    the Android failure_observation row. We allow the row to surface
    only when family matches OR the FTS keyword overlap is enough on
    its own; in practice, with no overlap, the block stays empty.
    """
    _seed_round_8_failure(db)
    planner = _planner_with_db(db)

    rendered, audit = planner._build_prior_failures_block(
        request_text="Refactor billing service: extract invoice helpers into separate module",
        scenario="python_bugfix",
        candidate_files=[
            {"path": "app/billing/service.py"},
            {"path": "app/billing/invoice.py"},
        ],
    )
    # The seeded row has task_family=android_map_location, no Python
    # keyword overlap, and a scope of gate:must_touch. With low score,
    # the retrieval threshold (>= 1.0) plus the family-mismatch penalty
    # keeps it out of the rendered block.
    assert "android_map_location" not in rendered, (
        "Unrelated Python task must NOT pull Android failure into its prompt; "
        f"got rendered block:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Acceptance #3 — prompt_eligible whitelist enforcement
# ---------------------------------------------------------------------------


def test_prompt_eligible_filter_excludes_non_planner_rows(db: Session) -> None:
    """A failure_observation row whose prompt_eligible list lacks
    'planner_warning' must NOT appear in the planner-side block, even
    if scope and family match perfectly."""
    svc = MemoryService(db)
    svc.write_failure_observation(
        failure_class="must_touch_incomplete_diff",
        scope="gate:must_touch",
        observation_text="should be hidden from planner",
        lesson="this row is repair_hint-only",
        task_family="android_map_location",
        trust_level="human_confirmed",
        # Crucial: no 'planner_warning' in the whitelist
        prompt_eligible=["repair_hint"],
        evidence_refs={},
    )
    db.commit()

    planner = _planner_with_db(db)
    rendered, audit = planner._build_prior_failures_block(
        request_text="finish p69-19 — map-based address picker with osmdroid MapView",
        scenario="jira_issue_develop",
        candidate_files=[{"path": "app/.../CustomerKYCAddressForm.kt"}],
    )
    assert rendered == "", (
        "Row with prompt_eligible=['repair_hint'] must NOT surface in planner block. "
        f"Got: {rendered!r}"
    )
    assert audit == []


def test_planner_reclassifies_familyless_memory_row(db: Session) -> None:
    svc = MemoryService(db)
    svc.write_failure_observation(
        failure_class="semantic_review_low_completeness",
        scope="review:semantic",
        observation_text=(
            "semantic_review low completeness: analytics dummy data and "
            "role simplification remain unimplemented."
        ),
        lesson=(
            "Before planning, cover hardcoded username, session cache, "
            "analytics dummy data, and admin role simplification."
        ),
        task_family=None,
        trust_level="auto_classified",
        prompt_eligible=["planner_warning", "codegen_warning"],
        evidence_refs={
            "semantic_review": {
                "summary": (
                    "analytics dummy data and role simplification remain "
                    "unimplemented."
                )
            }
        },
    )
    db.commit()
    planner = _planner_with_db(db)

    rendered, audit = planner._build_prior_failures_block(
        request_text=(
            "Remove hardcoded username, dummy analytics data, stale session "
            "cache, and master admin role titles."
        ),
        scenario="jira_issue_develop",
        candidate_files=[],
    )

    assert "semantic_review_low_completeness" in rendered
    assert audit[0]["task_family"] == "react_dashboard_session_data_cleanup"


# ---------------------------------------------------------------------------
# Acceptance #4 — trust_level ranking
# ---------------------------------------------------------------------------


def test_trust_level_ranking_verified_beats_auto_classified(db: Session) -> None:
    """When multiple failure_observation rows match, ``verified`` rows
    rank above ``human_confirmed`` rank above ``auto_classified`` —
    the top-K returned reflects this preference."""
    svc = MemoryService(db)
    # Three rows, identical except for trust_level.
    for trust in ("auto_classified", "human_confirmed", "verified"):
        svc.write_failure_observation(
            failure_class=f"must_touch_incomplete_diff_{trust}",
            scope="gate:must_touch",
            observation_text=f"trust={trust} task=test",
            lesson="same lesson body for parity",
            task_family="android_map_location",
            trust_level=trust,
            prompt_eligible=["planner_warning"],
            evidence_refs={},
        )
    db.commit()

    planner = _planner_with_db(db)
    rendered, audit = planner._build_prior_failures_block(
        request_text="finish p69-19 — map-based address picker with osmdroid MapView",
        scenario="jira_issue_develop",
        candidate_files=[],
        top_k=3,
    )
    assert len(audit) == 3
    # First row must be verified, second human_confirmed, third auto_classified.
    levels = [r["trust_level"] for r in audit]
    assert levels == ["verified", "human_confirmed", "auto_classified"], (
        f"trust ranking violated: got {levels}"
    )
    # And the rendered text must list verified first.
    verified_pos = rendered.find("[verified]")
    human_pos = rendered.find("[human_confirmed]")
    auto_pos = rendered.find("[auto_classified]")
    assert 0 <= verified_pos < human_pos < auto_pos, (
        f"render order broken: verified@{verified_pos} "
        f"human@{human_pos} auto@{auto_pos}"
    )


# ---------------------------------------------------------------------------
# Acceptance #5 — audit event emitted on injection
# ---------------------------------------------------------------------------


def test_planner_failure_memory_injected_event_payload(db: Session) -> None:
    """The audit payload returned alongside the rendered block must
    list the injected memory IDs / failure_classes / task_families /
    trust_levels so the orchestrator can record a
    ``planner.failure_memory_injected`` event for observability."""
    seeded = _seed_round_8_failure(db, trust_level="human_confirmed")
    planner = _planner_with_db(db)

    rendered, audit = planner._build_prior_failures_block(
        request_text="finish p69-19 — map-based address picker with osmdroid MapView",
        scenario="jira_issue_develop",
        candidate_files=[{"path": "app/.../CustomerKYCAddressForm.kt"}],
    )
    assert audit, "audit payload required when injection happens"
    payload_row = audit[0]
    assert payload_row["memory_id"] == seeded.id
    assert payload_row["failure_class"] == "must_touch_incomplete_diff"
    assert payload_row["task_family"] == "android_map_location"
    assert payload_row["trust_level"] == "human_confirmed"
    assert "score" in payload_row
    assert payload_row["score"] >= 5.0, (
        "round-8 row with family match should score >= 5 from family_match alone; "
        f"got {payload_row['score']}"
    )
