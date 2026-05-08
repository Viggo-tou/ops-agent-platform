"""Stale-task watchdog.

Background task that scans `executing` / `running` tasks every 30 seconds
and marks them STALE_FAILED if their `updated_at` hasn't advanced for
longer than a stage-specific threshold.

Why a watchdog at all
---------------------
Without one, a single hung tool call (LLM read timeout missing, sandbox
subprocess deadlock, etc.) leaves a task `executing` indefinitely. That
task holds workspace state, blocks the user from iterating, and looks
broken in the UI.

Why look at `updated_at` not "no event for N min"
-------------------------------------------------
Empirically codegen.generate_patch can take 3-4 minutes for a single
call and not emit any intermediate events. A naive "5 min no event"
watchdog would kill healthy tasks. updated_at is bumped by both
record_event and set_task_status (since record_event auto-commits, the
underlying ORM session refresh updates the task row's updated_at via
its onupdate=utcnow column default).

In practice we use both: per-stage thresholds, and a global ceiling so
nothing executes longer than 45 minutes regardless of which stage.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.enums import EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.models.task import Task
from app.services import task_cancel
from app.services.events import record_event, set_task_status

logger = logging.getLogger("watchdog")

# Stage-specific 'no progress' thresholds in seconds. Picked from
# observed runtime: codegen single call ~3-4 min, sandbox compile ~1 min,
# review ~30s typical. We allow 3-5x headroom over the 95th percentile.
_STAGE_THRESHOLDS_S: dict[str, int] = {
    "intake":     180,    # 3 min
    "planning":   600,    # 10 min — planner LLM can be slow
    "knowledge":  300,    # 5 min — knowledge synthesis
    "action":     900,    # 15 min — codegen + sandbox
    "review":     600,    # 10 min — diff_reviewer + semantic_review
    "done":        60,    # 1 min — already terminal-adjacent
}
_DEFAULT_THRESHOLD_S = 600

# Hard ceiling: no task should ever be 'executing' for longer than this.
_GLOBAL_CEILING_S = 45 * 60  # 45 min

# How often to scan.
_SCAN_INTERVAL_S = 30

# Statuses considered "in flight" — these are the ones we monitor.
_IN_FLIGHT_STATUSES = (
    TaskStatus.EXECUTING,
    TaskStatus.RUNNING,
    TaskStatus.PLANNING,
    TaskStatus.REVIEWING,
)


def _is_stale(task: Task, now: datetime) -> tuple[bool, str]:
    """Return (is_stale, reason)."""
    updated = task.updated_at
    if updated is None:
        return False, ""
    # Ensure both sides timezone-aware for safe subtraction.
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (now - updated).total_seconds()

    stage_value = (
        task.workflow_stage.value
        if task.workflow_stage and hasattr(task.workflow_stage, "value")
        else str(task.workflow_stage or "")
    )
    threshold = _STAGE_THRESHOLDS_S.get(stage_value, _DEFAULT_THRESHOLD_S)

    if age >= _GLOBAL_CEILING_S:
        return True, f"global_ceiling: no progress for {int(age)}s (limit {_GLOBAL_CEILING_S}s)"
    if age >= threshold:
        return True, f"stage_threshold: stage={stage_value or 'unknown'} no progress for {int(age)}s (limit {threshold}s)"
    return False, ""


def _mark_stale_failed_once() -> int:
    """One scan pass. Returns the count of tasks marked STALE_FAILED."""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    marked = 0
    try:
        stmt = select(Task).where(Task.status.in_(_IN_FLIGHT_STATUSES))
        rows = list(db.scalars(stmt))
        for task in rows:
            is_stale, reason = _is_stale(task, now)
            if not is_stale:
                continue
            try:
                set_task_status(
                    db,
                    task=task,
                    new_status=TaskStatus.STALE_FAILED,
                    new_stage=WorkflowStage.DONE,
                    role=RoleName.SYSTEM,
                    message=f"Watchdog marked task stale ({reason}).",
                    source=EventSource.SYSTEM,
                    payload={"reason": reason, "stage_at_mark": str(task.workflow_stage)},
                )
                record_event(
                    db,
                    task_id=task.id,
                    event_type=EventType.EXECUTION_FAILED,
                    source=EventSource.SYSTEM,
                    stage=WorkflowStage.DONE,
                    role=RoleName.SYSTEM,
                    message="Stale execution timeout — watchdog terminated task.",
                    payload={"reason": reason, "stage_at_mark": str(task.workflow_stage)},
                )
                # Cooperative cancel: flag the task so the worker thread
                # exits at its next record_event checkpoint and frees its
                # executor slot. Without this the underlying thread keeps
                # running its blocking LLM/subprocess call to completion
                # and the slot stays consumed.
                task_cancel.request_cancel(task.id)
                marked += 1
                logger.warning(
                    "watchdog marked task stale",
                    extra={"task_id": task.id, "reason": reason},
                )
            except Exception:  # noqa: BLE001
                logger.exception("watchdog: failed to mark task stale", extra={"task_id": task.id})
                db.rollback()
        return marked
    finally:
        db.close()


async def watchdog_loop() -> None:
    """Long-running coroutine: scan every _SCAN_INTERVAL_S seconds.

    Cancellation-safe — the FastAPI lifespan shutdown will cancel us and
    we exit cleanly via the asyncio.CancelledError that to_thread propagates.
    """
    logger.info("watchdog_started")
    while True:
        try:
            await asyncio.sleep(_SCAN_INTERVAL_S)
            count = await asyncio.to_thread(_mark_stale_failed_once)
            if count:
                logger.info(f"watchdog_scan_marked {count} tasks stale")
        except asyncio.CancelledError:
            logger.info("watchdog_cancelled")
            return
        except Exception:  # noqa: BLE001
            logger.exception("watchdog_scan_error")
            # Sleep a bit and try again — never let a transient error kill
            # the watchdog forever.
            await asyncio.sleep(_SCAN_INTERVAL_S)
