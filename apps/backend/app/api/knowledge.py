from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import ActorContext, require_permission
from app.schemas.knowledge import (
    KnowledgeDeleteResponse,
    KnowledgeDocumentSummary,
    KnowledgeSearchResponse,
    KnowledgeSourceDescriptor,
    KnowledgeSyncResponse,
    KnowledgeUploadResponse,
)
from app.services.knowledge import KnowledgeService
from app.services.knowledge_zip import ZipImportError, extract_zip_safely

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
DbSession = Annotated[Session, Depends(get_db)]
KnowledgeUploadActorCtx = Annotated[ActorContext, Depends(require_permission("knowledge:upload"))]
KnowledgeDeleteActorCtx = Annotated[ActorContext, Depends(require_permission("knowledge:delete"))]


@router.post("/sync", response_model=KnowledgeSyncResponse)
def sync_knowledge(
    db: DbSession,
    _actor: KnowledgeUploadActorCtx,
    source_name: str | None = None,
) -> KnowledgeSyncResponse:
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


@router.post("/upload", response_model=KnowledgeUploadResponse)
async def upload_knowledge_documents(
    db: DbSession,
    _actor: KnowledgeUploadActorCtx,
    files: list[UploadFile] = File(...),
    source_name: str | None = Form(default=None),
) -> KnowledgeUploadResponse:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one file is required.",
        )

    payload: list[tuple[str, bytes]] = []
    for upload in files:
        data = await upload.read()
        payload.append((upload.filename or "", data))

    service = KnowledgeService(db)
    try:
        return service.upload_documents(files=payload, source_name=source_name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


@router.post("/upload-zip", response_model=KnowledgeUploadResponse)
async def upload_knowledge_zip(
    db: DbSession,
    _actor: KnowledgeUploadActorCtx,
    archive: UploadFile = File(...),
    source_name: str | None = Form(default=None),
) -> KnowledgeUploadResponse:
    archive_bytes = await archive.read()
    if not archive_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Archive is empty.",
        )

    try:
        payload = extract_zip_safely(archive_bytes)
    except ZipImportError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": error.reason, "entry": error.entry},
        )

    service = KnowledgeService(db)
    try:
        return service.upload_documents(files=payload, source_name=source_name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


@router.delete("/documents/{document_id}", response_model=KnowledgeDeleteResponse)
def delete_knowledge_document(
    document_id: str,
    db: DbSession,
    _actor: KnowledgeDeleteActorCtx,
) -> KnowledgeDeleteResponse:
    service = KnowledgeService(db)
    try:
        return service.delete_document(document_id=document_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


@router.delete("/sources/{source_name}", response_model=KnowledgeDeleteResponse)
def delete_knowledge_source(
    source_name: str,
    db: DbSession,
    _actor: KnowledgeDeleteActorCtx,
) -> KnowledgeDeleteResponse:
    service = KnowledgeService(db)
    try:
        return service.delete_source(source_name=source_name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
