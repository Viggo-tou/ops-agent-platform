from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.core.db import get_db  # noqa: E402
from app.core.enums import EventType, TaskStatus, WorkflowStage  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services import task_cancel  # noqa: E402


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
        task_cancel.clear_cancel("task-executing")
        task_cancel.clear_cancel("task-completed")
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _headers() -> dict[str, str]:
    return {
        "X-Actor-Role": "admin",
        "X-Actor-App-Role": "admin",
    }


def _task(db: Session, task_id: str, status: TaskStatus) -> Task:
    task = Task(
        id=task_id,
        actor_name="admin",
        title="Abandon me",
        request_text="stuck work",
        scenario="develop",
        status=status,
        workflow_stage=WorkflowStage.ACTION if status == TaskStatus.EXECUTING else WorkflowStage.DONE,
    )
    db.add(task)
    db.commit()
    return task


def test_abandon_executing_task_marks_failed(client: TestClient, db_session: Session) -> None:
    task = _task(db_session, "task-executing", TaskStatus.EXECUTING)

    response = client.post(f"/api/tasks/{task.id}/abandon", headers=_headers())

    assert response.status_code == 200
    assert response.json()["status"] == TaskStatus.FAILED.value
    db_session.expire_all()
    task_model = db_session.get(Task, task.id)
    assert task_model is not None
    assert task_model.status == TaskStatus.FAILED
    assert task_model.workflow_stage == WorkflowStage.DONE
    events = db_session.scalars(
        select(Event).where(
            Event.task_id == task.id,
            Event.event_type == EventType.EXECUTION_FAILED,
        )
    ).all()
    assert len(events) == 1
    assert events[0].payload_json == {"reason": "abandoned_by_admin"}
    assert task_cancel.is_cancelled(task.id)


def test_abandon_completed_task_returns_400(client: TestClient, db_session: Session) -> None:
    task = _task(db_session, "task-completed", TaskStatus.COMPLETED)

    response = client.post(f"/api/tasks/{task.id}/abandon", headers=_headers())

    assert response.status_code == 400
    assert "cannot abandon" in response.json()["detail"]
    assert not task_cancel.is_cancelled(task.id)
