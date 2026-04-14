from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.memory import MemoryItem, MemorySettings
from app.schemas.memory import MemoryItemCreate, MemoryItemUpdate, MemorySettingsUpdate

MEMORY_SETTINGS_ID = "default"


def list_memory_items(db: Session, search: str | None = None) -> list[MemoryItem]:
    stmt = select(MemoryItem)
    normalized = (search or "").strip()
    if normalized:
        pattern = f"%{normalized.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(MemoryItem.title).like(pattern),
                func.lower(MemoryItem.body).like(pattern),
                func.lower(MemoryItem.topic).like(pattern),
            )
        )
    stmt = stmt.order_by(MemoryItem.updated_at.desc())
    return list(db.scalars(stmt))


def create_memory_item(db: Session, payload: MemoryItemCreate) -> MemoryItem:
    item = MemoryItem(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_memory_item(db: Session, item_id: str, payload: MemoryItemUpdate) -> MemoryItem:
    item = db.get(MemoryItem, item_id)
    if item is None:
        raise LookupError(f"Memory item not found: {item_id}")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return item


def delete_memory_item(db: Session, item_id: str) -> None:
    item = db.get(MemoryItem, item_id)
    if item is None:
        raise LookupError(f"Memory item not found: {item_id}")

    db.delete(item)
    db.commit()


def get_memory_settings(db: Session) -> MemorySettings:
    memory_settings = db.get(MemorySettings, MEMORY_SETTINGS_ID)
    if memory_settings is None:
        memory_settings = MemorySettings(id=MEMORY_SETTINGS_ID)
        db.add(memory_settings)
        db.commit()
        db.refresh(memory_settings)
    return memory_settings


def update_memory_settings(db: Session, payload: MemorySettingsUpdate) -> MemorySettings:
    memory_settings = get_memory_settings(db)

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(memory_settings, key, value)

    db.commit()
    db.refresh(memory_settings)
    return memory_settings
