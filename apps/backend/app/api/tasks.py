from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Annotated, Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.event import Event as EventModel
from app.models.task import Task as TaskModel

from app.core.db import get_db
from app.core.enums import (
    ActorRole,
    EventSource,
    EventType,
    RiskCategory,
    RoleName,
    TaskStatus,
    WorkflowStage,
)
from app.core.security import ActorContext, require_permission
from app.models.task import Task
from app.schemas.event import EventRead
from app.schemas.task import TaskCreateRequest, TaskDetail, TaskRollbackRequest, TaskSummary
from app.schemas.tool import ToolExecutionRead
from app.services.events import record_event, set_task_status
from app.services import task_cancel
from app.services.tasks import TaskService
from app.services.task_workspace import TaskWorkspace

router = APIRouter(prefix="/tasks", tags=["tasks"])
DbSession = Annotated[Session, Depends(get_db)]
TaskCreateActorCtx = Annotated[ActorContext, Depends(require_permission("task:create"))]
ApprovalDecisionActorCtx = Annotated[ActorContext, Depends(require_permission("approval:decide"))]


@router.post("", response_model=TaskDetail, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreateRequest, db: DbSession, _actor: TaskCreateActorCtx) -> TaskDetail:
    """Return the initial CREATED task; clients should poll GET /tasks/{id} for progress."""
    # TODO: task:create_high_risk gate based on risk_level
    service = TaskService(db)
    try:
        task = service.create_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return task


# Iteration / follow-up: spawn a child task linked to the parent.
# The existing continuation chain in TaskService._build_continuation_request
# already pulls the parent's plan + diff + compile errors + rejected patches
# into the new task's prompt, so this endpoint is mostly a thin wrapper
# that saves the frontend from building the larger TaskCreateRequest.

from pydantic import BaseModel as _PydBaseModel  # noqa: E402


class _IterateBody(_PydBaseModel):
    follow_up: str
    actor_name: str | None = None


# Statuses where the parent is mid-flight; iterating here would race the
# in-flight pipeline with the new continuation pipeline.
_ITERATE_BLOCKED_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.REVIEWING,
    TaskStatus.EXECUTING,
    TaskStatus.RUNNING,
    TaskStatus.QUEUED,
}


class _DiagnoseResponse(_PydBaseModel):
    summary: str
    root_cause: str
    likely_fix: str
    confidence: str
    related_files: list[str]


@router.post("/{task_id}/diagnose", response_model=_DiagnoseResponse)
def diagnose_task(
    task_id: str,
    db: DbSession,
    actor: TaskCreateActorCtx,
) -> _DiagnoseResponse:
    """Run the failure-diagnosis agent on a finished task.

    Reads compile.json + rejected diffs + review findings from the task's
    workspace, asks the primary LLM to produce a JSON diagnosis, persists
    it to task.latest_result_json.failure_diagnosis, and returns it.
    """
    if not db.get(TaskModel, task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    from app.services.diagnostic import DiagnosticError, run_diagnostic
    try:
        diag = run_diagnostic(task_id)
    except DiagnosticError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _DiagnoseResponse(**diag)


@router.post("/{task_id}/iterate", response_model=TaskDetail, status_code=status.HTTP_201_CREATED)
def iterate_task(
    task_id: str,
    body: _IterateBody,
    db: DbSession,
    actor: TaskCreateActorCtx,
) -> TaskDetail:
    """Create a child task that amends the parent's result with a follow-up instruction.

    Reuses TaskService.create_task() with previous_task_id set, so the new task
    inherits the parent's scenario, session, and an augmented prompt that
    contains the parent's plan + result + compile errors + rejected diffs.
    """
    parent = db.get(TaskModel, task_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    if parent.status in _ITERATE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Parent task is still running. Wait for it to finish or pause "
                "for approval before iterating."
            ),
        )

    follow_up = (body.follow_up or "").strip()
    if not follow_up:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="follow_up is required.",
        )

    payload = TaskCreateRequest(
        request=follow_up,
        actor_name=body.actor_name or parent.actor_name or "operator",
        actor_role=parent.actor_role,
        session_id=parent.session_id,
        previous_task_id=parent.id,
        source_name=parent.source_name,
    )

    service = TaskService(db)
    try:
        return service.create_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=list[TaskSummary])
