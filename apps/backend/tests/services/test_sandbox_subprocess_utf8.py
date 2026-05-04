"""Stage X.5: regression test for the NoneType subscript bug in
ExecutionSandbox.run. When subprocess returns CompletedProcess with
stdout / stderr == None (e.g. UnicodeDecodeError in reader thread),
sandbox.run must NOT raise TypeError.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.sandbox import ExecutionSandbox  # noqa: E402


def _fake_completed(returncode: int = 0, stdout=None, stderr=None):
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_run_handles_none_stdout_stderr_without_raising(tmp_path):
    """When subprocess returns CompletedProcess with stdout / stderr None
    (Windows reader-thread UnicodeDecodeError leaves them None), sandbox.run
    must return an empty-string equivalent, not raise TypeError.
    """
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    with patch("app.services.sandbox.subprocess.run") as mock_run:
        mock_run.return_value = _fake_completed(returncode=1, stdout=None, stderr=None)
        result = sb.run("any-cmd")
    assert result["exit_code"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["timed_out"] is False


def test_run_passes_explicit_utf8_encoding_to_subprocess(tmp_path):
    """sandbox.run must invoke subprocess.run with encoding='utf-8' so
    Windows doesn't default to GBK and crash on UTF-8 stderr from Gradle.
    """
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _fake_completed(returncode=0, stdout="ok", stderr="")

    with patch("app.services.sandbox.subprocess.run", side_effect=fake_run):
        sb.run("any-cmd")
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("text") is True
    assert captured.get("capture_output") is True


def test_run_truncates_to_max_output_chars(tmp_path):
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    with patch("app.services.sandbox.subprocess.run") as mock_run:
        mock_run.return_value = _fake_completed(returncode=0, stdout="A" * 200, stderr="B" * 200)
        result = sb.run("any-cmd", max_output_bytes=50)
    assert len(result["stdout"]) == 50
    assert len(result["stderr"]) == 50
