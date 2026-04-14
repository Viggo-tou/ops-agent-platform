from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import ActorContext, require_permission
from app.schemas.memory import (
    MemoryItemCreate,
    MemoryItemRead,
    MemoryItemUpdate,
    MemorySettingsRead,
    MemorySettingsUpdate,
)
from app.services.memory import (
    create_memory_item,
    delete_memory_item,
    get_memory_settings,
    list_memory_items,
    update_memory_item,
    update_memory_settings,
)

router = APIRouter(prefix="/memory", tags=["memory"])
DbSession = Annotated[Session, Depends(get_db)]
MemoryEditActorCtx = Annotated[ActorContext, Depends(require_permission("memory:edit"))]


@router.get("/items", response_model=list[MemoryItemRead])
def list_items(db: DbSession, search: str | None = None) -> list[MemoryItemRead]:
    return list_memory_items(db, search=search)


@router.post("/items", response_model=MemoryItemRead, status_code=status.HTTP_201_CREATED)
def create_item(payload: MemoryItemCreate, db: DbSession, _actor: MemoryEditActorCtx) -> MemoryItemRead:
    return create_memory_item(db, payload)


@router.patch("/items/{item_id}", response_model=MemoryItemRead)
def update_item(item_id: str, payload: MemoryItemUpdate, db: DbSession, _actor: MemoryEditActorCtx) -> MemoryItemRead:
    try:
        return update_memory_item(db, item_id, payload)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.delete("/items/{item_id}", response_model=dict[str, bool])
def delete_item(item_id: str, db: DbSession, _actor: MemoryEditActorCtx) -> dict[str, bool]:
    try:
        delete_memory_item(db, item_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return {"ok": True}


@router.get("/settings", response_model=MemorySettingsRead)
def get_settings(db: DbSession) -> MemorySettingsRead:
    return get_memory_settings(db)


@router.patch("/settings", response_model=MemorySettingsRead)
def update_settings(payload: MemorySettingsUpdate, db: DbSession, _actor: MemoryEditActorCtx) -> MemorySettingsRead:
    return update_memory_settings(db, payload)
