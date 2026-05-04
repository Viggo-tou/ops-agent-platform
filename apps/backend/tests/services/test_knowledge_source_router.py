"""Unit tests for knowledge_source_router LLM picker."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.knowledge_source_router import select_sources, _parse_selected  # noqa: E402


def _settings(api_key: str | None = "test-key", enabled: bool = True):
    return SimpleNamespace(
        minimax_api_key=api_key,
        minimax_base_url="https://api.test/v1",
        knowledge_source_router_enabled=enabled,
        knowledge_source_router_timeout_seconds=5.0,
        semantic_translator_model="MiniMax-Text-01",
    )


def _httpx_response(content: str):
    return SimpleNamespace(
        json=lambda: {"choices": [{"message": {"content": content}}]},
        raise_for_status=lambda: None,
    )


def test_disabled_flag_returns_empty():
    out = select_sources(
        query="anything",
        source_descriptions={"a": "desc", "b": "desc"},
        settings=_settings(enabled=False),
    )
    assert out == []


def test_missing_api_key_returns_empty():
    out = select_sources(
        query="anything",
        source_descriptions={"a": "desc", "b": "desc"},
        settings=_settings(api_key=None),
    )
    assert out == []


def test_empty_query_returns_empty():
    out = select_sources(
        query="   ",
        source_descriptions={"a": "desc", "b": "desc"},
        settings=_settings(),
    )
    assert out == []


def test_single_source_short_circuits_to_that_source():
    out = select_sources(
        query="anything",
        source_descriptions={"only_one": "desc"},
        settings=_settings(),
    )
    assert out == ["only_one"]


def test_llm_picks_app_for_signup_query():
    response = _httpx_response('{"selected": ["handymanapp"], "reason": "signup is app"}')
    with patch("app.services.knowledge_source_router.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = response
        out = select_sources(
            query="Add map-based address selection to account signup flow",
            source_descriptions={
                "handymanapp": "Customer-facing mobile app: signup, login, address selection, job booking",
                "hosteddashboard": "Admin web dashboard: user management, support feedback, analytics",
            },
            settings=_settings(),
        )
    assert out == ["handymanapp"]


def test_llm_returns_multiple_sources_when_relevant():
    response = _httpx_response('{"selected": ["a", "b"], "reason": "both"}')
    with patch("app.services.knowledge_source_router.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = response
        out = select_sources(
            query="cross-cutting refactor",
            source_descriptions={"a": "x", "b": "y"},
            settings=_settings(),
        )
    assert out == ["a", "b"]


def test_invalid_source_names_filtered_out():
    response = _httpx_response('{"selected": ["a", "unknown_source", "b"]}')
    with patch("app.services.knowledge_source_router.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = response
        out = select_sources(
            query="q",
            source_descriptions={"a": "x", "b": "y"},
            settings=_settings(),
        )
    assert out == ["a", "b"]


def test_http_failure_returns_empty():
    with patch("app.services.knowledge_source_router.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.side_effect = RuntimeError("boom")
        out = select_sources(
            query="q",
            source_descriptions={"a": "x", "b": "y"},
            settings=_settings(),
        )
    assert out == []


def test_unparseable_json_returns_empty():
    response = _httpx_response("not json at all { broken")
    with patch("app.services.knowledge_source_router.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = response
        out = select_sources(
            query="q",
            source_descriptions={"a": "x", "b": "y"},
            settings=_settings(),
        )
    assert out == []


def test_parse_selected_with_code_fence():
    out = _parse_selected('```json\n{"selected": ["x"]}\n```')
    assert out == ["x"]
