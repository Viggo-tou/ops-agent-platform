from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import ActorContext, require_permission
from app.schemas.model_config import ModelProviderRead, SelectedModelRead, SelectedModelUpdate
from app.services.model_config import get_selected_model, list_providers, set_selected_model

router = APIRouter(prefix="/model-config", tags=["model-config"])
DbSession = Annotated[Session, Depends(get_db)]
ModelConfigActorCtx = Annotated[ActorContext, Depends(require_permission("settings:model_config"))]


@router.get("/providers", response_model=list[ModelProviderRead])
def get_model_providers(db: DbSession) -> list[ModelProviderRead]:
    return list_providers(db)


@router.get("/selected", response_model=SelectedModelRead)
def get_selected(db: DbSession) -> SelectedModelRead:
    return get_selected_model(db)


@router.patch("/selected", response_model=SelectedModelRead)
def update_selected(payload: SelectedModelUpdate, db: DbSession, _actor: ModelConfigActorCtx) -> SelectedModelRead:
    try:
        return set_selected_model(db, payload.model_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


# --- Effective runtime config readout (read-only, infers preset mode) -----


class EffectiveRuntime(BaseModel):
    detected_mode: str  # "recommended" | "cli" | "api" | "advanced"
    planner_provider: str | None
    codegen_provider: str | None
    knowledge_synthesis_provider: str | None
    primary_agent_provider: str | None
    selected_model_id: str | None
    notes: list[str]


def _infer_mode(s) -> tuple[str, list[str]]:
    """Heuristic: derive preset mode + diagnostic notes from settings shape."""
    notes: list[str] = []
    planner = (getattr(s, "planner_provider", None) or "auto").lower()
    codegen = (getattr(s, "codegen_provider", None) or "auto").lower()
    primary = (getattr(s, "primary_agent_provider", None) or "auto").lower()

    cli_runtimes = {"claude_code", "codex"}
    api_runtimes = {"anthropic", "openai", "minimax", "deepseek"}

    if planner in cli_runtimes or codegen in cli_runtimes:
        if planner == codegen and planner in cli_runtimes:
            return "cli", [f"Both planner and codegen route through {planner} CLI."]
        notes.append(
            f"Mixed routing: planner={planner}, codegen={codegen} — closest preset is Advanced."
        )
        return "advanced", notes

    if planner in api_runtimes and codegen in api_runtimes:
        return "api", [f"planner={planner}, codegen={codegen} via direct API."]

    if planner == "auto" and codegen == "auto" and primary == "auto":
        return "recommended", ["All providers set to auto; system picks per stage."]

    notes.append(
        f"Custom mix detected: planner={planner}, codegen={codegen}, primary={primary}."
    )
    return "advanced", notes


@router.get("/runtime", response_model=EffectiveRuntime)
def get_effective_runtime(db: DbSession) -> EffectiveRuntime:
    s = get_settings()
    mode, notes = _infer_mode(s)
    try:
        selected = get_selected_model(db)
        selected_id = selected.model_id if selected else None
    except Exception:  # noqa: BLE001
        selected_id = None
    return EffectiveRuntime(
        detected_mode=mode,
        planner_provider=getattr(s, "planner_provider", None),
        codegen_provider=getattr(s, "codegen_provider", None),
        knowledge_synthesis_provider=getattr(s, "knowledge_synthesis_provider", None),
        primary_agent_provider=getattr(s, "primary_agent_provider", None),
        selected_model_id=selected_id,
        notes=notes,
    )
