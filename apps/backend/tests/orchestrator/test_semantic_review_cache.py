from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import Base
from app.models.task import Task
from app.orchestrator.service import (
    _semantic_review_lookup_verified_cache,
    _semantic_review_plan_signature,
    _semantic_review_report_from_payload,
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def _plan(*, acceptance_pattern: str = "MapEventsOverlay") -> dict:
    return {
        "provider": {"name": "harness:android_map_location_plan"},
        "domain_playbook_id": "android_map_location",
        "must_touch_files": [
            "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
            "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
        ],
        "required_contracts": [
            {"contract_id": "map_ui_present"},
            {"contract_id": "user_can_select_location"},
        ],
        "acceptance_tests": [
            {
                "kind": "diff_contains_pattern",
                "pattern": acceptance_pattern,
            }
        ],
    }


def _state(*, diff: str, semantic: dict | None = None) -> dict:
    state = {
        "diff": diff,
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True},
        "acceptance_check_done": True,
        "symbol_graph_done": True,
        "symbol_graph": {"passed": True},
    }
    if semantic is not None:
        state["semantic_review"] = semantic
    return state


def _passed_semantic(completeness: int = 90) -> dict:
    return {
        "status": "passed",
        "passed": True,
        "completeness_pct": completeness,
        "summary": "Verified equivalent diff.",
        "pass_threshold": 80,
        "total_findings_raw": 0,
        "findings_dropped_no_evidence": 0,
        "high_severity_count": 0,
        "provider_name": "deepseek",
        "review_attempts": 1,
        "repair_attempted": False,
        "findings": [],
    }


def test_semantic_review_cache_reuses_identical_verified_contract_and_diff() -> None:
    db = _db()
    try:
        diff = "diff --git a/a.kt b/a.kt\n+MapEventsOverlay\n+singleTapConfirmedHelper\n"
        previous = Task(
            title="previous",
            request_text="develop P69-19",
            scenario="jira_issue_develop",
            plan_json=_plan(),
            latest_result_json={
                "pipeline_state": _state(
                    diff=diff,
                    semantic=_passed_semantic(completeness=90),
                )
            },
        )
        current = Task(
            title="current",
            request_text="develop P69-19",
            scenario="jira_issue_develop",
            plan_json=_plan(),
        )
        db.add_all([previous, current])
        db.flush()

        hit = _semantic_review_lookup_verified_cache(
            db,
            current_task_id=current.id,
            plan_json=current.plan_json,
            pipeline_state=_state(diff=diff),
            pass_threshold=80,
        )

        assert hit is not None
        assert hit["source_task_id"] == previous.id
        assert hit["completeness_pct"] == 90
        assert hit["semantic_review"] == _passed_semantic(completeness=90)
    finally:
        db.close()


def test_semantic_review_cache_rejects_same_diff_with_different_contract() -> None:
    db = _db()
    try:
        diff = "diff --git a/a.kt b/a.kt\n+MapEventsOverlay\n"
        previous = Task(
            title="previous",
            request_text="develop P69-19",
            scenario="jira_issue_develop",
            plan_json=_plan(acceptance_pattern="MapEventsOverlay"),
            latest_result_json={
                "pipeline_state": _state(
                    diff=diff,
                    semantic=_passed_semantic(completeness=100),
                )
            },
        )
        current = Task(
            title="current",
            request_text="develop P69-19 changed contract",
            scenario="jira_issue_develop",
            plan_json=_plan(acceptance_pattern="updateChildren"),
        )
        db.add_all([previous, current])
        db.flush()

        assert _semantic_review_lookup_verified_cache(
            db,
            current_task_id=current.id,
            plan_json=current.plan_json,
            pipeline_state=_state(diff=diff),
            pass_threshold=80,
        ) is None
    finally:
        db.close()


def test_semantic_review_cache_rejects_unverified_prior_pipeline() -> None:
    db = _db()
    try:
        diff = "diff --git a/a.kt b/a.kt\n+MapEventsOverlay\n"
        prior_state = _state(diff=diff, semantic=_passed_semantic())
        prior_state["compile_gate"] = {"passed": False}
        previous = Task(
            title="previous",
            request_text="develop P69-19",
            scenario="jira_issue_develop",
            plan_json=_plan(),
            latest_result_json={"pipeline_state": prior_state},
        )
        current = Task(
            title="current",
            request_text="develop P69-19",
            scenario="jira_issue_develop",
            plan_json=_plan(),
        )
        db.add_all([previous, current])
        db.flush()

        assert _semantic_review_lookup_verified_cache(
            db,
            current_task_id=current.id,
            plan_json=current.plan_json,
            pipeline_state=_state(diff=diff),
            pass_threshold=80,
        ) is None
    finally:
        db.close()


def test_semantic_review_report_from_payload_reconstructs_report() -> None:
    report = _semantic_review_report_from_payload(_passed_semantic(completeness=95))

    assert report is not None
    assert report.passed is True
    assert report.completeness_pct == 95
    assert report.high_severity_count() == 0


def test_semantic_review_plan_signature_is_order_insensitive_for_targets() -> None:
    left = _plan()
    right = _plan()
    right["must_touch_files"] = list(reversed(right["must_touch_files"]))

    assert _semantic_review_plan_signature(left) == _semantic_review_plan_signature(right)
