from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.services import cc_agent

REPO_ROOT = Path(__file__).resolve().parents[4]


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_cc_glob_parses_claude_output_to_filematches(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")
    monkeypatch.setattr(cc_agent.subprocess, "run", lambda *args, **kwargs: _completed("src/App.js\nsrc/Login.js\n"))

    result = cc_agent.cc_glob("*.js", cwd=REPO_ROOT)

    assert result.error is None
    assert [match.path for match in result.matches] == ["src/App.js", "src/Login.js"]


def test_cc_grep_parses_with_line_numbers(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")
    monkeypatch.setattr(
        cc_agent.subprocess,
        "run",
        lambda *args, **kwargs: _completed("src/Login.js:42:function handleLogin() {}\n"),
    )

    result = cc_agent.cc_grep("handleLogin", cwd=REPO_ROOT, file_glob="*.js")

    assert result.error is None
    assert result.matches == [cc_agent.CCFileMatch(path="src/Login.js", line=42)]


def test_cc_read_returns_full_text_when_no_range(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")
    monkeypatch.setattr(cc_agent.subprocess, "run", lambda *args, **kwargs: _completed("line 1\nline 2\n"))

    result = cc_agent.cc_read("src/Login.js", cwd=REPO_ROOT)

    assert result.error is None
    assert result.raw_text == "line 1\nline 2\n"
    assert result.matches[0].path == "src/Login.js"


def test_cc_read_with_line_range(monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")

    def fake_run(*args, **kwargs):
        captured["input"] = kwargs["input"]
        return _completed("function body\n")

    monkeypatch.setattr(cc_agent.subprocess, "run", fake_run)

    result = cc_agent.cc_read("src/Login.js", cwd=REPO_ROOT, line_range=(35, 82))

    assert result.error is None
    assert "35-82" in captured["input"]
    assert result.raw_text == "function body\n"


def test_cc_glob_timeout_returns_error_result(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1)

    monkeypatch.setattr(cc_agent.subprocess, "run", fake_run)

    result = cc_agent.cc_glob("*.js", cwd=REPO_ROOT, timeout_s=1)

    assert "timeout" in (result.error or "")
    assert result.matches == []


def test_cc_grep_nonzero_exit_returns_error(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")
    monkeypatch.setattr(cc_agent.subprocess, "run", lambda *args, **kwargs: _completed("", "boom", 2))

    result = cc_agent.cc_grep("x", cwd=REPO_ROOT)

    assert "rc=2" in (result.error or "")
    assert "boom" in (result.error or "")


def test_cc_tool_default_excludes_filter_resources(monkeypatch) -> None:
    captured: dict[str, str] = {}
    settings = SimpleNamespace(
        claude_code_command="npx",
        claude_code_args="--yes @anthropic-ai/claude-code",
        cc_grep_default_excludes=["*.css", "*.svg", "node_modules/**"],
    )
    monkeypatch.setattr(cc_agent, "get_settings", lambda: settings)
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _cmd: "claude")

    def fake_run(*args, **kwargs):
        captured["input"] = kwargs["input"]
        return _completed("")

    monkeypatch.setattr(cc_agent.subprocess, "run", fake_run)

    cc_agent.cc_grep("ExportReportButton", cwd=REPO_ROOT)

    assert "*.css" in captured["input"]
    assert "*.svg" in captured["input"]
    assert "node_modules/**" in captured["input"]
