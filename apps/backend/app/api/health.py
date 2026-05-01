from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.pipeline_executor import pipeline_worker_snapshot
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import ApprovalStatus, EventType, TaskStatus, ToolExecutionStatus
from app.models.approval import Approval
from app.models.event import Event
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


def _active_task_last_event_age_seconds(db: Session, now: datetime) -> float | None:
    terminal_statuses = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.ROLLED_BACK,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.WAITING_APPROVAL,
    }
    rows = db.execute(
        select(Task.id, Task.updated_at, func.max(Event.created_at))
        .select_from(Task)
        .outerjoin(Event, Event.task_id == Task.id)
        .where(Task.status.notin_(terminal_statuses))
        .group_by(Task.id, Task.updated_at)
    ).all()
    oldest_age: float | None = None
    for _task_id, updated_at, last_event_at in rows:
        reference = last_event_at or updated_at
        if reference is None:
            continue
        age = (now - _to_utc(reference)).total_seconds()
        oldest_age = age if oldest_age is None else max(oldest_age, age)
    return None if oldest_age is None else max(0.0, oldest_age)


def _provider_bucket(provider: object, *, tool_name: str | None = None) -> str | None:
    if tool_name and tool_name.startswith("jira."):
        return "jira"
    normalized = str(provider or "").strip().lower()
    if not normalized:
        return None
    if "jira" in normalized:
        return "jira"
    if "minimax" in normalized or normalized in {"mm", "mini_max"}:
        return "minimax"
    if "codex" in normalized:
        return "codex_cli"
    if "anthropic" in normalized:
        return "anthropic"
    return None


def _external_api_recent_failures(db: Session, now: datetime) -> dict[str, int]:
    failures = {
        "minimax": 0,
        "anthropic": 0,
        "jira": 0,
        "codex_cli": 0,
    }
    since = now - timedelta(minutes=5)
    failure_event_types = {
        EventType.LLM_CALL,
        EventType.JIRA_FETCH_FAILED,
        EventType.MM_TRANSLATION_FAILED,
        EventType.SYNTHESIS_CALL_FAILED,
        EventType.TOOL_FAILED,
        EventType.TOOL_TIMED_OUT,
    }
    events = db.scalars(
        select(Event)
        .where(Event.created_at >= since, Event.event_type.in_(failure_event_types))
        .order_by(Event.created_at.desc())
    )
    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        if event.event_type == EventType.LLM_CALL and payload.get("success") is not False:
            continue
        provider = payload.get("provider_name") or payload.get("provider")
        bucket = _provider_bucket(provider, tool_name=event.tool_name)
        if bucket in failures:
            failures[bucket] += 1
    return failures


def _collect_health(db: Session) -> tuple[dict[str, object], HealthData]:
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    pipeline_workers = pipeline_worker_snapshot()

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
        last_event_age_seconds = _active_task_last_event_age_seconds(db, now)
        external_api_failures = _external_api_recent_failures(db, now)
    except Exception:
        db_connected = False
        last_successful_task_at = None
        pending_approval_count = 0
        completed_1h = 0
        failed_1h = 0
        total_1h = 0
        tool_failure_rate_1h = 0.0
        last_event_age_seconds = None
        external_api_failures = {
            "minimax": 0,
            "anthropic": 0,
            "jira": 0,
            "codex_cli": 0,
        }

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
    worker_saturation_age = pipeline_workers.get("saturation_age_seconds")
    workers_jammed = (
        isinstance(worker_saturation_age, (int, float))
        and int(pipeline_workers.get("max") or 0) > 0
        and int(pipeline_workers.get("active") or 0) >= int(pipeline_workers.get("max") or 0)
        and int(pipeline_workers.get("queue_depth") or 0) > 0
        and worker_saturation_age > 120
    )
    stale_task = last_event_age_seconds is not None and last_event_age_seconds > 300
    external_api_degraded = any(count > 5 for count in external_api_failures.values())
    status = (
        "healthy"
        if db_connected and not workers_jammed and not stale_task and not external_api_degraded
        else "degraded"
    )

    payload: dict[str, object] = {
        "status": status,
        "db_connected": db_connected,
        "last_successful_task_at": _isoformat_z(last_successful_task_at),
        "pending_approval_count": pending_approval_count,
        "task_counts_1h": {
            "completed": completed_1h,
            "failed": failed_1h,
            "total": total_1h,
        },
        "tool_failure_rate_1h": tool_failure_rate_1h,
        "pipeline_workers": {
            "active": pipeline_workers["active"],
            "max": pipeline_workers["max"],
            "queue_depth": pipeline_workers["queue_depth"],
            "last_event_age_seconds": last_event_age_seconds,
            "saturation_age_seconds": pipeline_workers["saturation_age_seconds"],
        },
        "external_api_recent_failures_5min": external_api_failures,
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
