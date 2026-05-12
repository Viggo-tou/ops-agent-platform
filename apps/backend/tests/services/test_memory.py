from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def _settings(**overrides) -> Settings:
    values = {
        "memory_enabled": True,
        "memory_judge_provider": "mock",
        "memory_dedup_threshold": 0.85,
    }
    values.update(overrides)
    return Settings(**values)


def _task(db: Session, task_id: str = "task-1", status: TaskStatus = TaskStatus.EXECUTING) -> Task:
    now = datetime.now(timezone.utc)
    task = Task(
        id=task_id,
        actor_name="tester",
        title="Memory task",
        request_text="Fix compile gate failure in package.json",
        scenario="jira_issue_develop",
        status=status,
        workflow_stage=WorkflowStage.DONE if status == TaskStatus.COMPLETED else WorkflowStage.ACTION,
        latest_result_json={
            "status": status.value,
            "message": "Compile gate resolved after changing package.json.",
            "result": {
                "summary": "Fixed malformed package config.",
                "files_changed": ["package.json"],
                "diff": "diff --git a/package.json b/package.json\n",
            },
        },
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    return task


def test_cheap_prefilter_rejects_short_and_overlong(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())

    assert service._cheap_prefilter("too short") is False
    assert service._cheap_prefilter("x" * 4001) is False
    assert service._cheap_prefilter("Compile gate failed on package.json with a reusable parse error.") is True


def test_disabled_memory_returns_none(db_session: Session) -> None:
    service = MemoryService(db_session, _settings(memory_enabled=False))

    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )

    assert memory is None
    assert db_session.scalars(select(AgentMemory)).all() == []


def test_skip_judge_records_pending_memory(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())

    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
        confidence=0.5,
    )

    assert memory is not None
    assert memory.scope == "gate:compile_gate"
    assert memory.confidence == 0.5
    assert memory.last_used_at is None


def test_query_excludes_pending_memory(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())
    service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )

    assert service.query(scope="gate:compile_gate", kind=GATE_MEMORY_KIND, text_hint="package json") == []


def test_query_returns_promoted_memory_and_increments_usage(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )
    assert memory is not None
    memory.last_used_at = datetime.now(timezone.utc)
    service._upsert_fts(memory)
    db_session.flush()

    results = service.query(scope="gate:compile_gate", kind=GATE_MEMORY_KIND, text_hint="npm package json")

    assert [item.id for item in results] == [memory.id]
    assert results[0].usage_count == 1


def test_query_filters_by_kind(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())
    wanted = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text="Fix package.json before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )
    other = service.maybe_record(
        observation_text="Repository context says package scripts are intentionally strict.",
        resolution_text="Keep package scripts valid JSON.",
        scope="gate:compile_gate",
        kind="repo_context",
        skip_judge=True,
    )
    assert wanted is not None and other is not None
    for memory in (wanted, other):
        memory.last_used_at = datetime.now(timezone.utc)
        service._upsert_fts(memory)
    db_session.flush()

    results = service.query(scope="gate:compile_gate", kind=GATE_MEMORY_KIND, text_hint="package scripts")

    assert [item.id for item in results] == [wanted.id]


def test_dedup_merges_near_duplicate_via_fts(db_session: Session) -> None:
    service = MemoryService(db_session, _settings(memory_dedup_threshold=0.8))
    first = service.maybe_record(
        observation_text="Compile gate failed because package.json had a trailing comma in scripts and npm could not parse it.",
        resolution_text="Remove the trailing comma before rerunning the compile gate.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )
    second = service.maybe_record(
        observation_text="Compile gate failed because package.json had a trailing comma in scripts, so npm could not parse it.",
        resolution_text="Remove the trailing comma before rerunning compile.",
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        skip_judge=True,
    )

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(db_session.scalars(select(AgentMemory)).all()) == 1


def test_promote_pending_on_completed_task_updates_resolution(db_session: Session) -> None:
    task = _task(db_session, status=TaskStatus.COMPLETED)
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text=None,
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )

    promoted = service.promote_pending(task_id=task.id)

    assert promoted == 1
    assert memory is not None
    assert memory.last_used_at is not None
    assert "package.json" in memory.resolution


def test_promote_pending_on_awaiting_approval_task(db_session: Session) -> None:
    task = _task(db_session, status=TaskStatus.AWAITING_APPROVAL)
    service = MemoryService(db_session, _settings())
    service.maybe_record(
        observation_text="Evidence chain failed because a changed file lacked a citation.",
        resolution_text=None,
        scope="gate:evidence_chain",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )

    assert service.promote_pending(task_id=task.id) == 1


