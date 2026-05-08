"""Repository / knowledge-source listing + management API."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.security import ActorContext, require_permission
from app.services.repository_registry import (
    RegistryError,
    clone_git_source,
    list_all_sources_for_api,
    list_managed_sources,
    remove_managed_source,
    upload_zip_source,
)

router = APIRouter(prefix="/repositories", tags=["repositories"])
RepoViewActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]
RepoWriteActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]


class RepositorySource(BaseModel):
    name: str
    path: str
    description: str = ""
    origin: str = "env"
    git_url: str = ""
    added_at: str = ""


class RepositoryListResponse(BaseModel):
    sources: list[RepositorySource]
    multi_source_enabled: bool


class CloneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    git_url: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=1000)


@router.get("/sources", response_model=RepositoryListResponse)
def list_sources(_actor: RepoViewActorCtx) -> RepositoryListResponse:
    rows = list_all_sources_for_api()
    return RepositoryListResponse(
        sources=[RepositorySource(**row) for row in rows],
        multi_source_enabled=len(rows) > 1,
    )


@router.post("/upload", response_model=RepositorySource, status_code=status.HTTP_201_CREATED)
async def upload_zip(
    _actor: RepoWriteActorCtx,
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
) -> RepositorySource:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="upload must be a .zip file")
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        record = upload_zip_source(
            name=name,
            description=description or "",
            zip_bytes=payload,
        )
    except (RegistryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RepositorySource(**record.to_dict())


@router.post("/clone", response_model=RepositorySource, status_code=status.HTTP_201_CREATED)
def clone_repo(payload: CloneRequest, _actor: RepoWriteActorCtx) -> RepositorySource:
    try:
        record = clone_git_source(
            name=payload.name,
            description=payload.description,
            git_url=payload.git_url,
        )
    except (RegistryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RepositorySource(**record.to_dict())


@router.delete("/{name}")
def delete_repo(name: str, _actor: RepoWriteActorCtx) -> dict[str, bool]:
    managed = {r.name for r in list_managed_sources()}
    if name not in managed:
        raise HTTPException(status_code=404, detail="source not found in managed registry")
    removed = remove_managed_source(name)
    return {"removed": removed}
