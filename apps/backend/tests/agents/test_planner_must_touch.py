from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import (  # noqa: E402
    FinalOutputContract,
    GeneratedPlanPayload,
    PlanAcceptanceTest,
    PlanStep,
    PlanTool,
)
from app.agents.service import (  # noqa: E402
    PrimaryAgentPlanner,
    build_fallback_plan_payload,
    _log_plan_target_warnings,
    _source_bind_expected_new_files,
    _validate_must_touch_against_kb,
)
from app.core.enums import RiskLevel, RoleName, ToolPermissionCategory  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _payload(**updates: object) -> GeneratedPlanPayload:
    payload = GeneratedPlanPayload(
        objective="Implement the request.",
        request_summary="Implement the request.",
        scenario="jira_issue_develop",
        change_summary="Update target files.",
        change_explanation="Update target files based on repository context.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[],
        must_touch_files=[],
        expected_new_files=[],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate code changes.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Generate patch",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=[],
                tool_name="codegen.generate_patch",
                expected_output="Patch generated.",
                success_criteria="Patch is valid.",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="code_change",
            required_fields=["diff"],
        ),
    )
    return payload.model_copy(update=updates)


def _add_doc(session: Session, relative_path: str) -> None:
    session.add(
        KnowledgeDocument(
            source_name="repo",
            relative_path=relative_path,
            title=relative_path,
            extension=Path(relative_path).suffix,
            language=None,
            size_bytes=10,
            line_count=1,
            content_hash=f"hash-{relative_path}",
            metadata_json=None,
            content="content",
        )
    )
    session.commit()


def test_merge_keeps_llm_must_touch_files() -> None:
    merged = PrimaryAgentPlanner._merge_plan_payload_with_defaults(
        {"must_touch_files": ["a.py", "b.py"]},
        _payload(),
    )

    assert merged["must_touch_files"] == ["a.py", "b.py"]


def test_merge_keeps_llm_expected_new_files() -> None:
    merged = PrimaryAgentPlanner._merge_plan_payload_with_defaults(
        {"expected_new_files": ["new.py"]},
        _payload(),
    )

    assert merged["expected_new_files"] == ["new.py"]


def test_kb_validation_drops_hallucinated_path(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    _add_doc(db_session, "real.kt")

    kept, dropped = _validate_must_touch_against_kb(["real.kt", "fake.kt"], db_session)
    assert kept == ["real.kt"]
    assert dropped == ["fake.kt"]

    planner = PrimaryAgentPlanner(db=db_session)
    with caplog.at_level(logging.WARNING, logger="app.agents.service"):
        validated = planner._validate_plan_targets(
            _payload(must_touch_files=["real.kt", "fake.kt"]),
        )

    assert validated.must_touch_files == ["real.kt"]
    assert "must_touch_files entries dropped" in caplog.text
    assert "fake.kt" in caplog.text


def test_kb_validation_empty_input(db_session: Session) -> None:
    assert _validate_must_touch_against_kb([], db_session) == ([], [])


def test_scenario_conditional_logs_develop_with_empty(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.agents.service"):
        _log_plan_target_warnings(
            scenario="jira_issue_develop",
            must_touch_files=[],
            expected_new_files=[],
        )

    assert "under-specified" in caplog.text
    assert "jira_issue_develop" in caplog.text


def test_scenario_conditional_quiet_for_question(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.agents.service"):
        _log_plan_target_warnings(
            scenario="process_question",
            must_touch_files=[],
            expected_new_files=[],
        )

    assert caplog.text == ""


def test_prompt_directive_includes_add_verb() -> None:
    prompt = PrimaryAgentPlanner._build_planning_instructions()

    assert "add to existing" in prompt


def test_source_binding_demotes_unsourced_expected_new_on_existing_task() -> None:
    payload = _payload(
        must_touch_files=["app/forms/CustomerKYCAddressForm.kt"],
        expected_new_files=["app/components/MapAddressPicker.kt"],
        likely_touch_files=[],
        acceptance_tests=[
            PlanAcceptanceTest(
                kind="diff_contains_pattern_in_file",
                file="app/components/MapAddressPicker.kt",
                pattern="MapAddressPicker",
                rationale="planner invented helper target",
            )
        ],
    )

    rebound, demoted = _source_bind_expected_new_files(
        payload,
        request_text="P69-19: add map-based address selection to existing KYC forms.",
        issue_context={
            "summary": "Default address selection should use the existing form maps",
            "description": "Update the current KYC address screens.",
        },
    )

    assert demoted == ["app/components/MapAddressPicker.kt"]
    assert rebound.expected_new_files == []
    assert rebound.likely_touch_files == ["app/components/MapAddressPicker.kt"]
    assert rebound.acceptance_tests == []


def test_source_binding_keeps_explicitly_named_expected_new_file() -> None:
    payload = _payload(
        must_touch_files=["src/firebase.ts"],
        expected_new_files=["database.rules.json"],
    )

    rebound, demoted = _source_bind_expected_new_files(
        payload,
        request_text="Create database.rules.json and wire the app to use it.",
        issue_context={},
    )

    assert demoted == []
    assert rebound.expected_new_files == ["database.rules.json"]


def test_source_binding_keeps_semantic_firebase_rules_artifact() -> None:
    payload = _payload(
        must_touch_files=["app/src/main/java/com/example/handyman/utils/FirebaseMetrics.kt"],
        expected_new_files=["database.rules.json"],
        likely_touch_files=[],
    )

    rebound, demoted = _source_bind_expected_new_files(
        payload,
        request_text="develop P69-8",
        issue_context={
            "summary": "Privacy Update: Firebase rule",
            "description": (
                "The Firebase database rules need to be changed from public "
                "to private to properly secure the data."
            ),
        },
    )

    assert demoted == []
    assert rebound.expected_new_files == ["database.rules.json"]
    assert rebound.must_touch_files == []
    assert rebound.likely_touch_files == [
        "app/src/main/java/com/example/handyman/utils/FirebaseMetrics.kt"
    ]


def test_source_binding_keeps_named_code_file_with_rules_artifact() -> None:
    code_path = "app/src/main/java/com/example/handyman/utils/FirebaseMetrics.kt"
    payload = _payload(
        must_touch_files=[code_path],
        expected_new_files=["database.rules.json"],
        likely_touch_files=[],
    )

    rebound, demoted = _source_bind_expected_new_files(
        payload,
        request_text="Update Firebase database rules and FirebaseMetrics.kt.",
        issue_context={},
    )

    assert demoted == []
    assert rebound.expected_new_files == ["database.rules.json"]
    assert rebound.must_touch_files == [code_path]


def test_fallback_plan_infers_firebase_rules_artifact() -> None:
    plan = build_fallback_plan_payload(
        "develop P69-8",
        scenario="jira_issue_develop",
        issue_context={
            "summary": "Privacy Update: Firebase rule",
            "description": (
                "The Firebase database rules need to be changed from public "
                "to private to properly secure the data."
            ),
        },
        candidate_files=[
            {"relative_path": "app/src/main/java/com/example/handyman/utils/FirebaseMetrics.kt"}
        ],
    )

    assert plan.expected_new_files == ["database.rules.json"]
    assert plan.must_touch_files == []
