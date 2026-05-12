from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.services.query_rewrite import expand_query_tokens  # noqa: E402


def _settings(**overrides: object) -> Settings:
    values = {
        "minimax_api_key": "test-key",
        "knowledge_query_rewrite_enabled": True,
        "knowledge_query_rewrite_timeout_seconds": 3.0,
        "knowledge_source_path": None,
    }
    values.update(overrides)
    return Settings(**values)


def _client_with_payload(payload: dict) -> tuple[Mock, Mock]:
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    return client, Mock(return_value=client)


def test_expand_query_tokens_is_additive_and_excludes_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, client_factory = _client_with_payload(
        {"choices": [{"message": {"content": '{"tokens": ["SupportFeedback", "emailjs"]}'}}]}
    )
    monkeypatch.setattr("app.services.query_rewrite.httpx.Client", client_factory)

    result = expand_query_tokens(
        query="support ticket reply pipeline",
        settings=_settings(),
        existing_tokens={"support", "ticket", "reply", "pipeline"},
    )

    assert {"supportfeedback", "feedback", "emailjs"}.issubset(result)
    assert result.isdisjoint({"support", "ticket", "reply", "pipeline"})
    assert "support" not in result
    client_factory.assert_called_once_with(timeout=3.0)
    assert client.post.call_count == 1


def test_expand_query_tokens_disabled_returns_empty_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.query_rewrite.httpx.Client",
        Mock(side_effect=AssertionError("HTTP should not be called")),
    )

    result = expand_query_tokens(
        query="support ticket reply pipeline",
        settings=_settings(knowledge_query_rewrite_enabled=False),
        existing_tokens={"support", "ticket", "reply", "pipeline"},
    )

    assert result == set()


def test_expand_query_tokens_malformed_json_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, client_factory = _client_with_payload(
        {"choices": [{"message": {"content": "not json"}}]}
    )
    monkeypatch.setattr("app.services.query_rewrite.httpx.Client", client_factory)

    result = expand_query_tokens(
        query="support ticket reply pipeline",
        settings=_settings(),
        existing_tokens={"support", "ticket", "reply", "pipeline"},
    )

    assert result == set()
