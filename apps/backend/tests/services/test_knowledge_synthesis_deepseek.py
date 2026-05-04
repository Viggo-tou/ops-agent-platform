"""Stage X.7.b: knowledge synthesis can be configured to use DeepSeek
instead of MiniMax. DeepSeek-Chat is ~10x faster than MiniMax-M2.7 for
the synthesis workload (3-8s vs 60-90s).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.knowledge_synthesis import (  # noqa: E402
    KnowledgeSynthesizer,
    KnowledgeSynthesisError,
)


def _settings(provider: str = "deepseek", deepseek_key: str | None = "ds-test-key"):
    return SimpleNamespace(
        knowledge_synthesis_enabled=True,
        knowledge_synthesis_provider=provider,
        knowledge_synthesis_model="MiniMax-M2.7",
        knowledge_synthesis_deepseek_model="deepseek-chat",
        knowledge_synthesis_timeout_seconds=45.0,
        knowledge_synthesis_max_snippet_chars=6000,
        deepseek_api_key=deepseek_key,
        deepseek_base_url="https://api.deepseek.com/anthropic",  # Anthropic-compat — must NOT be used
        minimax_api_key="mm-key",
        minimax_base_url="https://api.minimaxi.com",
    )


def _mock_response(content: str = "Test answer."):
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    resp.raise_for_status.return_value = None
    return resp


def test_deepseek_synth_uses_openai_compat_url():
    """Even with Anthropic-compat deepseek_base_url, synthesis must POST to /v1/chat/completions."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json", {})
        captured["headers"] = kwargs.get("headers", {})
        return _mock_response("synthesized answer")

    settings = _settings()
    synth = KnowledgeSynthesizer(db=MagicMock(), settings=settings)

    with patch("app.services.knowledge_synthesis.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.side_effect = fake_post
        text, in_tok, out_tok = synth._call_deepseek(
            system_prompt="sys", user_prompt="usr"
        )

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer ds-test-key"
    assert captured["json"]["model"] == "deepseek-chat"
    assert text == "synthesized answer"
    assert in_tok == 10 and out_tok == 20


def test_deepseek_synth_missing_key_raises():
    settings = _settings(deepseek_key=None)
    synth = KnowledgeSynthesizer(db=MagicMock(), settings=settings)
    with pytest.raises(KnowledgeSynthesisError, match="DEEPSEEK_API_KEY"):
        synth._call_deepseek(system_prompt="s", user_prompt="u")


def test_minimax_path_unchanged_when_provider_is_minimax():
    """Provider=minimax must call the original _call_minimax path."""
    settings = _settings(provider="minimax")
    synth = KnowledgeSynthesizer(db=MagicMock(), settings=settings)

    with patch.object(synth, "_call_minimax", return_value=("mm answer", 1, 2)) as mm:
        with patch.object(synth, "_call_deepseek") as ds:
            text, _, _ = synth._call_minimax(system_prompt="s", user_prompt="u")
    assert text == "mm answer"
