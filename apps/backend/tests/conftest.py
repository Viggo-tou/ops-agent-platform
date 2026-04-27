from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import Future
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

_ORIGINAL_MKDIR = None


def pytest_configure(config):  # noqa: ANN001
    global _ORIGINAL_MKDIR
    if os.name != "nt" or _ORIGINAL_MKDIR is not None:
        return

    _ORIGINAL_MKDIR = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777, *, dir_fd=None) -> None:  # noqa: ANN001
        if dir_fd is None:
            _ORIGINAL_MKDIR(path, 0o777)
            return
        _ORIGINAL_MKDIR(path, 0o777, dir_fd=dir_fd)

    tempfile._os.mkdir = mkdir_with_write_access


def pytest_unconfigure(config):  # noqa: ANN001
    global _ORIGINAL_MKDIR
    if _ORIGINAL_MKDIR is None:
        return
    tempfile._os.mkdir = _ORIGINAL_MKDIR
    _ORIGINAL_MKDIR = None


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
