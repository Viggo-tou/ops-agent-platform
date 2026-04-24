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
) -> Event:
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
    db.add(event)
    db.flush()
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
