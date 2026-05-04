from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.core.enums import TaskStatus
from app.models.task import Task

CheckpointStage = Literal[
    "intake",
    "translate",
    "retrieve",
    "plan",
    "review_pre",
    "codegen",
    "compile",
    "review_post",
    "awaiting_approval",
]
ResumeMethod = Literal["replay_from_output", "redo_stage", "abort_to_user"]

CHECKPOINT_SCHEMA_VERSION = "stage26.task_checkpoint.v1"
CHECKPOINT_STAGES: set[str] = {
    "intake",
    "translate",
    "retrieve",
    "plan",
    "review_pre",
    "codegen",
    "compile",
    "review_post",
    "awaiting_approval",
}
ACTIVE_RESUME_STATUSES = {
    TaskStatus.CREATED,
    TaskStatus.PLANNING,
    TaskStatus.REVIEWING,
    TaskStatus.EXECUTING,
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
}


@dataclass(frozen=True)
class TaskCheckpoint:
    stage: CheckpointStage
    completed_at: datetime
    output_payload: dict[str, Any]
    sandbox_snapshot_id: str | None
    can_resume: bool
    resume_method: ResumeMethod
    schema_version: str = CHECKPOINT_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["completed_at"] = self.completed_at.astimezone(timezone.utc).isoformat()
        return _json_safe(payload)


def write_task_checkpoint(
    db: Session,
    *,
    task: Task,
    stage: CheckpointStage,
    output_payload: dict[str, Any] | None = None,
    sandbox_snapshot_id: str | None = None,
    can_resume: bool = True,
    resume_method: ResumeMethod = "replay_from_output",
) -> TaskCheckpoint:
    if stage not in CHECKPOINT_STAGES:
        raise ValueError(f"Unknown checkpoint stage: {stage}")
    checkpoint = TaskCheckpoint(
        stage=stage,
        completed_at=datetime.now(timezone.utc),
        output_payload=_json_safe(output_payload or {}),
        sandbox_snapshot_id=sandbox_snapshot_id,
        can_resume=can_resume,
        resume_method=resume_method,
    )
    task.latest_checkpoint_json = checkpoint.to_json()
    db.flush()
    return checkpoint


def read_task_checkpoint(task: Task) -> TaskCheckpoint | None:
    raw = getattr(task, "latest_checkpoint_json", None)
    if not isinstance(raw, dict):
        return None
    stage = raw.get("stage")
    if not isinstance(stage, str) or stage not in CHECKPOINT_STAGES:
        return None
    completed_at = _parse_datetime(raw.get("completed_at"))
    output_payload = raw.get("output_payload")
    if not isinstance(output_payload, dict):
        output_payload = {}
    sandbox_snapshot_id = raw.get("sandbox_snapshot_id")
    if sandbox_snapshot_id is not None:
        sandbox_snapshot_id = str(sandbox_snapshot_id)
    resume_method = raw.get("resume_method")
    if resume_method not in {"replay_from_output", "redo_stage", "abort_to_user"}:
        resume_method = "abort_to_user"
    return TaskCheckpoint(
        stage=stage,  # type: ignore[arg-type]
        completed_at=completed_at,
        output_payload=output_payload,
        sandbox_snapshot_id=sandbox_snapshot_id,
        can_resume=bool(raw.get("can_resume")),
        resume_method=resume_method,  # type: ignore[arg-type]
        schema_version=str(raw.get("schema_version") or CHECKPOINT_SCHEMA_VERSION),
    )


def is_checkpoint_fresh(
    checkpoint: TaskCheckpoint,
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> bool:
    current = _normalize_datetime(now or datetime.now(timezone.utc))
    completed = _normalize_datetime(checkpoint.completed_at)
    return current - completed <= timedelta(hours=max(0, int(max_age_hours)))


def is_task_resumable(
    task: Task,
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> bool:
    if getattr(task, "pending_approval", False):
        return False
    if task.status not in ACTIVE_RESUME_STATUSES:
        return False
    checkpoint = read_task_checkpoint(task)
    if checkpoint is None or not checkpoint.can_resume:
        return False
    if checkpoint.resume_method == "abort_to_user":
        return False
    return is_checkpoint_fresh(checkpoint, max_age_hours=max_age_hours, now=now)


def find_resumable_tasks(
    db: Session,
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> list[Task]:
    candidates = db.query(Task).filter(
        Task.status.in_(list(ACTIVE_RESUME_STATUSES)),
        Task.pending_approval.is_(False),
        Task.latest_checkpoint_json.is_not(None),
    ).all()
    return [
        task
        for task in candidates
        if is_task_resumable(task, max_age_hours=max_age_hours, now=now)
    ]


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, str):
        try:
            return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=True))
