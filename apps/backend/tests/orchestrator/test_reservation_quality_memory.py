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

from app.agents.schemas import FinalOutputContract, GeneratedPlan, PlanStep, PlanTool  # noqa: E402
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
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.failure_classifier import classify_acceptance_test_pattern_missing  # noqa: E402
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


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-reservation-memory",
        session_id="session-reservation-memory",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="finish p69-19 - map-based address picker with osmdroid MapView",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        translation_json=None,
        plan_json={
            "objective": "Add map-based address picker",
            "must_touch_files": [
                "app/.../CustomerSignup.kt",
                "app/.../HandymanSignup.kt",
            ],
        },
        latest_result_json=None,
        governance_json=None,
        pending_approval=False,
        retry_count=0,
        request_source="jira",
        source_name="handymanapp",
    )


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-reservation-memory",
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
            )
        ],
        steps=[
            PlanStep(
                step_id="s1",
                title="Edit",
                kind="action",
                owner_role=RoleName.ACTION,
                expected_output="diff",
                success_criteria="ok",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _seed_acceptance_failure(db: Session) -> object:
    cls = classify_acceptance_test_pattern_missing(
        pipeline_failed_message=(
            "Acceptance gate failed: "
            "[diff_contains_pattern] pattern 'singleTapConfirmedHelper|setOnMarkerDragListener' not in any added line"
        ),
        provider="deepseek-v4-pro",
        task_family="android_map_location",
        task_id="round-9-seed",
    )
    assert cls is not None
    return MemoryService(db).write_failure_observation(
        failure_class=cls.failure_class,
        scope=cls.scope,
        observation_text="task=round-9-seed family=android_map_location acceptance miss",
        lesson=cls.lesson,
        task_family=cls.task_family,
        trust_level="human_confirmed",
        prompt_eligible=list(cls.prompt_eligible),
        evidence_refs=dict(cls.evidence_refs),
    )


def test_reservation_quality_memory_injects_codegen_checklist(db: Session) -> None:
    orch = PrimaryOrchestrator(db=db)
    task = _task()
    row = orch._record_reservation_quality_memory(
        task=task,
        reservations_detailed=[
            {
                "text": "Customer and handyman signup persist different address schemas.",
                "severity": "policy",
                "auto_fixable": False,
                "blocking": True,
            },
            {
                "text": "MapView lifecycle cleanup is missing in signup screens.",
                "severity": "bug",
                "auto_fixable": True,
                "blocking": False,
            },
        ],
        files_changed=[
            "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
            "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
        ],
        issue_key="P69-19",
        provenance_event_id="event-reservations",
    )
    assert row is not None
    db.commit()

    directive, audit = orch._build_codegen_failure_warnings(task=task, plan=_plan())

    assert "QUALITY RESERVATION WARNING" in directive
    assert "different address schemas" in directive
    assert "lifecycle cleanup" in directive
    assert "least duplicated, most consistent implementation" in directive
    assert len(audit) == 1
    assert audit[0]["failure_class"] == "approval_reservation_quality"
    assert audit[0]["task_family"] == "android_map_location"


def test_codegen_warning_combines_failure_patterns_and_quality_reservations(db: Session) -> None:
    seeded = _seed_acceptance_failure(db)
    orch = PrimaryOrchestrator(db=db)
    row = orch._record_reservation_quality_memory(
        task=_task(),
        reservations_detailed=[
            {
                "text": "Prefer a shared map picker helper instead of copy-pasting MapView setup.",
                "severity": "style",
                "auto_fixable": True,
                "blocking": False,
            }
        ],
        files_changed=["app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt"],
        issue_key="P69-19",
    )
    assert row is not None
    db.commit()

    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())

    assert seeded.id in {item["memory_id"] for item in audit}
    assert row.id in {item["memory_id"] for item in audit}
    assert "singleTapConfirmedHelper" in directive
    assert "QUALITY RESERVATION WARNING" in directive
    assert "shared map picker helper" in directive


def test_semantic_review_failure_memory_injects_codegen_checklist(db: Session) -> None:
    MemoryService(db).record_semantic_review_findings(
        task=_task(),  # type: ignore[arg-type]
        review_payload={
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
                }
            ],
        },
        provenance_event_id="event-semantic",
    )
    db.commit()

    orch = PrimaryOrchestrator(db=db)
    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())

    assert "SEMANTIC REVIEW WARNING" in directive
    assert "state_sync" in directive
    assert "reverseGeocodeAddress" in directive
    assert audit[0]["failure_class"] == "semantic_review_state_sync"
    assert audit[0]["task_family"] == "android_map_location"


