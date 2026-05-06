"""LLM HTTP cache (Tool 2) - record/replay wrapper for fast dev iteration.

Wraps httpx.post calls to LLM providers so that real HTTP calls can be
recorded once and replayed many times on subsequent harness iterations.
Drives a record/replay loop similar to vcrpy / unittest.mock.responses
but project-scoped and zero-dependency.

Usage at call sites:

    from app.services.llm_cache import cached_http_post

    response = cached_http_post(
        url=url,
        json=body,
        headers=headers,
        timeout=timeout,
        provider_hint="deepseek",  # purely for filename/debug; not part of cache key
    )

Cache key = SHA256( url + json.dumps(body, sort_keys=True) ).
Cache file: <llm_cache_dir>/<key[:2]>/<key>.json containing:
  {
    "url": str,
    "request_body": dict,
    "request_headers_redacted": dict,  # auth values replaced with "<redacted>"
    "response_status": int,
    "response_headers": dict,
    "response_json": dict,
    "recorded_at": str (ISO),
    "provider_hint": str,
  }

Modes (driven by Settings.llm_cache_mode):
- "off": pass through to httpx.post unchanged.
- "record": call httpx.post, persist response, return real response.
  If a cache file already exists for this key, OVERWRITE it (last call wins).
- "replay": compute key, look up file:
    - Hit: synthesize a response object (with .json() and .raise_for_status())
      from the saved data; do NOT make any HTTP call.
    - Miss: behavior follows Settings.llm_cache_replay_on_miss:
        - "passthrough" (default): fall through to real httpx.post (no record).
        - "raise": raise LLMCacheMissError so dev sees what's missing.
- "auto": hybrid — cache hit returns cached without HTTP; cache miss
  calls live AND records. First time a prompt is seen costs full LLM
  time; every identical retry is instant. Recommended for production.

Scope: ONLY the call sites that pass through cached_http_post. Other
httpx usage (Jira API, knowledge HTTP, etc.) is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings


class LLMCacheMissError(RuntimeError):
    """Raised in replay mode when no cached response exists for a request
    and replay_on_miss is set to "raise"."""


_REDACTED = "<redacted>"
_AUTH_HEADER_KEYS = {"authorization", "x-api-key", "x-goog-api-key", "anthropic-version"}


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _AUTH_HEADER_KEYS:
            out[k] = _REDACTED
        else:
            out[k] = v
    return out


def _compute_cache_key(*, url: str, body: dict[str, Any] | None) -> str:
    payload = json.dumps(body or {}, sort_keys=True, ensure_ascii=False, default=str)
    raw = f"{url}\n{payload}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_path(*, cache_dir: Path, key: str) -> Path:
    return cache_dir / key[:2] / f"{key}.json"


def _resolve_cache_dir() -> Path:
    settings = get_settings()
    raw = getattr(settings, "llm_cache_dir", "apps/backend/data/llm_cache")
    p = Path(raw)
    if not p.is_absolute():
        # resolve relative to cwd of the backend process
        p = Path(os.getcwd()) / p
    return p


class _ReplayedResponse:
    """Minimal stand-in for httpx.Response - supports .json(),
    .raise_for_status(), .status_code, .headers, .text used by callers."""

    def __init__(self, *, status: int, json_data: dict[str, Any], headers: dict[str, str]):
        self.status_code = int(status)
        self._json = json_data
        self.headers = dict(headers or {})
        self.text = json.dumps(json_data, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"replayed status {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


def _save_cache_entry(
    *,
    cache_dir: Path,
    key: str,
    url: str,
    body: dict[str, Any] | None,
    headers: dict[str, str] | None,
    response: httpx.Response,
    provider_hint: str,
) -> None:
    log = logging.getLogger("app.services.llm_cache")
    try:
        path = _cache_path(cache_dir=cache_dir, key=key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            response_json = response.json()
        except Exception:  # noqa: BLE001
            response_json = {"_raw_text": response.text[:50000]}
        entry = {
            "url": url,
            "request_body": body or {},
            "request_headers_redacted": _redact_headers(headers),
            "response_status": int(response.status_code),
            "response_headers": dict(response.headers),
            "response_json": response_json,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "provider_hint": provider_hint,
        }
        path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "llm_cache.save_failed",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)[:200]},
        )


def _load_cache_entry(*, cache_dir: Path, key: str) -> dict[str, Any] | None:
    path = _cache_path(cache_dir=cache_dir, key=key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def cached_http_post(
    *,
    url: str,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    provider_hint: str = "",
) -> httpx.Response | _ReplayedResponse:
    """Wrap httpx.post with optional record/replay caching.

    See module docstring for cache key semantics and on-disk format.
    Behavior is byte-identical to httpx.post when llm_cache_mode is "off".
    """
    settings = get_settings()
    mode = (getattr(settings, "llm_cache_mode", "off") or "off").lower().strip()
    if mode not in ("record", "replay", "auto"):
        return httpx.post(url, json=json, headers=headers, timeout=timeout)

    cache_dir = _resolve_cache_dir()
    key = _compute_cache_key(url=url, body=json)
    log = logging.getLogger("app.services.llm_cache")

    if mode == "auto":
        # Hybrid: cache hit returns cached response without HTTP call;
        # cache miss calls live API AND records the response, so the
        # next identical call hits cache automatically. Best-of-both:
        # zero replay cost when prompts repeat, zero correctness risk
        # because new prompts always go to the real provider.
        entry = _load_cache_entry(cache_dir=cache_dir, key=key)
        if entry is not None:
            log.info(
                "llm_cache.auto_hit",
                extra={"key": key, "provider_hint": provider_hint, "url": url[:80]},
            )
            return _ReplayedResponse(
                status=int(entry.get("response_status", 200)),
                json_data=entry.get("response_json", {}),
                headers=entry.get("response_headers", {}),
            )
        log.info(
            "llm_cache.auto_miss_recording",
            extra={"key": key, "provider_hint": provider_hint, "url": url[:80]},
        )
        response = httpx.post(url, json=json, headers=headers, timeout=timeout)
        if 200 <= response.status_code < 300:
            _save_cache_entry(
                cache_dir=cache_dir,
                key=key,
                url=url,
                body=json,
                headers=headers,
                response=response,
                provider_hint=provider_hint,
            )
        return response

    if mode == "replay":
        entry = _load_cache_entry(cache_dir=cache_dir, key=key)
        if entry is not None:
            log.info(
                "llm_cache.replay_hit",
                extra={"key": key, "provider_hint": provider_hint, "url": url[:80]},
            )
            return _ReplayedResponse(
                status=int(entry.get("response_status", 200)),
                json_data=entry.get("response_json", {}),
                headers=entry.get("response_headers", {}),
            )
        log.info(
            "llm_cache.replay_miss",
            extra={"key": key, "provider_hint": provider_hint, "url": url[:80]},
        )
        miss_policy = (getattr(settings, "llm_cache_replay_on_miss", "passthrough") or "passthrough").lower()
        if miss_policy == "raise":
            raise LLMCacheMissError(
                f"No cached response for provider_hint={provider_hint!r}, key={key} "
                f"(set OPS_AGENT_LLM_CACHE_MODE=record to capture or "
                f"OPS_AGENT_LLM_CACHE_REPLAY_ON_MISS=passthrough to fall through)."
            )
        # passthrough on miss
        return httpx.post(url, json=json, headers=headers, timeout=timeout)

    # mode == "record"
    response = httpx.post(url, json=json, headers=headers, timeout=timeout)
    if 200 <= response.status_code < 300:
        _save_cache_entry(
            cache_dir=cache_dir,
            key=key,
            url=url,
            body=json,
            headers=headers,
            response=response,
            provider_hint=provider_hint,
        )
    return response