def list_tasks(
    db: DbSession,
    search: str | None = None,
    session_id: str | None = None,
    status: TaskStatus | None = None,
    provider: str | None = None,
    actor_role: ActorRole | None = None,
    risk_category: RiskCategory | None = None,
) -> list[TaskSummary]:
    service = TaskService(db)
    return service.list_tasks(
        search=search,
        session_id=session_id,
        status=status,
        provider=provider,
        actor_role=actor_role,
        risk_category=risk_category,
    )


@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: str, db: DbSession) -> TaskDetail:
    service = TaskService(db)
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task


@router.get("/{task_id}/events", response_model=list[EventRead])
def list_task_events(task_id: str, db: DbSession) -> list[EventRead]:
    service = TaskService(db)
    if not service.task_exists(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return service.list_events(task_id)


# --- SSE event stream -------------------------------------------------------

# Terminal task statuses that close the stream.
_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.ROLLED_BACK,
}
# Statuses that pause the stream (waiting for human action) but don't close it.
# We still stop polling but mark this in the `done` payload so the frontend can
# show "approval needed" without spinning.
_PAUSED_STATUSES = {
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.WAITING_APPROVAL,
}

# How long to keep the connection open in seconds before forcibly closing.
# Keeps proxies happy and prevents zombie streams when a client drops.
_STREAM_HARD_TIMEOUT_S = 30 * 60  # 30 min
_HEARTBEAT_INTERVAL_S = 25  # under most proxy 30s timeouts
_POLL_INTERVAL_S = 1.0


def _serialize_event(ev: EventModel) -> dict[str, Any]:
    return {
        "id": ev.id,
        "task_id": ev.task_id,
        "session_id": ev.session_id,
        "event_type": ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type),
        "source": ev.source.value if hasattr(ev.source, "value") else str(ev.source),
        "stage": ev.stage.value if (ev.stage and hasattr(ev.stage, "value")) else (ev.stage if ev.stage else None),
        "role": ev.role.value if (ev.role and hasattr(ev.role, "value")) else (ev.role if ev.role else None),
        "tool_name": ev.tool_name,
        "message": ev.message,
        "payload_json": ev.payload_json,
        "created_at": ev.created_at.isoformat() if isinstance(ev.created_at, datetime) else str(ev.created_at),
    }


def _serialize_task_min(task: TaskModel) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "scenario": task.scenario,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "workflow_stage": (
            task.workflow_stage.value
            if hasattr(task.workflow_stage, "value")
            else str(task.workflow_stage)
        ),
        "current_role": (
            task.current_role.value
            if (task.current_role and hasattr(task.current_role, "value"))
            else (task.current_role if task.current_role else None)
        ),
        "pending_approval": task.pending_approval,
        "updated_at": task.updated_at.isoformat() if isinstance(task.updated_at, datetime) else str(task.updated_at),
    }


def _format_sse(event_name: str, data: dict[str, Any], event_id: str | None = None) -> str:
    parts: list[str] = []
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event_name}")
    parts.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    parts.append("")  # blank line terminator
    parts.append("")
    return "\n".join(parts)


