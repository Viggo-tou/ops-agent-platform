from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import time
from threading import Lock
from typing import Any, Callable, Protocol


class _Submitter(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future: ...


_executor: ThreadPoolExecutor | None = None
_executor_override: _Submitter | None = None
_max_workers = 0
_active_workers = 0
_queued_jobs = 0
_saturated_since: float | None = None
_lock = Lock()


def init_pipeline_executor(max_workers: int) -> None:
    """Initialise the process-local pipeline executor once."""
    global _executor, _max_workers
    with _lock:
        if _executor is not None:
            return
        _max_workers = max(0, int(max_workers))
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pipeline")


def shutdown_pipeline_executor(wait: bool = True) -> None:
    """Shut down the process-local pipeline executor if it exists."""
    global _executor, _max_workers, _active_workers, _queued_jobs, _saturated_since
    with _lock:
        executor = _executor
        _executor = None
        _max_workers = 0
        _active_workers = 0
        _queued_jobs = 0
        _saturated_since = None
    if executor is not None:
        executor.shutdown(wait=wait)


def _refresh_saturation_locked(now: float | None = None) -> None:
    global _saturated_since
    if _max_workers > 0 and _active_workers >= _max_workers and _queued_jobs > 0:
        if _saturated_since is None:
            _saturated_since = now if now is not None else time.monotonic()
    else:
        _saturated_since = None


def _run_tracked_job(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    global _active_workers, _queued_jobs
    with _lock:
        _queued_jobs = max(0, _queued_jobs - 1)
        _active_workers += 1
        _refresh_saturation_locked()
    try:
        return fn(*args, **kwargs)
    finally:
        with _lock:
            _active_workers = max(0, _active_workers - 1)
            _refresh_saturation_locked()


def submit_pipeline_job(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    global _queued_jobs
    executor = _executor_override
    if executor is not None:
        future = executor.submit(fn, *args, **kwargs)
        try:
            setattr(future, "_pipeline_executor_override", True)
        except Exception:
            pass
        return future

    executor = _executor
    if executor is None:
        raise RuntimeError("Pipeline executor not initialised")
    with _lock:
        _queued_jobs += 1
        _refresh_saturation_locked()
    try:
        return executor.submit(_run_tracked_job, fn, args, kwargs)
    except Exception:
        with _lock:
            _queued_jobs = max(0, _queued_jobs - 1)
            _refresh_saturation_locked()
        raise


def get_pipeline_executor() -> ThreadPoolExecutor | None:
    return _executor


def set_pipeline_executor_override(executor: _Submitter | None) -> None:
    global _executor_override
    _executor_override = executor


def active_workers_count() -> int:
    with _lock:
        return _active_workers


def queue_depth() -> int:
    with _lock:
        return _queued_jobs


def max_workers_count() -> int:
    with _lock:
        return _max_workers


def saturation_age_seconds(now: float | None = None) -> float | None:
    with _lock:
        if _saturated_since is None:
            return None
        current = now if now is not None else time.monotonic()
        return max(0.0, current - _saturated_since)


def pipeline_worker_snapshot() -> dict[str, object]:
    with _lock:
        age = None if _saturated_since is None else max(0.0, time.monotonic() - _saturated_since)
        return {
            "active": _active_workers,
            "max": _max_workers,
            "queue_depth": _queued_jobs,
            "saturation_age_seconds": age,
        }
