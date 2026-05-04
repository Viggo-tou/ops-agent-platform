from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.main as main_module
from app.agents.schemas import (
    FinalOutputContract,
    GeneratedPlan,
    GeneratedSemanticTranslation,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
)
from app.core.enums import ActorRole, EventType, RiskCategory, RiskLevel, RoleName, TaskStatus, ToolPermissionCategory, WorkflowStage
from app.models.base import Base
from app.models.event import Event
from app.models.task import Task
from app.orchestrator.service import PrimaryOrchestrator
from app.services.checkpointing import write_task_checkpoint


def _plan() -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-resume",
        objective="Implement TEST-1.",
        request_summary="Implement TEST-1.",
        scenario="jira_issue_develop",
        change_summary="Update source.",
        change_explanation="Modify the target source file.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[
            PlanCodeLocation(source_name="repo", relative_path="src/a.py", reason="Target file.")
        ],
        must_touch_files=["src/a.py"],
        expected_new_files=[],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate patch.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step-1",
                title="Generate patch",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=[],
                tool_name="codegen.generate_patch",
                expected_output="Patch.",
                success_criteria="Patch generated.",
            )
        ],
        final_output_contract=FinalOutputContract(type="jira_issue_develop", required_fields=["status"]),
    )


def _translation() -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id="task-resume",
        provider={"name": "test"},
        normalized_request="implement TEST-1",
        intent="develop_jira_issue",
        work_type="feature",
        objective="Implement TEST-1.",
        issue_key="TEST-1",
        issue_url=None,
        candidate_modules=[],
        search_queries=[],
        constraints=[],
        requested_outputs=["jira_issue_develop"],
        grounding_terms=[],
        missing_information=[],
        confidence=0.9,
    )


def _task(plan: GeneratedPlan | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-resume",
        session_id="session-resume",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        title="Resume task",
        request_text="implement TEST-1",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        current_role=RoleName.ACTION,
        translation_json=_translation().model_dump(mode="json"),
        plan_json=plan.model_dump(mode="json") if plan is not None else None,
        review_json=None,
        latest_result_json=None,
        latest_checkpoint_json=None,
        pending_approval=False,
        retry_count=0,
        risk_level=RiskLevel.MEDIUM,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
    )


def _orchestrator() -> PrimaryOrchestrator:
    orchestrator = PrimaryOrchestrator(db=Mock())
    orchestrator.db.flush = Mock()
    orchestrator.tool_gateway.settings.resumability_enabled = True
    return orchestrator


def _codegen_state() -> dict[str, object]:
    return {
        "codegen_result": {
            "diff": "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\nold\n+new\n",
            "files_changed": ["src/a.py"],
            "provider_name": "mock",
        },
        "diff": "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\nold\n+new\n",
        "files_changed": ["src/a.py"],
    }


def test_resume_after_kill_during_codegen() -> None:
    plan = _plan()
    task = _task(plan)
    task.latest_result_json = {"pipeline_state": _codegen_state()}
    orchestrator = _orchestrator()

    write_task_checkpoint(
        orchestrator.db,
        task=task,
        stage="codegen",
        output_payload={
            "translation_json": task.translation_json,
            "plan_json": task.plan_json,
            "latest_result_json": task.latest_result_json,
            "pipeline_state": task.latest_result_json["pipeline_state"],
        },
    )

    def _complete(task, actor_name, plan, approval_id=None):  # noqa: ANN001
        assert plan.must_touch_files == ["src/a.py"]
        task.status = TaskStatus.AWAITING_APPROVAL
        task.latest_result_json = {"status": TaskStatus.AWAITING_APPROVAL.value}

    orchestrator._execute_develop_pipeline = Mock(side_effect=_complete)

    with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.commit_checkpoint"):
        assert orchestrator.resume_task(task=task, actor_name="tester")

    orchestrator._execute_develop_pipeline.assert_called_once()
    assert task.status == TaskStatus.AWAITING_APPROVAL


