from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeSyncResponse(BaseModel):
    source_name: str
    source_path: str
    indexed_documents: int
    updated_documents: int
    removed_documents: int


class KnowledgeSourceDescriptor(BaseModel):
    source_name: str
    source_path: str
    indexed_document_count: int


class KnowledgeCitation(BaseModel):
    document_id: str
    source_name: str
    title: str
    relative_path: str
    line_start: int
    line_end: int
    snippet: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeAnswerTrace(BaseModel):
    source_name: str
    source_path: str
    selected_sources: list[str] = Field(default_factory=list)
    strategy: str
    route_kind: str
    route_reason: str
    top_k: int
    indexed_document_count: int
    selected_paths: list[str]
    matched_tokens: list[str] = Field(default_factory=list)
    token_coverage: float
    top_score: float
    citation_count: int
    hallucination_risk: str
    rationale: str


class KnowledgeSearchResult(BaseModel):
    query: str
    answer: str
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    answer_trace: KnowledgeAnswerTrace
    packaged_context: str


class KnowledgeSearchResponse(KnowledgeSearchResult):
    pass


class KnowledgeDocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_name: str
    relative_path: str
    title: str
    extension: str
    language: str | None = None
    size_bytes: int
    line_count: int
    metadata_json: dict[str, Any] | None = None
    updated_at: datetime
