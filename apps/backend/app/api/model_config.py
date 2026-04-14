from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

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