def test_resume_skips_completed_translate() -> None:
    plan = _plan()
    task = _task()
    task.status = TaskStatus.PLANNING
    orchestrator = _orchestrator()
    orchestrator._translate_request = Mock(side_effect=AssertionError("translate must not be called"))
    orchestrator._anchor_precheck_fails = Mock(return_value=False)
    orchestrator._workspace_write_plan = Mock()
    orchestrator.primary_agent.generate_plan = Mock(
        return_value=SimpleNamespace(
            plan=plan,
            provider_name="mock",
            model_name="mock",
            used_fallback=False,
            fallback_reason=None,
        )
    )
    orchestrator.reviewer_agent.review_plan = Mock(
        return_value=SimpleNamespace(
            review=SimpleNamespace(
                verdict="approved",
                model_dump=Mock(
                    return_value={
                        "verdict": "approved",
                        "summary": "ok",
                        "approval_requirements": [],
                    }
                ),
            )
        )
    )
    orchestrator._execute_plan = Mock()

    write_task_checkpoint(
        orchestrator.db,
        task=task,
        stage="retrieve",
        output_payload={
            "translation_json": task.translation_json,
            "semantic_translation": task.translation_json,
            "issue_context": {"issue_key": "TEST-1", "summary": "Implement."},
            "planning_knowledge_context": {"citations": []},
            "planning_request_text": "cached planning text",
        },
    )

    with patch("app.orchestrator.service.record_event"), patch(
        "app.orchestrator.service.set_task_status"
    ), patch("app.orchestrator.service.commit_checkpoint"):
        assert orchestrator.resume_task(task=task, actor_name="tester")

    orchestrator._translate_request.assert_not_called()
    orchestrator.primary_agent.generate_plan.assert_called_once()
    orchestrator._execute_plan.assert_called_once()


def test_resume_preserves_must_touch_files() -> None:
    plan = _plan()
    task = _task(plan)
    task.latest_result_json = {"pipeline_state": _codegen_state()}
    orchestrator = _orchestrator()
    seen: list[list[str]] = []

    def _capture(task, actor_name, plan, approval_id=None):  # noqa: ANN001
        seen.append(list(plan.must_touch_files))

    orchestrator._execute_develop_pipeline = Mock(side_effect=_capture)
    write_task_checkpoint(
        orchestrator.db,
        task=task,
        stage="codegen",
        output_payload={
            "translation_json": task.translation_json,
            "plan_json": task.plan_json,
            "latest_result_json": task.latest_result_json,
            "pipeline_state": task.latest_result_json["pipeline_state"],
        },
    )

    with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.commit_checkpoint"):
        assert orchestrator.resume_task(task=task, actor_name="tester")

    assert seen == [["src/a.py"]]
    assert task.plan_json["must_touch_files"] == ["src/a.py"]


def test_resumability_disabled_does_not_resume() -> None:
    plan = _plan()
    task = _task(plan)
    orchestrator = _orchestrator()
    orchestrator.tool_gateway.settings.resumability_enabled = False
    write_task_checkpoint(
        orchestrator.db,
        task=task,
        stage="plan",
        output_payload={"translation_json": task.translation_json, "plan_json": task.plan_json},
    )

    assert not orchestrator.resume_task(task=task, actor_name="tester")


def test_orphan_sweep_handles_too_old_tasks(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    monkeypatch.setattr(main_module, "SessionLocal", TestingSessionLocal)
    settings = main_module.get_settings()
    settings.resumability_enabled = True
    settings.resumability_orphan_threshold_hours = 6

    db = TestingSessionLocal()
    try:
        task = Task(
            id="old-resume-task",
            title="old resume",
            request_text="implement TEST-1",
            scenario="jira_issue_develop",
            status=TaskStatus.EXECUTING,
            workflow_stage=WorkflowStage.ACTION,
            pending_approval=False,
        )
        db.add(task)
        db.flush()
        write_task_checkpoint(db, task=task, stage="codegen", output_payload={"task_id": task.id})
        checkpoint_json = dict(task.latest_checkpoint_json)
        checkpoint_json["completed_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=7)
        ).isoformat()
        task.latest_checkpoint_json = checkpoint_json
        db.commit()
    finally:
        db.close()

    main_module._sweep_orphaned_tasks()

    check = TestingSessionLocal()
    try:
        swept = check.get(Task, "old-resume-task")
        assert swept is not None
        assert swept.status == TaskStatus.FAILED
        event = check.scalars(
            select(Event).where(
                Event.task_id == swept.id,
                Event.event_type == EventType.FINAL_RESPONSE_EMITTED,
            )
        ).first()
        assert event is not None
    finally:
        check.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
