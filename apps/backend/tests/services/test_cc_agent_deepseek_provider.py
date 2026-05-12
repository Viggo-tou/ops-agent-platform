"""Unit tests for the deepseek branch in cc_agent_loop._call_decision_provider."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.cc_agent_loop import _call_decision_provider, CCDecisionError  # noqa: E402


def _settings(api_key: str | None = "test-deepseek-key", model: str = "deepseek-chat"):
    return SimpleNamespace(
        deepseek_api_key=api_key,
        deepseek_model=model,
        deepseek_base_url="https://api.deepseek.com/anthropic",  # Anthropic-compat — should NOT be used
        # other fields cc_agent might reference
        minimax_api_key=None,
        minimax_base_url="",
        semantic_translator_model="MiniMax-M2.7",
        claude_code_command="claude",
        claude_code_args="",
        codex_command="codex",
    )


def _httpx_response(content: str = '{"done": true}'):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


def test_deepseek_provider_calls_openai_compat_url(monkeypatch):
    """Even if deepseek_base_url is the Anthropic-compat path, the cc_agent
    branch must hit the OpenAI-compat /v1/chat/completions endpoint."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json", {})
        captured["headers"] = kwargs.get("headers", {})
        return _httpx_response('{"done": true}')

    monkeypatch.setattr("app.services.cc_agent_loop.httpx.post", fake_post)
    with patch("app.services.cc_agent_loop.get_settings", return_value=_settings()):
        out = _call_decision_provider("deepseek", prompt="pick file", cwd=Path("."), timeout_s=10.0)

    assert out == '{"done": true}'
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-deepseek-key"
    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][1]["content"] == "pick file"


def test_deepseek_provider_missing_api_key_raises(monkeypatch):
    with patch("app.services.cc_agent_loop.get_settings", return_value=_settings(api_key=None)):
        with pytest.raises(CCDecisionError, match="DeepSeek API key"):
            _call_decision_provider("deepseek", prompt="x", cwd=Path("."), timeout_s=5.0)


def test_deepseek_provider_uses_configured_model(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["model"] = kwargs.get("json", {}).get("model")
        return _httpx_response()

    monkeypatch.setattr("app.services.cc_agent_loop.httpx.post", fake_post)
    with patch("app.services.cc_agent_loop.get_settings", return_value=_settings(model="deepseek-v4-pro")):
        _call_decision_provider("deepseek", prompt="x", cwd=Path("."), timeout_s=5.0)

    assert captured["model"] == "deepseek-v4-pro"


def test_unsupported_provider_still_raises():
    with patch("app.services.cc_agent_loop.get_settings", return_value=_settings()):
        with pytest.raises(CCDecisionError, match="unsupported provider"):
            _call_decision_provider("gpt-5", prompt="x", cwd=Path("."), timeout_s=5.0)
