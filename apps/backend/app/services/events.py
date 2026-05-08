from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.core.logging import get_logger
from app.models.event import Event
from app.models.task import Task

_event_logger = get_logger(component="events")


def commit_checkpoint(db: Session, *, label: str) -> None:
    """Persist all pending pipeline state to DB at a stable boundary.

    Why: run_pipeline_job wraps the entire orchestrator in one transaction and
    only commits at the end. Calling this at stable gates (plan generated,
    review passed, codegen done, sandbox applied, compile passed, approval
    reached, before long external RPCs) makes progress visible to the UI in
    near-real time AND ensures a backend crash leaves a recoverable checkpoint
    instead of losing all mid-flight events.
    """
    try:
        db.commit()
    except Exception as exc:
        _event_logger.warning("commit_checkpoint_failed", label=label, error=str(exc)[:200])
        db.rollback()
        raise


def record_event(
    db: Session,
    *,
    task_id: str,
    event_type: EventType,
    source: EventSource,
    message: str,
    session_id: str | None = None,
    stage: WorkflowStage | None = None,
    role: RoleName | None = None,
    tool_name: str | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> Event:
    """Insert a lifecycle event row. Auto-commits by default.

    Why auto-commit: the orchestrator's run_pipeline_job wraps the ENTIRE
    pipeline in one transaction (one final ``db.commit()`` at the end).
    Inside that transaction, ~180 ``record_event`` calls each fire a
    SQLite INSERT via ``flush()``. WAL mode gives each writer an
    exclusive write lock from the first INSERT until COMMIT — held for
    the whole pipeline (5-30 min). During that window EVERY other
    writer in the process (chat persistence, /iterate, /diagnose,
    follow-up tasks) gets ``database is locked``.

    Committing after each event:
      + releases the lock after every event so other writers can slip in
      + makes progress visible to the UI immediately (better SSE feel)
      + means task.plan_json / task.status updates committed alongside
        the event are also persisted live

    Trade-off: callers that intended an atomic Task+Event batch lose
    rollback on crash. In practice that's fine here — events are
    append-only progress logs, persisting them through a crash is a
    feature not a bug, and crash recovery already re-derives state
    from the latest event sequence.

    Pass ``commit=False`` to opt out for legitimate batch-write callers.
    """
    if session_id is None:
        task = db.get(Task, task_id)
        session_id = task.session_id if task is not None else None

    event = Event(
        task_id=task_id,
        session_id=session_id,
        event_type=event_type,
        source=source,
        stage=stage,
        role=role,
        tool_name=tool_name,
        message=message,
        payload_json=payload,
    )
    # Retry-on-locked: under high write concurrency (max_workers=6, each
    # codegen batch in parallel_max=4 also writes events), SQLite's single
    # writer lock loses to concurrent writers and INSERT/COMMIT raises
    # 'database is locked' even with WAL + busy_timeout=120s. We retry up
    # to 5 times with exponential backoff (50→800ms, total ~1.5s) and
    # only re-raise if every attempt loses.
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            db.add(event)
            db.flush()
            if commit:
                db.commit()
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            db.rollback()
            msg = str(exc).lower()
            is_locked = "database is locked" in msg or "operationalerror" in msg
            if not is_locked or attempt == 4:
                _event_logger.warning(
                    "event_commit_failed",
                    error=str(exc)[:200],
                    attempts=attempt + 1,
                    is_locked=is_locked,
                )
                raise
            # Re-add the event for the retry — rollback() detached it.
            event = Event(
                task_id=task_id,
                session_id=session_id,
                event_type=event_type,
                source=source,
                stage=stage,
                role=role,
                tool_name=tool_name,
                message=message,
                payload_json=payload,
            )
            _time.sleep(0.05 * (2 ** attempt))
    _event_logger.info(
        "lifecycle_event",
        task_id=task_id,
        event_type=event_type.value if hasattr(event_type, "value") else str(event_type),
        source=source.value if hasattr(source, "value") else str(source),
        stage=stage.value if stage and hasattr(stage, "value") else str(stage),
        role=role.value if role and hasattr(role, "value") else str(role),
        tool_name=tool_name,
        message=message,
    )
    return event


def set_task_status(
    db: Session,
    *,
    task: Task,
    new_status: TaskStatus,
    new_stage: WorkflowStage,
    role: RoleName | None,
    message: str,
    source: EventSource = EventSource.SYSTEM,
    payload: dict[str, Any] | None = None,
) -> None:
    previous_status = task.status
    task.status = new_status
    task.workflow_stage = new_stage
    task.current_role = role

    status_payload = {
        "from_status": previous_status.value if previous_status else None,
        "to_status": new_status.value,
    }
    if payload:
        status_payload.update(payload)

    record_event(
        db,
        task_id=task.id,
        event_type=EventType.TASK_STATUS_CHANGED,
        source=source,
        stage=new_stage,
        role=role,
        message=message,
        payload=status_payload,
    )
