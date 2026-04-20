"""Unit tests for the intent_resolution service module."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.intent_resolution import (  # noqa: E402
    IntentResolutionTimeoutError,
    MCPNotConfiguredError,
    _build_agent_prompt,
    _parse_agent_output,
    resolve_intent,
)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "mcp_jira_enabled": True,
        "mcp_jira_server_url": "http://127.0.0.1:9100/mcp",
        "claude_code_command": "claude",
        "claude_code_args": "--print",
        "claude_code_git_bash_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeProc:
    pid = 12345

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.wait_called = False
        self.kill_called = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        del timeout
        return self._stdout, self._stderr

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_called = True
        return self.returncode

    def kill(self) -> None:
        self.kill_called = True


class _TimeoutProc(_FakeProc):
    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def test_build_agent_prompt_includes_agent_rules_and_context() -> None:
    prompt = _build_agent_prompt(
        user_input="complete OPS-123",
        pre_fetched_context={"key": "OPS-123", "summary": "Add audit log"},
        translation={"intent": "develop_jira_issue"},
        source_tree_summary="src/\nsrc/audit.py",
        allowed_tools=["mcp__jira__get_issue"],
        max_tool_calls=2,
    )

    assert "You are an intent resolution agent" in prompt
    assert "Maximum 2 tool calls" in prompt
    assert "In English" in prompt
    assert "complete OPS-123" in prompt
    assert "Add audit log" in prompt
    assert "mcp__jira__get_issue" in prompt
    assert "src/audit.py" in prompt
    assert "Return only the refined request text" in prompt


def test_parse_agent_output_json_wrapper_with_tool_calls() -> None:
    stdout = json.dumps(
        {
            "result": "1. Update src/audit.py: add audit logging to create_task.",
            "tool_calls": [
                {
                    "name": "mcp__jira__get_issue",
                    "input": {"key": "OPS-123"},
                }
            ],
        }
    )

    refined, tool_count, sources = _parse_agent_output(
        stdout=stdout,
        user_input="complete OPS-123",
        allowed_tools=["mcp__jira__get_issue"],
    )

    assert refined == "1. Update src/audit.py: add audit logging to create_task."
    assert tool_count == 1
    assert sources == ["jira:OPS-123"]


def test_parse_agent_output_plain_text_counts_tool_mentions() -> None:
    refined, tool_count, sources = _parse_agent_output(
        stdout="1. Update src/audit.py: add audit logging to create_task.",
        stderr="tool_use: mcp__jira__get_issue",
        user_input="complete OPS-123",
        allowed_tools=["mcp__jira__get_issue"],
    )

    assert refined.startswith("1. Update")
    assert tool_count == 1
    assert sources == ["jira:OPS-123"]


def test_resolve_intent_raises_when_mcp_disabled() -> None:
    with patch(
        "app.services.intent_resolution.get_settings",
        return_value=_settings(mcp_jira_enabled=False),
    ):
        with pytest.raises(MCPNotConfiguredError, match="disabled"):
            resolve_intent(user_input="complete OPS-123")


def test_resolve_intent_invokes_claude_with_allowed_tools() -> None:
    stdout = json.dumps(
        {
            "result": "1. Update src/audit.py: add audit logging to create_task.",
            "tool_calls_made": 1,
            "sources_consulted": ["jira:OPS-123"],
        }
    )
    fake_proc = _FakeProc(stdout=stdout)

    with patch("app.services.intent_resolution.get_settings", return_value=_settings()):
        with patch("app.services.intent_resolution.shutil.which", return_value="claude"):
            with patch("app.services.intent_resolution.subprocess.Popen", return_value=fake_proc) as popen:
                result = resolve_intent(
                    user_input="complete OPS-123",
                    allowed_tools=["mcp__jira__get_issue"],
                    max_tool_calls=3,
                    timeout_seconds=10,
                )

    cmd = popen.call_args.args[0]
    assert "--allowedTools" in cmd
    assert "mcp__jira__get_issue" in cmd
    assert "-p" in cmd or "--print" in cmd
    assert result.refined_text.startswith("1. Update src/audit.py")
    assert result.tool_calls_made == 1
    assert result.sources_consulted == ["jira:OPS-123"]


def test_resolve_intent_timeout_terminates_process() -> None:
    fake_proc = _TimeoutProc()

    with patch("app.services.intent_resolution.get_settings", return_value=_settings()):
        with patch("app.services.intent_resolution.shutil.which", return_value="claude"):
            with patch("app.services.intent_resolution.subprocess.Popen", return_value=fake_proc):
                with patch("app.services.intent_resolution.subprocess.run", return_value=Mock()):
                    with pytest.raises(IntentResolutionTimeoutError, match="timed out"):
                        resolve_intent(
                            user_input="complete OPS-123",
                            allowed_tools=["mcp__jira__get_issue"],
                            timeout_seconds=0.01,
                        )
