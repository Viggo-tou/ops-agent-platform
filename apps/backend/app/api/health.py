from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import ApprovalStatus, TaskStatus, ToolExecutionStatus
from app.models.approval import Approval
from app.models.task import Task
from app.models.tool_execution import ToolExecution
from app.services.alerts import AlertEngine, AlertResult, HealthData, WebhookDispatcher

router = APIRouter(tags=["health"])
APP_STARTED_AT = datetime.now(timezone.utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _count_tasks(db: Session, status: TaskStatus, since: datetime) -> int:
    statement = select(func.count()).select_from(Task).where(Task.status == status, Task.created_at >= since)
    return int(db.scalar(statement) or 0)


def _collect_health(db: Session) -> tuple[dict[str, object], HealthData]:
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    try:
        db.execute(text("SELECT 1"))
        db_connected = True

        last_successful_task_at = db.scalar(
            select(Task.updated_at)
            .where(Task.status == TaskStatus.COMPLETED)
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
        pending_approval_count = int(
            db.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.status == ApprovalStatus.PENDING)
            )
            or 0
        )
        completed_1h = _count_tasks(db, TaskStatus.COMPLETED, one_hour_ago)
        failed_1h = _count_tasks(db, TaskStatus.FAILED, one_hour_ago)
        total_1h = int(
            db.scalar(select(func.count()).select_from(Task).where(Task.created_at >= one_hour_ago)) or 0
        )

        total_tool_executions_1h = int(
            db.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(ToolExecution.started_at >= one_hour_ago)
            )
            or 0
        )
        failed_tool_executions_1h = int(
            db.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(
                    ToolExecution.started_at >= one_hour_ago,
                    ToolExecution.status.in_(
                        [ToolExecutionStatus.FAILED, ToolExecutionStatus.TIMED_OUT]
                    ),
                )
            )
            or 0
        )
        tool_failure_rate_1h = (
            failed_tool_executions_1h / total_tool_executions_1h
            if total_tool_executions_1h
            else 0.0
        )
    except Exception:
        db_connected = False
        last_successful_task_at = None
        pending_approval_count = 0
        completed_1h = 0
        failed_1h = 0
        total_1h = 0
        tool_failure_rate_1h = 0.0

    if last_successful_task_at is None:
        last_successful_task_minutes_ago = None
    else:
        last_successful_task_minutes_ago = (
            now - _to_utc(last_successful_task_at)
        ).total_seconds() / 60

    health = HealthData(
        db_connected=db_connected,
        pending_approval_count=pending_approval_count,
        task_failed_1h=failed_1h,
        tool_failure_rate_1h=tool_failure_rate_1h,
        last_successful_task_minutes_ago=last_successful_task_minutes_ago,
    )
    payload: dict[str, object] = {
        "status": "healthy" if db_connected else "unhealthy",
        "db_connected": db_connected,
        "last_successful_task_at": _isoformat_z(last_successful_task_at),
        "pending_approval_count": pending_approval_count,
        "task_counts_1h": {
            "completed": completed_1h,
            "failed": failed_1h,
            "total": total_1h,
        },
        "tool_failure_rate_1h": tool_failure_rate_1h,
        "uptime_seconds": int((now - APP_STARTED_AT).total_seconds()),
    }
    return payload, health


@router.get("/health")
def healthcheck(db: Session = Depends(get_db)) -> dict[str, object]:
    payload, _ = _collect_health(db)
    return payload


def _alert_response(alerts: list[AlertResult]) -> list[dict[str, object]]:
    return [asdict(alert) for alert in alerts]


@router.get("/health/alerts")
def check_alerts(db: Session = Depends(get_db)) -> dict[str, list[dict[str, object]]]:
    """Evaluate alert rules and return results without dispatching webhooks."""
    _, health = _collect_health(db)
    alerts = AlertEngine().evaluate(health)
    return {"alerts": _alert_response(alerts)}


@router.post("/health/alerts/dispatch")
def dispatch_alerts(db: Session = Depends(get_db)) -> dict[str, object]:
    """Evaluate and dispatch fired alerts via webhook."""
    _, health = _collect_health(db)
    alerts = AlertEngine().evaluate(health)
    sent = WebhookDispatcher(get_settings().alert_webhook_url).dispatch(alerts)
    return {"sent": sent, "alerts": _alert_response(alerts)}
