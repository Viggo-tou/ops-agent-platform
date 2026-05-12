"""Regression test: codegen._call_deepseek must hit the OpenAI-compat
URL even when settings.deepseek_base_url is configured for the
Anthropic-compat path (used by the deepseek_agent.py wrapper).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codegen import CodeGenerator  # noqa: E402


def _settings():
    return SimpleNamespace(
        deepseek_api_key="test-key",
        deepseek_base_url="https://api.deepseek.com/anthropic",  # Anthropic-compat — must NOT be used
        deepseek_model="deepseek-coder",
        deepseek_timeout_seconds=60.0,
        primary_agent_model="gpt-4o-mini",
        codegen_provider="deepseek",
        deepseek_max_tokens=4096,
        codegen_diff_max_chars=200_000,
        primary_agent_provider="auto",
        codegen_disabled_providers="",
    )


def _httpx_response_with_diff():
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": '{"diff": "diff --git a/x.py b/x.py\\n--- a/x.py\\n+++ b/x.py\\n@@ -1 +1 @@\\n-old\\n+new\\n", "files_changed": ["x.py"], "summary": "test"}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    resp.raise_for_status.return_value = None
    return resp


def test_deepseek_codegen_uses_openai_compat_url():
    """Even with Anthropic-compat deepseek_base_url, _call_deepseek must POST to /v1/chat/completions."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        return _httpx_response_with_diff()

    settings = _settings()
    gen = CodeGenerator(settings)

    # We only care that the HTTP call hits the OpenAI-compat URL.
    # Whether the diff body parses is orthogonal to the URL check.
    with patch("app.services.codegen.httpx.post", fake_post):
        try:
            gen._call_deepseek("test prompt")
        except Exception:
            pass

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "deepseek-coder"


def test_deepseek_codegen_missing_key_raises():
    from app.services.codegen import CodegenError

    settings = _settings()
    settings.deepseek_api_key = None
    gen = CodeGenerator(settings)

    import pytest as _pytest
    with _pytest.raises(CodegenError, match="DEEPSEEK_API_KEY"):
        gen._call_deepseek("anything")
