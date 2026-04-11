from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas.tool import ToolRegistryEntryRead
from app.tools.gateway import ToolGateway

router = APIRouter(prefix="/tools", tags=["tools"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/registry", response_model=list[ToolRegistryEntryRead])
def list_tool_registry(db: DbSession) -> list[ToolRegistryEntryRead]:
    gateway = ToolGateway(db)
    return gateway.list_registry_entries()
