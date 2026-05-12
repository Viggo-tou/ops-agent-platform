"""Runtime configuration: per-stage provider overrides + CLI/API auth status.

GET  /api/runtime-config/overrides              — list per-stage status
PATCH /api/runtime-config/overrides             — update an override
GET  /api/runtime-config/auth-status            — detect CLI logins + API keys
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import ActorContext, require_permission
from app.services.runtime_override import (
    SUPPORTED_PROVIDERS_BY_STAGE,
    SUPPORTED_STAGES,
    list_all_with_defaults,
    set_override,
)

router = APIRouter(prefix="/runtime-config", tags=["runtime-config"])
ViewActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]
WriteActorCtx = Annotated[ActorContext, Depends(require_permission("settings:model_config"))]


# --- Per-stage overrides -------------------------------------------------


class StageStatus(BaseModel):
    stage: str
    default: str | None
    override: str | None
    effective: str | None
    allowed: list[str]


class OverridesResponse(BaseModel):
    stages: list[StageStatus]


class OverrideUpdate(BaseModel):
    stage: str = Field(min_length=1, max_length=64)
    # Empty string or null clears the override and falls back to .env default.
    value: str | None = Field(default=None, max_length=64)


@router.get("/overrides", response_model=OverridesResponse)
def get_overrides(_actor: ViewActorCtx) -> OverridesResponse:
    rows = list_all_with_defaults()
    return OverridesResponse(
        stages=[
            StageStatus(
                stage=stage,
                default=info.get("default"),
                override=info.get("override"),
                effective=info.get("effective"),
                allowed=info.get("allowed", []) or [],
            )
            for stage, info in rows.items()
        ],
    )


@router.patch("/overrides", response_model=OverridesResponse)
def patch_override(payload: OverrideUpdate, _actor: WriteActorCtx) -> OverridesResponse:
    if payload.stage not in SUPPORTED_STAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stage '{payload.stage}' not supported",
        )
    try:
        set_override(payload.stage, payload.value or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_overrides(_actor=_actor)  # type: ignore[arg-type]


# --- CLI / API auth status -----------------------------------------------


class CliAuthEntry(BaseModel):
    key: str
    label: str
    cli_available: bool        # binary on PATH
    authenticated: bool        # auth file present (best-effort)
    auth_path: str | None
    login_command: str
    notes: str = ""


class ApiKeyEntry(BaseModel):
    key: str
    label: str
    env_var: str
    set: bool                   # value present in .env
    notes: str = ""


class AuthStatusResponse(BaseModel):
    cli: list[CliAuthEntry]
    api_keys: list[ApiKeyEntry]


def _user_home() -> Path:
    """Cross-platform user home dir."""
    return Path(os.path.expanduser("~"))


def _path_exists_nonempty(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        try:
            return path.stat().st_size > 0
        except OSError:
            return False
    if path.is_dir():
        try:
            return any(path.iterdir())
        except (OSError, PermissionError):
            return False
    return False


def _detect_claude_code() -> tuple[bool, bool, str | None]:
    """(callable, authenticated, auth_path).

    "callable" is True when EITHER `claude` is on PATH OR `npx` is on PATH
    (the codebase uses `npx --yes @anthropic-ai/claude-code` as the default
    invocation). authenticated is detected from ~/.claude presence.
    """
    callable_via = shutil.which("claude") is not None or shutil.which("npx") is not None
    auth_dir = _user_home() / ".claude"
    authed = (auth_dir / "projects").is_dir() and any(
        (auth_dir / "projects").iterdir() if (auth_dir / "projects").exists() else []
    )
    if not authed:
        authed = _path_exists_nonempty(auth_dir)
    return callable_via, authed, str(auth_dir) if auth_dir.exists() else None


def _detect_codex() -> tuple[bool, bool, str | None]:
    bin_present = shutil.which("codex") is not None
    auth_dir = _user_home() / ".codex"
    auth_file_candidates = [
        auth_dir / "auth.json",
        auth_dir / "credentials.json",
        auth_dir / "session.json",
    ]
    authed = any(_path_exists_nonempty(p) for p in auth_file_candidates)
    if not authed and auth_dir.exists():
        # Some codex versions just store under .codex/ without canonical name.
        try:
            authed = any(p.is_file() for p in auth_dir.iterdir())
        except (OSError, PermissionError):
            authed = False
    return bin_present, authed, str(auth_dir) if auth_dir.exists() else None


def _detect_gemini() -> tuple[bool, bool, str | None]:
    bin_present = shutil.which("gemini") is not None
    auth_dir = _user_home() / ".gemini"
    authed = _path_exists_nonempty(auth_dir)
    return bin_present, authed, str(auth_dir) if auth_dir.exists() else None


@router.get("/auth-status", response_model=AuthStatusResponse)
def auth_status(_actor: ViewActorCtx) -> AuthStatusResponse:
    s = get_settings()

    cc_bin, cc_authed, cc_path = _detect_claude_code()
    cx_bin, cx_authed, cx_path = _detect_codex()
    gm_bin, gm_authed, gm_path = _detect_gemini()

    cli = [
        CliAuthEntry(
            key="claude_code",
            label="Claude Code CLI",
            cli_available=cc_bin,
            authenticated=cc_authed,
            auth_path=cc_path,
            login_command="claude /login",
            notes=("" if cc_authed else "Run `claude /login` in a terminal once. Auth persists across sessions."),
        ),
        CliAuthEntry(
            key="codex",
            label="Codex CLI",
            cli_available=cx_bin,
            authenticated=cx_authed,
            auth_path=cx_path,
            login_command="codex login",
            notes=("" if cx_authed else "Install via `npm i -g @openai/codex` then `codex login` (uses ChatGPT auth)."),
        ),
        CliAuthEntry(
            key="gemini",
            label="Gemini CLI",
            cli_available=gm_bin,
            authenticated=gm_authed,
            auth_path=gm_path,
            login_command="gemini auth login",
            notes=("" if gm_authed else "Install Google Gemini CLI and run `gemini auth login`."),
        ),
    ]

    api_keys = [
        ApiKeyEntry(
            key="anthropic",
            label="Anthropic API",
            env_var="OPS_AGENT_ANTHROPIC_API_KEY",
            set=bool((getattr(s, "anthropic_api_key", "") or "").strip()),
        ),
        ApiKeyEntry(
            key="openai",
            label="OpenAI API",
            env_var="OPS_AGENT_OPENAI_API_KEY",
            set=bool((getattr(s, "openai_api_key", "") or "").strip()),
        ),
        ApiKeyEntry(
            key="deepseek",
            label="DeepSeek API",
            env_var="OPS_AGENT_DEEPSEEK_API_KEY",
            set=bool((getattr(s, "deepseek_api_key", "") or "").strip()),
        ),
        ApiKeyEntry(
            key="minimax",
            label="MiniMax API",
            env_var="OPS_AGENT_MINIMAX_API_KEY",
            set=bool((getattr(s, "minimax_api_key", "") or "").strip()),
        ),
    ]

    return AuthStatusResponse(cli=cli, api_keys=api_keys)
