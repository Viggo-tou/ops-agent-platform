from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeSyncResponse(BaseModel):
    source_name: str = Field(..., description="Name of the knowledge source being synced")
    source_path: str = Field(..., description="File system path of the knowledge source")
    indexed_documents: int = Field(..., description="Number of documents indexed from the source")
    updated_documents: int = Field(..., description="Number of documents updated during sync")
    removed_documents: int = Field(..., description="Number of documents removed during sync")


class KnowledgeSourceDescriptor(BaseModel):
    source_name: str = Field(..., description="Name of the knowledge source")
    source_path: str = Field(..., description="File system path of the knowledge source")
    indexed_document_count: int = Field(..., description="Total number of documents indexed from this source")


class KnowledgeCitation(BaseModel):
    document_id: str = Field(..., description="Unique identifier of the cited document")
    source_name: str = Field(..., description="Name of the source containing the citation")
    title: str = Field(..., description="Title of the cited document")
    relative_path: str = Field(..., description="Relative file path of the cited document")
    line_start: int = Field(..., description="Starting line number of the citation in the document")
    line_end: int = Field(..., description="Ending line number of the citation in the document")
    snippet: str = Field(..., description="Text snippet of the citation")
    score: float = Field(..., description="Relevance score of the citation")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata for the citation")


class KnowledgeAnswerTrace(BaseModel):
    source_name: str = Field(..., description="Name of the knowledge source used")
    source_path: str = Field(..., description="File system path of the source")
    selected_sources: list[str] = Field(default_factory=list, description="List of source names selected for the answer")
    strategy: str = Field(..., description="Search strategy employed")
    route_kind: str = Field(..., description="Type of routing used")
    route_reason: str = Field(..., description="Explanation for the routing choice")
    top_k: int = Field(..., description="Number of top results considered")
    indexed_document_count: int = Field(..., description="Total number of documents indexed from the source")
    selected_paths: list[str] = Field(..., description="File paths of documents selected for the answer")
    matched_tokens: list[str] = Field(default_factory=list, description="Tokens that matched the query")
    token_coverage: float = Field(..., description="Fraction of query tokens covered by matched documents")
    top_score: float = Field(..., description="Highest relevance score among results")
    citation_count: int = Field(..., description="Number of citations included in the answer")
    hallucination_risk: str = Field(..., description="Risk level of potential hallucination in the answer")
    rationale: str = Field(..., description="Explanation for why this answer was generated")
    answer_provider: str = Field(default="template", description="Provider used to produce the final answer text")


class KnowledgeSearchResult(BaseModel):
    query: str = Field(..., description="The search query string")
    answer: str = Field(..., description="Generated answer text")
    citations: list[KnowledgeCitation] = Field(default_factory=list, description="Citations supporting the answer")
    answer_trace: KnowledgeAnswerTrace = Field(..., description="Detailed trace of the answer generation process")
    packaged_context: str = Field(..., description="Formatted context string used for generation")


class KnowledgeSearchResponse(KnowledgeSearchResult):
    pass


class KnowledgeDocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Unique identifier of the document")
    source_name: str = Field(..., description="Name of the knowledge source containing the document")
    relative_path: str = Field(..., description="Relative file path of the document")
    title: str = Field(..., description="Title of the document")
    extension: str = Field(..., description="File extension of the document")
    language: str | None = Field(default=None, description="Programming or natural language of the document")
    size_bytes: int = Field(..., description="Size of the document in bytes")
    line_count: int = Field(..., description="Number of lines in the document")
    metadata_json: dict[str, Any] | None = Field(default=None, description="Additional metadata stored as JSON")
    updated_at: datetime = Field(..., description="UTC timestamp of last document update")


class KnowledgeUploadSkipped(BaseModel):
    file_name: str = Field(..., description="Name of the file that was skipped")
    reason: str = Field(..., description="Reason why the file was skipped during upload")


class KnowledgeUploadResponse(BaseModel):
    source_name: str = Field(..., description="Name of the knowledge source")
    source_path: str = Field(..., description="File system path of the source")
    indexed_documents: list[KnowledgeDocumentSummary] = Field(default_factory=list, description="Documents successfully indexed")
    skipped: list[KnowledgeUploadSkipped] = Field(default_factory=list, description="Files that were skipped during upload")


class KnowledgeDeleteResponse(BaseModel):
    source_name: str = Field(..., description="Name of the knowledge source")
    removed_documents: int = Field(..., description="Number of documents removed")
    removed_from_disk: bool = Field(..., description="Whether the documents were also removed from disk")
