from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.sandbox import ExecutionSandbox  # noqa: E402


def _python_command(code: str) -> str:
    parts = [sys.executable, "-c", code]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


@pytest.fixture()
def sandbox(tmp_path):
    settings = get_settings()
    original_external_root = settings.sandbox_external_root
    settings.sandbox_external_root = None
    sb = ExecutionSandbox(task_id="tree-kill-task", base_dir=str(tmp_path))
    sb.work_dir.mkdir(parents=True, exist_ok=False)
    try:
        yield sb
    finally:
        settings.sandbox_external_root = original_external_root


def test_run_completes_normally(sandbox: ExecutionSandbox) -> None:
    result = sandbox.run(_python_command("print('hello')"), timeout_seconds=5)

    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["timed_out"] is False


def test_run_kills_on_timeout(sandbox: ExecutionSandbox) -> None:
    result = sandbox.run(
        _python_command("import time; time.sleep(5)"),
        timeout_seconds=0.5,
    )

    assert result["exit_code"] == -1
    assert result["timed_out"] is True
    assert "timed out" in str(result["stderr"]).lower()


def test_run_captures_stderr_on_failure(sandbox: ExecutionSandbox) -> None:
    result = sandbox.run(
        _python_command("import sys; sys.stderr.write('failure on stderr\\n'); sys.exit(7)"),
        timeout_seconds=5,
    )

    assert result["exit_code"] == 7
    assert "failure on stderr" in result["stderr"]
    assert result["timed_out"] is False
