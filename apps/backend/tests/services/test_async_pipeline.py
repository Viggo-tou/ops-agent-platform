from __future__ import annotations

from concurrent.futures import Future

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.enums import EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.core.pipeline_executor import set_pipeline_executor_override
from app.models.base import Base
from app.models.event import Event
from app.models.task import Task
from app.orchestrator.service import PrimaryOrchestrator
from app.schemas.task import TaskCreateRequest
from app.services.events import set_task_status
from app.services.tasks import TaskService
import app.services.tasks as tasks_module


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    monkeypatch.setattr(tasks_module, "SessionLocal", testing_session_local)

    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _payload() -> TaskCreateRequest:
    return TaskCreateRequest(
        title="Async pipeline test",
        request="Locate Firebase configuration files in the repository.",
        actor_name="tester",
    )


def _complete_bootstrap(self: PrimaryOrchestrator, task: Task, *, actor_name: str) -> None:
    task.latest_result_json = {"status": TaskStatus.COMPLETED.value, "actor_name": actor_name}
    set_task_status(
        self.db,
        task=task,
        new_status=TaskStatus.COMPLETED,
        new_stage=WorkflowStage.DONE,
        role=RoleName.SYSTEM,
        source=EventSource.ORCHESTRATOR,
        message="Mock pipeline completed.",
        payload={"actor_name": actor_name},
    )


def test_executor_override_runs_pipeline_synchronously(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PrimaryOrchestrator, "bootstrap_task", _complete_bootstrap)

    task = TaskService(db_session).create_task(_payload())

    assert task.status == TaskStatus.COMPLETED
    assert task.workflow_stage == WorkflowStage.DONE


class _CaptureExecutor:
    def __init__(self) -> None:
        self.submitted: tuple[object, tuple[object, ...], dict[str, object]] | None = None

    def submit(self, fn, *args, **kwargs):
        self.submitted = (fn, args, kwargs)
        return Future()


def test_without_running_job_create_task_returns_created(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_executor = _CaptureExecutor()
    set_pipeline_executor_override(capture_executor)
    monkeypatch.setattr(PrimaryOrchestrator, "bootstrap_task", _complete_bootstrap)

    task = TaskService(db_session).create_task(_payload())

    assert task.status == TaskStatus.CREATED
    assert task.workflow_stage == WorkflowStage.INTAKE
    assert capture_executor.submitted is not None

    fn, args, kwargs = capture_executor.submitted
    fn(*args, **kwargs)

    db_session.expire_all()
    advanced_task = db_session.get(Task, task.id)
    assert advanced_task is not None
    assert advanced_task.status == TaskStatus.COMPLETED
    assert advanced_task.workflow_stage == WorkflowStage.DONE


def test_pipeline_exception_marks_task_failed_and_records_event(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_bootstrap(self: PrimaryOrchestrator, task: Task, *, actor_name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(PrimaryOrchestrator, "bootstrap_task", raise_bootstrap)

    task = TaskService(db_session).create_task(_payload())

    db_session.expire_all()
    failed_task = db_session.get(Task, task.id)
    assert failed_task is not None
    assert failed_task.status == TaskStatus.FAILED
    assert failed_task.workflow_stage == WorkflowStage.DONE

    events = list(
        db_session.scalars(
            select(Event).where(
                Event.task_id == task.id,
                Event.event_type == EventType.EXECUTION_FAILED,
            )
        )
    )
    assert events
    assert events[-1].payload_json == {"error_type": "RuntimeError", "error": "boom"}