def test_semantic_low_completeness_memory_injects_obligation_checklist(db: Session) -> None:
    task = SimpleNamespace(
        id="task-low-completeness",
        session_id="session-low-completeness",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text=(
            "Develop P69-10 hardcoded username analytics dummy data "
            "and role simplification cleanup"
        ),
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        translation_json=None,
        plan_json={
            "objective": "Clean hardcoded values and analytics dummy data.",
            "change_summary": "Data and Role Cleanup",
            "must_touch_files": [
                "app/src/main/java/com/example/handyman/chatbox/MainActivity.kt"
            ],
        },
        latest_result_json=None,
        governance_json=None,
        pending_approval=False,
        retry_count=0,
        source_name="handymanapp",
    )
    MemoryService(db).record_semantic_review_low_completeness(
        task=task,  # type: ignore[arg-type]
        review_payload={
            "status": "failed",
            "provider_name": "deepseek",
            "completeness_pct": 45,
            "pass_threshold": 80,
            "high_severity_count": 0,
            "summary": (
                "Partial implementation: hardcoded username overridden but may still flash; "
                "analytics dummy data and role simplification remain completely unimplemented."
            ),
            "findings": [],
        },
        provenance_event_id="event-low-completeness",
    )
    db.commit()

    orch = PrimaryOrchestrator(db=db)
    directive, audit = orch._build_codegen_failure_warnings(task=task, plan=_plan())

    assert "SEMANTIC REVIEW WARNING" in directive
    assert "low_completeness" in directive
    assert "analytics dummy data" in directive
    assert "role simplification" in directive
    assert audit[0]["failure_class"] == "semantic_review_low_completeness"
    assert audit[0]["task_family"] == "android_session_data_cleanup"


def test_codegen_memory_reclassifies_familyless_low_completeness_row(db: Session) -> None:
    MemoryService(db).write_failure_observation(
        failure_class="semantic_review_low_completeness",
        scope="review:semantic",
        observation_text=(
            "semantic_review low completeness: analytics dummy data and role "
            "simplification remain unimplemented."
        ),
        lesson=(
            "Treat as an unverified checklist: hardcoded username, session "
            "cache, analytics dummy data, and admin role simplification all "
            "need coverage."
        ),
        task_family=None,
        trust_level="auto_classified",
        prompt_eligible=["planner_warning", "codegen_warning"],
        evidence_refs={
            "semantic_review": {
                "completeness_pct": 45,
                "summary": (
                    "analytics dummy data and role simplification remain "
                    "completely unimplemented."
                ),
            },
            "finding": {
                "category": "low_completeness",
                "description": "role simplification remains unimplemented.",
                "obligations": [
                    "analytics dummy data remains completely unimplemented",
                    "role simplification remains completely unimplemented",
                ],
            },
        },
    )
    for idx in range(8):
        MemoryService(db).write_failure_observation(
            failure_class=f"semantic_review_irrelevant_{idx}",
            scope="review:semantic",
            observation_text=(
                f"develop P69-10 unrelated Android map-location semantic warning {idx}"
            ),
            lesson="This row should lose to the same-family reclassification filter.",
            task_family="android_map_location",
            trust_level="auto_classified",
            prompt_eligible=["planner_warning", "codegen_warning"],
            evidence_refs={"finding": {"description": "osmdroid map warning"}},
        )
    db.commit()

    task = SimpleNamespace(
        id="task-familyless-low-completeness",
        session_id="session-low-completeness",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        request_text="develop P69-10",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        translation_json=None,
        plan_json={
            "objective": "Implement referenced Jira issue.",
            "change_explanation": (
                "Hardcoded values, dummy analytics data, caching issues, "
                "and master admin role simplification must be cleaned up."
            ),
            "must_touch_files": [
                "app/src/main/java/com/example/handyman/chatbox/MainActivity.kt"
            ],
        },
        latest_result_json=None,
        governance_json=None,
        pending_approval=False,
        retry_count=0,
        source_name="handymanapp",
    )

    directive, audit = PrimaryOrchestrator(db=db)._build_codegen_failure_warnings(
        task=task,  # type: ignore[arg-type]
        plan=_plan(),
    )

    assert "SEMANTIC REVIEW WARNING" in directive
    assert "analytics dummy data" in directive
    assert audit[0]["task_family"] == "android_session_data_cleanup"


def test_codegen_warning_keeps_quality_reservation_when_two_pattern_rows_exist(db: Session) -> None:
    _seed_acceptance_failure(db)
    MemoryService(db).write_failure_observation(
        failure_class="must_touch_incomplete_diff",
        scope="gate:must_touch",
        observation_text="task=round-8 family=android_map_location missed files",
        lesson="Touch both signup and KYC files and include the map picker symbols.",
        task_family="android_map_location",
        trust_level="human_confirmed",
        prompt_eligible=["planner_warning", "codegen_warning"],
        evidence_refs={
            "missing_patterns": [
                "singleTapConfirmedHelper|setOnMarkerDragListener",
                "getFromLocation\\s*\\(",
            ]
        },
    )
    orch = PrimaryOrchestrator(db=db)
    quality_row = orch._record_reservation_quality_memory(
        task=_task(),
        reservations_detailed=[
            {
                "text": "Customer and handyman signup persist different address schemas.",
                "severity": "policy",
                "auto_fixable": False,
                "blocking": True,
            }
        ],
        files_changed=["app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt"],
        issue_key="P69-19",
    )
    assert quality_row is not None
    db.commit()

    directive, audit = orch._build_codegen_failure_warnings(task=_task(), plan=_plan())

    classes = {item["failure_class"] for item in audit}
    assert "approval_reservation_quality" in classes
    assert len(audit) == 2
    assert quality_row.id in {item["memory_id"] for item in audit}
    assert "singleTapConfirmedHelper" in directive
    assert "different address schemas" in directive
