"""Cooperative task cancellation.

Background: Python threads can't be safely killed from outside, but a
worker that polls a flag at well-defined checkpoints can exit on its
own. The watchdog uses this to free pipeline_executor slots when it
marks a task STALE_FAILED — without it, the underlying thread keeps
running its blocking LLM / subprocess call until completion (or
forever) and the worker slot stays consumed.

Flow:
    1. Watchdog detects task stale → sets DB status=STALE_FAILED.
    2. Watchdog calls request_cancel(task_id).
    3. Next time the worker reaches record_event() (the hottest
       checkpoint — runs hundreds of times per pipeline), it
       raises TaskCancelledError.
    4. run_pipeline_job's outer try/except catches the exception,
       commits any partial state, returns.
    5. Worker slot freed; new tasks no longer wait behind a zombie.

Time-to-cancel is bounded by the next record_event() call, which
typically fires every few seconds in healthy pipelines. The window
is much shorter than a hung 30-min repair loop.

If a worker is stuck inside a blocking call that emits no events
(e.g. an httpx.post that the upstream provider never closes), this
won't help — but those cases are rare and need provider-level
timeout fixes anyway.
"""
from __future__ import annotations

from threading import Lock


class TaskCancelledError(RuntimeError):
    """Raised at a record_event checkpoint when the task has been cancelled.

    Carries the task_id so the run_pipeline_job catch can log the right
    task even though SQLAlchemy session may have been rolled back.
    """

    def __init__(self, task_id: str, reason: str = "cancelled") -> None:
        super().__init__(f"task {task_id} cancelled: {reason}")
        self.task_id = task_id
        self.reason = reason


_cancelled_task_ids: set[str] = set()
_lock = Lock()


def request_cancel(task_id: str) -> None:
    """Mark a task as needing cancellation. Idempotent."""
    if not task_id:
        return
    with _lock:
        _cancelled_task_ids.add(task_id)


def is_cancelled(task_id: str) -> bool:
    """Return True if request_cancel was called for this task."""
    if not task_id:
        return False
    with _lock:
        return task_id in _cancelled_task_ids


def clear_cancel(task_id: str) -> None:
    """Remove a task from the cancel set (e.g. after successful unwind)."""
    if not task_id:
        return
    with _lock:
        _cancelled_task_ids.discard(task_id)


def check_or_raise(task_id: str, reason: str = "cancelled by watchdog") -> None:
    """Raise TaskCancelledError if cancellation was requested for this task.

    Called at hot checkpoints (record_event, etc.) so the worker can
    exit cleanly without changing every individual tool/LLM call.
    """
    if is_cancelled(task_id):
        raise TaskCancelledError(task_id, reason)


def cancelled_set_size() -> int:
    """Diagnostic: how many tasks are currently flagged for cancel."""
    with _lock:
        return len(_cancelled_task_ids)
