from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EvidenceSource = Literal[
    "rag_lexical",
    "rag_fts5",
    "rag_card",
    "cc_glob",
    "cc_grep",
    "cc_read",
    "user_provided",
    "spec_anchor",
]


ChunkKind = Literal[
    "function",
    "method",
    "class",
    "module",
    "line_window",
    "grep_hit",
    "synthetic",
]


_SHELL_MEANINGFUL_CHARS = re.compile(r"[\x00-\x1f`$;&|<>'\"*?!]")


def _validate_repo_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("file_path must not be empty")
    if _SHELL_MEANINGFUL_CHARS.search(normalized):
        raise ValueError("file_path contains unsafe characters")
    if PurePosixPath(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute():
        raise ValueError("file_path must be repository-relative")
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("file_path must not contain dot segments")
    return normalized


class EvidenceItem(BaseModel):
    """Unified evidence representation across retrieval channels."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="UUID for cross-references")
    source: EvidenceSource
    file_path: str = Field(..., description="Repository-relative path; absolute paths rejected")
    line_start: int | None = None
    line_end: int | None = None
    snippet: str = ""
    enclosing_symbol: str | None = None
    chunk_kind: ChunkKind | None = None
    retrieval_channel: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    content_hash: str | None = Field(default=None, description="Hash of source file at retrieval time")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("file_path")
    @classmethod
    def _file_path_is_repo_relative(cls, value: str) -> str:
        return _validate_repo_relative_path(value)

    @model_validator(mode="after")
    def _line_range_is_ordered(self) -> "EvidenceItem":
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end must be greater than or equal to line_start")
        return self