def test_promote_pending_keeps_unresolved_task_pending(db_session: Session) -> None:
    task = _task(db_session, status=TaskStatus.EXECUTING)
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text=None,
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )

    assert service.promote_pending(task_id=task.id) == 0
    assert memory is not None
    assert memory.last_used_at is None


def test_promote_pending_enforces_twenty_four_hour_window(db_session: Session) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=3)
    task = _task(db_session, status=TaskStatus.COMPLETED)
    task.updated_at = old + timedelta(days=2)
    service = MemoryService(db_session, _settings())
    memory = service.maybe_record(
        observation_text="Compile gate failed because package.json could not be parsed by npm.",
        resolution_text=None,
        scope="gate:compile_gate",
        kind=GATE_MEMORY_KIND,
        provenance_task_id=task.id,
        skip_judge=True,
    )
    assert memory is not None
    memory.created_at = old
    db_session.flush()

    assert service.promote_pending(task_id=task.id) == 0
    assert memory.last_used_at is None


def test_attach_provenance_lines_includes_scope_usage_confidence_and_task(db_session: Session) -> None:
    service = MemoryService(db_session, _settings())
    memory = AgentMemory(
        scope="gate:compile_gate",
        key="k",
        kind=GATE_MEMORY_KIND,
        observation="Compile gate failed on package.json.",
        resolution="Fix malformed JSON before rerunning compile.",
        provenance_task_id="task-abc",
        confidence=0.5,
        usage_count=4,
        last_used_at=datetime.now(timezone.utc),
    )

    rendered = service.attach_provenance_lines([memory])

    assert "memory:gate_failure_resolution" in rendered
    assert "scope:gate:compile_gate" in rendered
    assert "used 4x" in rendered
    assert "confidence 0.5" in rendered
    assert "from task task-abc" in rendered


def test_gate_event_records_compile_scope_and_provenance(db_session: Session) -> None:
    task = _task(db_session, status=TaskStatus.EXECUTING)
    event = Event(
        task_id=task.id,
        event_type=EventType.COMPILE_FAILED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.REVIEW,
        role=RoleName.REVIEWER,
        tool_name="compile_failed",
        message="Compile verification failed; attempting repair within allowed files.",
        payload_json={"errors": [{"file": "package.json", "error": "JSON parse failed"}]},
    )
    db_session.add(event)
    db_session.flush()
    service = MemoryService(db_session, _settings())

    memory = service.maybe_record_gate_event(event=event, task=task)

    assert memory is not None
    assert memory.scope == "gate:compile_gate"
    assert memory.provenance_event_id == event.id
    assert memory.provenance_task_id == task.id


def test_gate_event_records_tool_failed_with_tool_scope(db_session: Session) -> None:
    """T1.1: TOOL_FAILED events should be recorded with tool:<name> scope."""
    task = _task(db_session, status=TaskStatus.EXECUTING)
    event = Event(
        task_id=task.id,
        event_type=EventType.TOOL_FAILED,
        source=EventSource.TOOL_GATEWAY,
        stage=WorkflowStage.PLANNING,
        role=RoleName.PLANNER,
        tool_name="jira.get_issue",
        message="Jira issue context fetch failed (HTTP 401).",
        payload_json={
            "error_kind": "auth_expired",
            "http_status": 401,
            "error_message": "Jira authentication failed (HTTP 401 on /myself).",
        },
    )
    db_session.add(event)
    db_session.flush()
    service = MemoryService(db_session, _settings())

    memory = service.maybe_record_gate_event(event=event, task=task)

    assert memory is not None
    assert memory.scope == "tool:jira"
    assert memory.provenance_event_id == event.id


def test_gate_event_records_tool_timed_out(db_session: Session) -> None:
    """T1.1: TOOL_TIMED_OUT events should also be recorded."""
    task = _task(db_session, status=TaskStatus.EXECUTING)
    event = Event(
        task_id=task.id,
        event_type=EventType.TOOL_TIMED_OUT,
        source=EventSource.TOOL_GATEWAY,
        stage=WorkflowStage.PLANNING,
        role=RoleName.PLANNER,
        tool_name="codegen.generate_patch",
        message="Codegen exceeded per-call timeout of 30s.",
        payload_json={
            "reason": "external_api_timeout",
            "provider_name": "deepseek",
            "duration_ms": 30000,
        },
    )
    db_session.add(event)
    db_session.flush()
    service = MemoryService(db_session, _settings())

    memory = service.maybe_record_gate_event(event=event, task=task)

    assert memory is not None
    assert memory.scope == "tool:codegen"
