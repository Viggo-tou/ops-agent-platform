from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.core.pipeline_executor as pipeline_executor
import app.services.tasks as tasks_module
from app.api.health import _collect_health
from app.core.db import set_sqlite_pragmas
from app.core.enums import EventType, TaskStatus, WorkflowStage
from app.core.pipeline_executor import (
    init_pipeline_executor,
    queue_depth,
    set_pipeline_executor_override,
    shutdown_pipeline_executor,
    submit_pipeline_job,
)
from app.models.base import Base
from app.models.event import Event
from app.models.task import Task
from app.orchestrator.service import PrimaryOrchestrator
from app.services.tasks import run_pipeline_job


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


def _insert_task(db: Session, *, task_id: str = "task-1") -> Task:
    task = Task(
        id=task_id,
        title="Pipeline reliability test",
        request_text="Look up TEST-1",
        scenario="jira_issue_plan",
        status=TaskStatus.CREATED,
        workflow_stage=WorkflowStage.INTAKE,
    )
    db.add(task)
    db.commit()
    return task


def test_db_wal_mode_set_on_connect(tmp_path) -> None:
    db_path = tmp_path / "wal-test.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    event.listen(engine, "connect", lambda dbapi_conn, _record: set_sqlite_pragmas(dbapi_conn))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE smoke (id INTEGER PRIMARY KEY)")
            connection.commit()
            journal_mode = str(connection.exec_driver_sql("PRAGMA journal_mode").scalar() or "").lower()
            busy_timeout = int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0)
            synchronous = int(connection.exec_driver_sql("PRAGMA synchronous").scalar() or -1)
    finally:
        engine.dispose()

    assert journal_mode == "wal"
    assert busy_timeout == 30000
    assert synchronous == 1


def test_external_api_timeout_marks_task_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(tasks_module, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    task = _insert_task(db, task_id="timeout-task")
    db.close()

    def raise_timeout(self: PrimaryOrchestrator, task: Task, *, actor_name: str) -> None:
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(PrimaryOrchestrator, "bootstrap_task", raise_timeout)

    check: Session | None = None
    try:
        run_pipeline_job(task.id, "tester")

        check = TestingSessionLocal()
        failed_task = check.get(Task, task.id)
        assert failed_task is not None
        assert failed_task.status == TaskStatus.FAILED
        failed_event = check.scalars(
            select(Event)
            .where(Event.task_id == task.id, Event.event_type == EventType.EXECUTION_FAILED)
            .order_by(Event.created_at.desc())
        ).first()
        assert failed_event is not None
        assert failed_event.payload_json["reason"] == "external_api_timeout"
        assert failed_event.payload_json["error_type"] == "ReadTimeout"
    finally:
        if check is not None:
            check.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _start_blocking_pipeline_jobs(job_count: int) -> tuple[threading.Event, list[Future]]:
    set_pipeline_executor_override(None)
    shutdown_pipeline_executor(wait=False)
    init_pipeline_executor(2)

    release = threading.Event()
    both_started = threading.Event()
    started = 0
    lock = threading.Lock()

    def block() -> None:
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        release.wait(timeout=5)

    futures = [submit_pipeline_job(block) for _ in range(job_count)]
    assert both_started.wait(timeout=2)
    return release, futures


def test_health_reports_pipeline_workers_active(db_session: Session) -> None:
    release, futures = _start_blocking_pipeline_jobs(2)
    try:
        payload, _health = _collect_health(db_session)

        assert payload["pipeline_workers"]["active"] == 2
        assert payload["pipeline_workers"]["max"] == 2
        assert payload["pipeline_workers"]["queue_depth"] == 0
    finally:
        release.set()
        for future in futures:
            future.result(timeout=5)
        shutdown_pipeline_executor(wait=True)


def test_health_returns_degraded_when_workers_jammed(db_session: Session) -> None:
    release, futures = _start_blocking_pipeline_jobs(3)
    try:
        deadline = time.monotonic() + 2
        while queue_depth() != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queue_depth() == 1

        with pipeline_executor._lock:  # noqa: SLF001
            pipeline_executor._saturated_since = time.monotonic() - 121  # noqa: SLF001

        payload, _health = _collect_health(db_session)

        assert payload["status"] == "degraded"
        assert payload["pipeline_workers"]["active"] == 2
        assert payload["pipeline_workers"]["queue_depth"] == 1
    finally:
        release.set()
        for future in futures:
            future.result(timeout=5)
        shutdown_pipeline_executor(wait=True)


def test_jira_fetch_emits_started_succeeded_events(db_session: Session) -> None:
    task = _insert_task(db_session, task_id="jira-task")
    orchestrator = PrimaryOrchestrator(db_session)
    orchestrator.tool_gateway.execute = Mock(
        return_value={
            "issue_key": "TEST-1",
            "summary": "Implement retry settings",
            "description": "Add retry settings.",
        }
    )

    result = orchestrator._prefetch_jira_issue_context(
        task=task,
        actor_name="tester",
        issue_key="TEST-1",
    )

    assert result is not None
    events = list(
        db_session.scalars(
            select(Event).where(Event.task_id == task.id).order_by(Event.created_at.asc())
        )
    )
    event_types = [event.event_type for event in events]
    assert EventType.JIRA_FETCH_STARTED in event_types
    assert EventType.JIRA_FETCH_SUCCEEDED in event_types

    started_event = next(event for event in events if event.event_type == EventType.JIRA_FETCH_STARTED)
    succeeded_event = next(event for event in events if event.event_type == EventType.JIRA_FETCH_SUCCEEDED)
    assert started_event.payload_json["provider_name"] == "jira"
    assert started_event.payload_json["request_size_bytes"] > 0
    assert succeeded_event.payload_json["provider_name"] == "jira"
    assert succeeded_event.payload_json["duration_ms"] >= 0
