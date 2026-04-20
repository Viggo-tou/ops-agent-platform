from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait=True):
        pass


@pytest.fixture(autouse=True)
def _sync_pipeline_executor():
    from app.core.pipeline_executor import set_pipeline_executor_override

    set_pipeline_executor_override(_ImmediateExecutor())
    yield
    set_pipeline_executor_override(None)
