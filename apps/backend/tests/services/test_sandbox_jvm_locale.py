"""Stage X.7.a: ExecutionSandbox.run() must inject JAVA_TOOL_OPTIONS so
JVM child processes (gradle, kotlinc, javac) emit English error messages
regardless of system locale. Without this, Gradle on a zh-CN Windows host
emits GBK Chinese which compile_gate's repair codegen can't parse.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.sandbox import ExecutionSandbox  # noqa: E402


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_run_injects_jvm_locale_en_us(tmp_path):
    """sandbox.run must include JAVA_TOOL_OPTIONS forcing en-US locale + UTF-8."""
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _fake_completed()

    with patch("app.services.sandbox.subprocess.run", side_effect=fake_run):
        sb.run("any-cmd")

    env = captured.get("env") or {}
    jto = env.get("JAVA_TOOL_OPTIONS", "")
    assert "-Duser.language=en" in jto
    assert "-Duser.country=US" in jto
    assert "-Dfile.encoding=UTF-8" in jto


def test_run_caller_env_jto_merges_with_locale_flags(tmp_path):
    """Caller's JAVA_TOOL_OPTIONS must NOT override the locale flags;
    they should be merged so JVM still gets en-US."""
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _fake_completed()

    with patch("app.services.sandbox.subprocess.run", side_effect=fake_run):
        sb.run("any-cmd", env={"JAVA_TOOL_OPTIONS": "-Xmx2g"})

    env = captured.get("env") or {}
    jto = env.get("JAVA_TOOL_OPTIONS", "")
    assert "-Duser.language=en" in jto
    assert "-Xmx2g" in jto


def test_run_caller_env_other_keys_still_propagate(tmp_path):
    """Caller-supplied non-JTO env keys (e.g. GRADLE_OPTS) must still flow through."""
    sb = ExecutionSandbox(task_id="t1", base_dir=str(tmp_path))
    sb.sandbox_dir.mkdir(parents=True, exist_ok=True)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _fake_completed()

    with patch("app.services.sandbox.subprocess.run", side_effect=fake_run):
        sb.run("any-cmd", env={"GRADLE_OPTS": "-Dorg.gradle.daemon=false"})

    env = captured.get("env") or {}
    assert env.get("GRADLE_OPTS") == "-Dorg.gradle.daemon=false"
    # And locale flags still present
    jto = env.get("JAVA_TOOL_OPTIONS", "")
    assert "-Duser.language=en" in jto
