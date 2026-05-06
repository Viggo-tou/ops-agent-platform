"""Stage X.5: regression test for the NoneType subscript bug in
ExecutionSandbox.run. When subprocess returns CompletedProcess with
stdout / stderr == None (e.g. UnicodeDecodeError in reader thread),
sandbox.run must NOT raise TypeError.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.sandbox import ExecutionSandbox  # noqa: E402


class _FakePopen:
    def __init__(self, *, returncode: int = 0, stdout=None, stderr=None):
        self.returncode = returncode
        self.pid = 12345
        self.stdout = io.StringIO(stdout) if isinstance(stdout, str) else stdout
        self.stderr = io.StringIO(stderr) if isinstance(stderr, str) else stderr

    def wait(self, timeout=None):  # noqa: ANN001
        return self.returncode

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture()
def sandbox(tmp_path):  # noqa: ANN001
    settings = get_settings()
    original_external_root = settings.sandbox_external_root
    settings.sandbox_external_root = None
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield sb
    finally:
        settings.sandbox_external_root = original_external_root


def test_run_handles_none_stdout_stderr_without_raising(sandbox: ExecutionSandbox):
    """When Popen exposes stdout / stderr as None, sandbox.run
    must return an empty-string equivalent, not raise TypeError.
    """
    with patch("app.services.sandbox.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _FakePopen(returncode=1, stdout=None, stderr=None)
        result = sandbox.run("any-cmd")
    assert result["exit_code"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["timed_out"] is False


def test_run_passes_explicit_utf8_encoding_to_popen(sandbox: ExecutionSandbox):
    """sandbox.run must invoke Popen with encoding='utf-8' so
    Windows doesn't default to GBK and crash on UTF-8 stderr from Gradle.
    """
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakePopen(returncode=0, stdout="ok", stderr="")

    with patch("app.services.sandbox.subprocess.Popen", side_effect=fake_popen):
        sandbox.run("any-cmd")
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("text") is True
    assert captured.get("stdout") is not None
    assert captured.get("stderr") is not None


def test_run_truncates_to_max_output_chars(sandbox: ExecutionSandbox):
    with patch("app.services.sandbox.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _FakePopen(returncode=0, stdout="A" * 200, stderr="B" * 200)
        result = sandbox.run("any-cmd", max_output_bytes=50)
    assert len(result["stdout"]) == 50
    assert len(result["stderr"]) == 50
