"""Repository / knowledge-source listing API.

Parses OPS_AGENT_KNOWLEDGE_SOURCE_SPECS into structured rows so the
frontend /repositories page can display:
- which sources are configured
- which is currently active (knowledge_source_name)
- per-source path + description

Read-only for 1.0. Switching active source is .env edit + restart.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import ActorContext, require_permission

router = APIRouter(prefix="/repositories", tags=["repositories"])
RepoViewActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]


class RepositorySource(BaseModel):
    name: str
    path: str
    description: str = ""
    is_active: bool = False


class RepositoryListResponse(BaseModel):
    sources: list[RepositorySource]
    active: str
    multi_source_enabled: bool


def _parse_specs(raw: str) -> list[RepositorySource]:
    """Parse 'name=path|description;name=path|description' into rows."""
    out: list[RepositorySource] = []
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, rest = entry.split("=", 1)
        if "|" in rest:
            path, desc = rest.split("|", 1)
        else:
            path, desc = rest, ""
        out.append(
            RepositorySource(
                name=name.strip(),
                path=path.strip(),
                description=desc.strip(),
            )
        )
    return out


@router.get("/sources", response_model=RepositoryListResponse)
def list_sources(_actor: RepoViewActorCtx) -> RepositoryListResponse:
    """List all configured knowledge sources with the active one flagged."""
    settings = get_settings()
    raw_specs = (getattr(settings, "knowledge_source_specs", None) or "").strip()
    active_name = (getattr(settings, "knowledge_source_name", None) or "").strip()

    sources = _parse_specs(raw_specs) if raw_specs else []

    # Fallback: when only knowledge_source_path is configured (single-source
    # mode), surface that as a one-row list so the UI still has something
    # to render instead of "no sources".
    if not sources:
        single_path = (getattr(settings, "knowledge_source_path", None) or "").strip()
        if single_path:
            sources = [
                RepositorySource(
                    name=active_name or "default",
                    path=single_path,
                    description="",
                )
            ]

    for source in sources:
        if active_name and source.name == active_name:
            source.is_active = True

    # If active_name is configured but doesn't appear in parsed sources
    # (e.g. operator typo), surface the discrepancy by leaving is_active=False
    # on every row — the UI can highlight it as "Active source not found in
    # specs".
    return RepositoryListResponse(
        sources=sources,
        active=active_name,
        multi_source_enabled=bool(raw_specs),
    )