async def _task_event_stream(
    task_id: str,
    last_event_id: str | None,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE-formatted bytes for a task's lifecycle.

    Polling-based: we re-open a short-lived DB session each tick rather than
    instrumenting every record_event() caller to push to a queue. Events live
    in the `event` table so polling sees them just fine.
    """
    started_at = time.monotonic()

    # Initial snapshot.
    db: Session = SessionLocal()
    try:
        task = db.query(TaskModel).filter(TaskModel.id == task_id).one_or_none()
        if task is None:
            yield _format_sse(
                "error",
                {"code": "not_found", "message": "Task not found."},
            ).encode("utf-8")
            return

        events = (
            db.query(EventModel)
            .filter(EventModel.task_id == task_id)
            .order_by(EventModel.created_at.asc(), EventModel.id.asc())
            .all()
        )
        snapshot_payload = {
            "task": _serialize_task_min(task),
            "events": [_serialize_event(e) for e in events],
        }
        last_seen_event_id = events[-1].id if events else None
        last_seen_event_ts = events[-1].created_at if events else None
        last_status = task.status
    finally:
        db.close()

    yield _format_sse("snapshot", snapshot_payload, event_id=last_seen_event_id).encode("utf-8")

    # If we already loaded a Last-Event-ID and the snapshot already included it,
    # skip ahead. (Last-Event-ID replay logic is handled implicitly: snapshot
    # always contains the full timeline so any reconnect resumes from there.)
    _ = last_event_id  # reserved for future delta-replay optimization

    last_heartbeat = time.monotonic()
    terminal_reason: str | None = None

    while True:
        # Hard timeout safety net.
        if time.monotonic() - started_at > _STREAM_HARD_TIMEOUT_S:
            terminal_reason = "stream_timeout"
            break

        await asyncio.sleep(_POLL_INTERVAL_S)

        db = SessionLocal()
        try:
            task = db.query(TaskModel).filter(TaskModel.id == task_id).one_or_none()
            if task is None:
                terminal_reason = "task_deleted"
                break

            # Status transitions.
            if task.status != last_status:
                yield _format_sse(
                    "status",
                    {
                        "previous_status": last_status.value
                        if hasattr(last_status, "value")
                        else str(last_status),
                        "status": task.status.value
                        if hasattr(task.status, "value")
                        else str(task.status),
                        "workflow_stage": task.workflow_stage.value
                        if hasattr(task.workflow_stage, "value")
                        else str(task.workflow_stage),
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                ).encode("utf-8")
                last_status = task.status

            # New events since last_seen_event_ts.
            q = db.query(EventModel).filter(EventModel.task_id == task_id)
            if last_seen_event_ts is not None:
                # Use both timestamp + id to handle equal timestamps deterministically.
                q = q.filter(EventModel.created_at >= last_seen_event_ts)
            q = q.order_by(EventModel.created_at.asc(), EventModel.id.asc())
            new_events = [
                e for e in q.all() if e.id != last_seen_event_id and (
                    last_seen_event_ts is None or e.created_at > last_seen_event_ts
                    or (e.created_at == last_seen_event_ts and e.id > (last_seen_event_id or ""))
                )
            ]
            if new_events:
                for ev in new_events:
                    yield _format_sse(
                        "log",
                        _serialize_event(ev),
                        event_id=ev.id,
                    ).encode("utf-8")
                    last_seen_event_id = ev.id
                    last_seen_event_ts = ev.created_at
                last_heartbeat = time.monotonic()

            # Terminal / paused?
            if task.status in _TERMINAL_STATUSES:
                terminal_reason = "terminal"
                final_status = task.status
                final_stage = task.workflow_stage
                break
            if task.status in _PAUSED_STATUSES:
                # Send a one-shot paused done so the client stops spinning, but
                # the client can re-open the stream later (e.g. after approval)
                # to keep watching. We don't return mid-loop because the task
                # may transition again on the next tick.
                # We use a separate event name so the client distinguishes it.
                yield _format_sse(
                    "paused",
                    {
                        "status": task.status.value
                        if hasattr(task.status, "value")
                        else str(task.status),
                        "workflow_stage": task.workflow_stage.value
                        if hasattr(task.workflow_stage, "value")
                        else str(task.workflow_stage),
                    },
                ).encode("utf-8")
                # Hold the connection but stop polling tightly: heartbeat-only.
                # If the user grants/rejects approval the status will flip and
                # we'll catch it on the next slow-tick.
                await asyncio.sleep(5.0)
                continue
        finally:
            db.close()

        # Heartbeat if quiet.
        if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
            yield _format_sse(
                "heartbeat",
                {"ts": int(time.time())},
            ).encode("utf-8")
            last_heartbeat = time.monotonic()

    # Terminal: emit `done` once and close.
    if terminal_reason == "terminal":
        yield _format_sse(
            "done",
            {
                "final_status": final_status.value
                if hasattr(final_status, "value")
                else str(final_status),
                "final_stage": final_stage.value
                if hasattr(final_stage, "value")
                else str(final_stage),
            },
        ).encode("utf-8")
    else:
        yield _format_sse(
            "done",
            {"final_status": "stream_closed", "reason": terminal_reason or "unknown"},
        ).encode("utf-8")


@router.get("/{task_id}/events/stream")
async def stream_task_events(
    task_id: str,
    db: DbSession,
    last_event_id: str | None = None,
) -> StreamingResponse:
    """SSE: live stream of task lifecycle events.

    Sends `snapshot` once with current task + all historical events, then
    polls every second and emits `log` events as new rows appear. Sends
    `status` on status transitions, `paused` on awaiting_approval, `heartbeat`
    every ~25s when quiet, and `done` on terminal status (then closes).

    Auth: same as the underlying GET /api/tasks/{id} (no extra check —
    callers already need a valid actor header to reach this router).
    """
    service = TaskService(db)
    if not service.task_exists(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    return StreamingResponse(
        _task_event_stream(task_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
            "Connection": "keep-alive",
        },
    )


@router.get("/{task_id}/tool-executions", response_model=list[ToolExecutionRead])
def list_task_tool_executions(task_id: str, db: DbSession) -> list[ToolExecutionRead]:
    service = TaskService(db)
    if not service.task_exists(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return service.list_tool_executions(task_id)


@router.get("/{task_id}/workspace/checkpoint", response_model=dict[str, Any])
def get_task_workspace_checkpoint(task_id: str, db: DbSession) -> dict[str, Any]:
    service = TaskService(db)
    if not service.task_exists(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    checkpoint = TaskWorkspace.for_task(task_id).read_checkpoint()
    return checkpoint or {}


@router.post("/{task_id}/abandon", response_model=TaskDetail)
def abandon_task(
    task_id: str,
    db: DbSession,
    _actor: ApprovalDecisionActorCtx,
) -> TaskDetail:
    """Force-fail an executing task so operators can recover a stuck slot."""
    service = TaskService(db)
    task_model = db.get(Task, task_id)
    if task_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    if task_model.status not in (TaskStatus.EXECUTING, TaskStatus.CREATED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is in status {task_model.status.value}; cannot abandon.",
        )

    set_task_status(
        db,
        task=task_model,
        new_status=TaskStatus.FAILED,
        new_stage=WorkflowStage.DONE,
        role=RoleName.SYSTEM,
        message="Task abandoned by admin.",
        payload={"reason": "abandoned_by_admin"},
    )
    record_event(
        db,
        task_id=task_id,
        event_type=EventType.EXECUTION_FAILED,
        source=EventSource.SYSTEM,
        stage=WorkflowStage.DONE,
        role=RoleName.SYSTEM,
        message="Task abandoned by admin.",
        payload={"reason": "abandoned_by_admin"},
    )
    db.commit()
    task_cancel.request_cancel(task_id)
    return service.get_task(task_id, raise_if_missing=True)


@router.post("/{task_id}/rollback", response_model=TaskDetail)
def rollback_task(
    task_id: str,
    payload: TaskRollbackRequest,
    db: DbSession,
    _actor: ApprovalDecisionActorCtx,
) -> TaskDetail:
    service = TaskService(db)
    try:
        return service.rollback_task(task_id=task_id, payload=payload)
    except ValueError as exc:
        if str(exc) == "Task not found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
