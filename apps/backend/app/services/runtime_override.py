"""Per-stage provider runtime override.

Allows operators to override which provider runs each pipeline stage
(planner / codegen / synthesis / cc_agent) WITHOUT editing .env or
restarting the backend. Stored in
``data/runtime_overrides.json`` so changes survive restart.

Strict additive contract:
- ``effective_provider(stage_key, default)`` returns override if set, else
  the supplied default (typically ``settings.X_provider``).
- ``set_override(stage_key, value)`` writes (or clears, if value is None
  or empty string).
- All callers MUST treat the return value as advisory — when None /
  empty / missing, fall back to settings.

This pattern mirrors ``repository_registry`` (file lock + atomic write).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings

_FILE = "runtime_overrides.json"
_LOCK = threading.Lock()

# Stages that operators can override. Keep the set tight so we don't
# accept arbitrary keys from the API and turn this into a generic kv store.
SUPPORTED_STAGES = frozenset(
    {
        "planner",       # planner_provider in settings
        "codegen",       # codegen_provider
        "synthesis",     # knowledge_synthesis_provider
        "cc_agent",      # cc_agent_provider_chain (head)
        "primary_agent", # primary_agent_provider
    }
)

# Providers an operator may pick. Mirror the Literal sets in config.py.
SUPPORTED_PROVIDERS_BY_STAGE: dict[str, frozenset[str]] = {
    "planner": frozenset({"auto", "claude_code", "anthropic", "openai", "minimax", "mock"}),
    "codegen": frozenset({"auto", "claude_code", "codex", "anthropic", "openai", "minimax", "deepseek", "ollama", "mock"}),
    "synthesis": frozenset({"minimax", "deepseek"}),
    "cc_agent": frozenset({"claude_code", "codex", "deepseek", "anthropic", "openai", "minimax"}),
    "primary_agent": frozenset({"auto", "mock", "openai", "minimax", "anthropic", "deepseek", "ollama", "claude_code", "codex"}),
}


@dataclass
class StageOverride:
    stage: str
    value: str
    updated_at: str


def _data_path() -> Path:
    settings = get_settings()
    backend_dir = Path(__file__).resolve().parents[2]
    base = getattr(settings, "data_dir", None)
    root = Path(base) if base else backend_dir / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / _FILE


def _read() -> dict[str, str]:
    """Read overrides as plain {stage: value} dict."""
    path = _data_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    raw = data.get("overrides", {}) if isinstance(data, dict) else {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                result[k] = v
    return result


def _write(payload: dict[str, str]) -> None:
    path = _data_path()
    body = {
        "overrides": payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def list_overrides() -> dict[str, str]:
    """Return current overrides as {stage: value} dict (empty when none)."""
    with _LOCK:
        return _read()


def get_override(stage: str) -> str | None:
    """Return raw override value for stage, or None if not set."""
    return list_overrides().get(stage)


def set_override(stage: str, value: str | None) -> None:
    """Write or clear an override. ``value=None`` or empty string clears."""
    if stage not in SUPPORTED_STAGES:
        raise ValueError(f"unsupported stage: {stage}")
    with _LOCK:
        current = _read()
        if not value:
            current.pop(stage, None)
        else:
            allowed = SUPPORTED_PROVIDERS_BY_STAGE.get(stage, frozenset())
            if allowed and value not in allowed:
                raise ValueError(
                    f"provider '{value}' not allowed for stage '{stage}'. "
                    f"allowed: {sorted(allowed)}"
                )
            current[stage] = value
        _write(current)


def effective_provider(stage: str, default: Any) -> Any:
    """Return override if set for stage, else the supplied default.

    Caller pattern:

        from app.services.runtime_override import effective_provider
        provider = effective_provider("codegen", self.settings.codegen_provider)

    Failure-safe: any exception in registry I/O falls through to default.
    """
    if stage not in SUPPORTED_STAGES:
        return default
    try:
        override = get_override(stage)
        return override if override else default
    except Exception:  # noqa: BLE001
        return default


def list_all_with_defaults() -> dict[str, dict[str, str | None]]:
    """Return per-stage status: {stage: {effective, default, override}}.

    Used by /api/model-config/overrides GET endpoint to render UI.
    """
    settings = get_settings()
    settings_map = {
        "planner": getattr(settings, "planner_provider", None),
        "codegen": getattr(settings, "codegen_provider", None),
        "synthesis": getattr(settings, "knowledge_synthesis_provider", None),
        "cc_agent": _first_in_chain(getattr(settings, "cc_agent_provider_chain", "")),
        "primary_agent": getattr(settings, "primary_agent_provider", None),
    }
    overrides = list_overrides()
    out: dict[str, dict[str, str | None]] = {}
    for stage in sorted(SUPPORTED_STAGES):
        default = settings_map.get(stage)
        override = overrides.get(stage)
        out[stage] = {
            "default": str(default) if default else None,
            "override": override,
            "effective": override or (str(default) if default else None),
            "allowed": sorted(SUPPORTED_PROVIDERS_BY_STAGE.get(stage, [])),
        }
    return out


def _first_in_chain(chain: str | None) -> str | None:
    if not chain:
        return None
    parts = [p.strip() for p in str(chain).split(",") if p.strip()]
    return parts[0] if parts else None
