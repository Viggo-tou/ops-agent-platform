from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import ActorRole, RiskCategory, TaskStatus
from app.core.security import ActorContext, require_permission
from app.schemas.event import EventRead
from app.schemas.task import TaskCreateRequest, TaskDetail, TaskRollbackRequest, TaskSummary
from app.schemas.tool import ToolExecutionRead
from app.services.tasks import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])
DbSession = Annotated[Session, Depends(get_db)]
TaskCreateActorCtx = Annotated[ActorContext, Depends(require_permission("task:create"))]
ApprovalDecisionActorCtx = Annotated[ActorContext, Depends(require_permission("approval:decide"))]


@router.post("", response_model=TaskDetail, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreateRequest, db: DbSession, _actor: TaskCreateActorCtx) -> TaskDetail:
    # TODO: task:create_high_risk gate based on risk_level
    service = TaskService(db)
    task = service.create_task(payload)
    return task


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


@router.get("/{task_id}/tool-executions", response_model=list[ToolExecutionRead])
def list_task_tool_executions(task_id: str, db: DbSession) -> list[ToolExecutionRead]:
    service = TaskService(db)
    if not service.task_exists(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return service.list_tool_executions(task_id)


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
