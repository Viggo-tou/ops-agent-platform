from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.llm_cache import (  # noqa: E402
    LLMCacheMissError,
    _compute_cache_key,
    cached_http_post,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(self._json_data)

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPS_AGENT_LLM_CACHE_MODE", raising=False)
    monkeypatch.delenv("OPS_AGENT_LLM_CACHE_DIR", raising=False)
    monkeypatch.delenv("OPS_AGENT_LLM_CACHE_REPLAY_ON_MISS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_cache_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str,
    on_miss: str = "passthrough",
) -> None:
    monkeypatch.setenv("OPS_AGENT_LLM_CACHE_MODE", mode)
    monkeypatch.setenv("OPS_AGENT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_AGENT_LLM_CACHE_REPLAY_ON_MISS", on_miss)
    get_settings.cache_clear()


def _cache_file(cache_dir: Path, *, url: str, body: dict[str, Any]) -> Path:
    key = _compute_cache_key(url=url, body=body)
    return cache_dir / key[:2] / f"{key}.json"


def test_off_mode_passes_through_to_httpx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="off")
    calls: list[tuple[str, dict[str, Any]]] = []
    fake_response = _FakeResponse(json_data={"ok": True})

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((url, kwargs))
        return fake_response

    monkeypatch.setattr("app.services.llm_cache.httpx.post", fake_post)

    response = cached_http_post(
        url="https://example.test/chat",
        json={"prompt": "hello"},
        headers={"Authorization": "Bearer secret"},
        timeout=12.0,
    )

    assert response is fake_response
    assert len(calls) == 1
    assert calls[0][0] == "https://example.test/chat"
    assert calls[0][1] == {
        "json": {"prompt": "hello"},
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 12.0,
    }


def test_record_mode_saves_response_to_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="record")
    url = "https://example.test/chat"
    body = {"messages": [{"role": "user", "content": "hi"}]}
    fake_response = _FakeResponse(
        json_data={"choices": [{"message": {"content": "hi"}}]},
        headers={"x-request-id": "req-1"},
    )

    monkeypatch.setattr("app.services.llm_cache.httpx.post", lambda *args, **kwargs: fake_response)

    response = cached_http_post(
        url=url,
        json=body,
        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        timeout=1.0,
        provider_hint="test.provider",
    )

    assert response is fake_response
    path = _cache_file(tmp_path, url=url, body=body)
    assert path.exists()
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["response_json"] == {"choices": [{"message": {"content": "hi"}}]}
    assert entry["request_headers_redacted"]["Authorization"] == "<redacted>"
    assert entry["request_headers_redacted"]["Content-Type"] == "application/json"
    assert entry["provider_hint"] == "test.provider"


def test_record_mode_does_not_save_on_error_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="record")
    url = "https://example.test/chat"
    body = {"prompt": "fail"}
    fake_response = _FakeResponse(status_code=500, json_data={"error": "server"})
    monkeypatch.setattr("app.services.llm_cache.httpx.post", lambda *args, **kwargs: fake_response)

    response = cached_http_post(url=url, json=body, headers={"Authorization": "Bearer secret"})

    assert response is fake_response
    assert not _cache_file(tmp_path, url=url, body=body).exists()


def test_replay_mode_returns_cached_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="replay")
    url = "https://example.test/chat"
    body = {"prompt": "cached"}
    path = _cache_file(tmp_path, url=url, body=body)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "url": url,
                "request_body": body,
                "request_headers_redacted": {},
                "response_status": 200,
                "response_headers": {"content-type": "application/json"},
                "response_json": {"choices": [{"message": {"content": "from-cache"}}]},
                "recorded_at": "2026-05-06T00:00:00+00:00",
                "provider_hint": "test.provider",
            }
        ),
        encoding="utf-8",
    )

    def fail_post(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("httpx.post should not be called on replay hit")

    monkeypatch.setattr("app.services.llm_cache.httpx.post", fail_post)

    response = cached_http_post(url=url, json=body, headers={"Authorization": "Bearer secret"})

    assert response.json() == {"choices": [{"message": {"content": "from-cache"}}]}
    assert response.status_code == 200


def test_replay_miss_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="replay", on_miss="passthrough")
    calls: list[tuple[str, dict[str, Any]]] = []
    fake_response = _FakeResponse(json_data={"choices": [{"message": {"content": "live"}}]})

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((url, kwargs))
        return fake_response

    monkeypatch.setattr("app.services.llm_cache.httpx.post", fake_post)

    response = cached_http_post(url="https://example.test/chat", json={"prompt": "miss"})

    assert response is fake_response
    assert len(calls) == 1


def test_replay_miss_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="replay", on_miss="raise")

    def fail_post(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("httpx.post should not be called when replay miss raises")

    monkeypatch.setattr("app.services.llm_cache.httpx.post", fail_post)

    with pytest.raises(LLMCacheMissError):
        cached_http_post(url="https://example.test/chat", json={"prompt": "miss"})


def test_cache_key_independent_of_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="record")
    url = "https://example.test/chat"
    body = {"prompt": "same"}
    monkeypatch.setattr(
        "app.services.llm_cache.httpx.post",
        lambda *args, **kwargs: _FakeResponse(json_data={"ok": True}),
    )

    cached_http_post(url=url, json=body, headers={"Authorization": "Bearer one"})
    cached_http_post(url=url, json=body, headers={"Authorization": "Bearer two"})

    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_cache_key_changes_with_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_cache_env(monkeypatch, tmp_path, mode="record")
    url = "https://example.test/chat"
    monkeypatch.setattr(
        "app.services.llm_cache.httpx.post",
        lambda *args, **kwargs: _FakeResponse(json_data={"ok": True}),
    )

    cached_http_post(url=url, json={"prompt": "one"})
    cached_http_post(url=url, json={"prompt": "two"})

    assert len(list(tmp_path.rglob("*.json"))) == 2
