from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas.knowledge import (
    KnowledgeDocumentSummary,
    KnowledgeSearchResponse,
    KnowledgeSourceDescriptor,
    KnowledgeSyncResponse,
)
from app.services.knowledge import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/sync", response_model=KnowledgeSyncResponse)
def sync_knowledge(db: DbSession, source_name: str | None = None) -> KnowledgeSyncResponse:
    service = KnowledgeService(db)
    return service.sync_repositories(source_name=source_name)


@router.get("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    query: str,
    db: DbSession,
    top_k: int | None = None,
    source_name: str | None = None,
) -> KnowledgeSearchResponse:
    service = KnowledgeService(db)
    return service.search_repositories(query=query, top_k=top_k, source_name=source_name)


@router.get("/documents", response_model=list[KnowledgeDocumentSummary])
def list_knowledge_documents(
    db: DbSession,
    limit: int = 100,
    source_name: str | None = None,
) -> list[KnowledgeDocumentSummary]:
    service = KnowledgeService(db)
    return service.list_documents(limit=limit, source_name=source_name)


@router.get("/sources", response_model=list[KnowledgeSourceDescriptor])
def list_knowledge_sources(db: DbSession) -> list[KnowledgeSourceDescriptor]:
    service = KnowledgeService(db)
    return service.list_sources()
