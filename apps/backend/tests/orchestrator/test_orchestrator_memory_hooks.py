from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.enums import EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.models.base import Base
from app.models.event import Event
from app.models.memory import AgentMemory
from app.models.task import Task
from app.orchestrator.service import PrimaryOrchestrator, record_event, set_task_status
from app.services.failure_diagnosis import DiagnosisOutput
from app.services.memory import GATE_MEMORY_KIND, MemoryService


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _task(db: Session, task_id: str = "task-memory") -> Task:
    now = datetime.now(timezone.utc)
    task = Task(
        id=task_id,
        actor_name="tester",
        title="Memory integration task",
        request_text="Fix compile gate failure in package.json",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        latest_result_json={
            "status": TaskStatus.COMPLETED.value,
            "message": "Compile gate fixed.",
            "result": {
                "summary": "Fixed package.json syntax.",
                "files_changed": ["package.json"],
            },
        },
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    return task


def _settings() -> Settings:
    return Settings(memory_enabled=True, memory_judge_provider="mock", memory_top_n_per_query=2)


def test_record_event_wrapper_records_gate_failure_memory(db_session: Session) -> None:
    task = _task(db_session)

    event = record_event(
        db_session,
        task_id=task.id,
        event_type=EventType.COMPILE_FAILED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.REVIEW,
        role=RoleName.REVIEWER,
        tool_name="compile_failed",
        message="Compile verification failed; attempting repair within allowed files.",
        payload={"errors": [{"file": "package.json", "error": "JSON parse failed"}]},
    )

    memory = db_session.scalars(select(AgentMemory)).one()
    assert memory.scope == "gate:compile_gate"
    assert memory.provenance_event_id == event.id
    assert memory.provenance_task_id == task.id
    assert memory.last_used_at is None


def test_set_task_status_wrapper_promotes_pending_memory(db_session: Session) -> None:
    task = _task(db_session)
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text=None,
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )

    set_task_status(
        db_session,
        task=task,
        new_status=TaskStatus.COMPLETED,
        new_stage=WorkflowStage.DONE,
        role=RoleName.PRIMARY,
        source=EventSource.ORCHESTRATOR,
        message="Task completed.",
    )

    assert memory is not None
    assert memory.last_used_at is not None
    assert "package.json" in memory.resolution


def test_codegen_memory_context_reads_active_memory(db_session: Session) -> None:
    task = _task(db_session, "task-codegen-memory")
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json syntax before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )
    assert memory is not None
    memory.last_used_at = datetime.now(timezone.utc)
    service._upsert_fts(memory)
    db_session.flush()
    orchestrator = PrimaryOrchestrator(db_session)
    orchestrator.tool_gateway.settings.memory_enabled = True
    orchestrator.tool_gateway.settings.memory_top_n_per_query = 1

    context = orchestrator._build_codegen_memory_context(task)

    assert "Observation: Compile gate failed" in context
    assert "Resolution: Fix package.json syntax" in context
    assert "from task task-codegen-memory" in context


def test_codegen_memory_context_respects_disabled_setting(db_session: Session) -> None:
    task = _task(db_session, "task-memory-disabled")
    orchestrator = PrimaryOrchestrator(db_session)
    orchestrator.tool_gateway.settings.memory_enabled = False

    assert orchestrator._build_codegen_memory_context(task) == ""


def test_failure_diagnosis_hook_records_generated_diagnosis_memory(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(db_session, "task-diagnosis-memory")
    orchestrator = PrimaryOrchestrator(db_session)
    orchestrator.tool_gateway.settings.memory_enabled = True
    orchestrator.tool_gateway.settings.memory_judge_provider = "mock"

    def fake_run_diagnosis(**kwargs):  # noqa: ANN003
        event = Event(
            task_id=task.id,
            event_type=EventType.FAILURE_DIAGNOSIS_GENERATED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            tool_name="failure_diagnosis",
            message="failure_diagnosis: high - package.json malformed",
            payload_json={
                "summary": "package.json malformed",
                "root_cause": "JSON parse failed before compile.",
                "likely_fix": "Fix package.json syntax.",
                "confidence": "high",
            },
        )
        db_session.add(event)
        db_session.flush()
        return DiagnosisOutput(
            summary="package.json malformed",
            root_cause="JSON parse failed before compile.",
            likely_fix="Fix package.json syntax.",
            confidence="high",
            related_files=["package.json"],
        )

    monkeypatch.setattr("app.orchestrator.service.run_diagnosis", fake_run_diagnosis)

    orchestrator._run_failure_diagnosis(task, failure_kind="compile_repair_cap_exceeded")

    memory = db_session.scalars(select(AgentMemory)).one()
    assert memory.scope == "gate:failure_diagnosis"
    assert "Fix package.json syntax" in memory.resolution
