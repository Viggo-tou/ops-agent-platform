from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable, Protocol


class _Submitter(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future: ...


_executor: ThreadPoolExecutor | None = None
_executor_override: _Submitter | None = None
_lock = Lock()


def init_pipeline_executor(max_workers: int) -> None:
    """Initialise the process-local pipeline executor once."""
    global _executor
    with _lock:
        if _executor is not None:
            return
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pipeline")


def shutdown_pipeline_executor(wait: bool = True) -> None:
    """Shut down the process-local pipeline executor if it exists."""
    global _executor
    with _lock:
        executor = _executor
        _executor = None
    if executor is not None:
        executor.shutdown(wait=wait)


def submit_pipeline_job(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
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
    return executor.submit(fn, *args, **kwargs)


def get_pipeline_executor() -> ThreadPoolExecutor | None:
    return _executor


def set_pipeline_executor_override(executor: _Submitter | None) -> None:
    global _executor_override
    _executor_override = executor
