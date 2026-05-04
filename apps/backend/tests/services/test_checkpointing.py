from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.enums import TaskStatus, WorkflowStage
from app.models.base import Base
from app.models.task import Task
from app.services.checkpointing import (
    find_resumable_tasks,
    is_task_resumable,
    read_task_checkpoint,
    write_task_checkpoint,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    session._test_engine = engine  # type: ignore[attr-defined]
    return session


def _close(session: Session) -> None:
    engine = session._test_engine  # type: ignore[attr-defined]
    session.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _task(task_id: str = "task-1") -> Task:
    return Task(
        id=task_id,
        title="checkpoint test",
        request_text="implement TEST-1",
        scenario="jira_issue_develop",
        status=TaskStatus.EXECUTING,
        workflow_stage=WorkflowStage.ACTION,
        plan_json={"must_touch_files": ["src/a.py"], "verification_profile": {"mode": "compile"}},
    )


def test_checkpoint_roundtrips_stage_payload_and_plan_contract() -> None:
    db = _session()
    try:
        task = _task()
        db.add(task)
        db.flush()

        write_task_checkpoint(
            db,
            task=task,
            stage="compile",
            output_payload={
                "plan_json": task.plan_json,
                "pipeline_state": {
                    "compile_gate_done": True,
                    "compile_gate": {"passed": True, "verified_by": "compile"},
                },
            },
            sandbox_snapshot_id="git:abc123",
        )

        checkpoint = read_task_checkpoint(task)

        assert checkpoint is not None
        assert checkpoint.stage == "compile"
        assert checkpoint.sandbox_snapshot_id == "git:abc123"
        assert checkpoint.output_payload["plan_json"]["must_touch_files"] == ["src/a.py"]
        assert checkpoint.output_payload["pipeline_state"]["compile_gate"]["verified_by"] == "compile"
    finally:
        _close(db)


def test_find_resumable_tasks_filters_old_pending_and_terminal_tasks() -> None:
    db = _session()
    try:
        fresh = _task("fresh")
        old = _task("old")
        pending = _task("pending")
        done = _task("done")
        pending.pending_approval = True
        done.status = TaskStatus.COMPLETED
        db.add_all([fresh, old, pending, done])
        db.flush()

        now = datetime.now(timezone.utc)
        for task in (fresh, old, pending, done):
            write_task_checkpoint(db, task=task, stage="codegen", output_payload={"task_id": task.id})
        old_checkpoint_json = dict(old.latest_checkpoint_json)
        old_checkpoint_json["completed_at"] = (now - timedelta(hours=7)).isoformat()
        old.latest_checkpoint_json = old_checkpoint_json

        resumable = find_resumable_tasks(db, max_age_hours=6, now=now)

        assert [task.id for task in resumable] == ["fresh"]
        assert is_task_resumable(fresh, max_age_hours=6, now=now)
        assert not is_task_resumable(old, max_age_hours=6, now=now)
        assert not is_task_resumable(pending, max_age_hours=6, now=now)
        assert not is_task_resumable(done, max_age_hours=6, now=now)
    finally:
        _close(db)
