from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.evidence import EvidenceItem
from app.schemas.knowledge import KnowledgeAnswerTrace, KnowledgeCitation, KnowledgeSearchResult


def _item(**overrides) -> EvidenceItem:
    payload = {
        "id": "ev-1",
        "source": "rag_lexical",
        "file_path": "src/auth.py",
        "line_start": 10,
        "line_end": 12,
        "snippet": "def login(): pass",
        "enclosing_symbol": "login",
        "chunk_kind": "function",
        "retrieval_channel": "keyword",
        "confidence": 0.75,
        "content_hash": "abc123",
        "metadata": {"score": 37.5},
    }
    payload.update(overrides)
    return EvidenceItem(**payload)


def test_evidence_item_roundtrip() -> None:
    item = _item()

    dumped = item.model_dump(mode="json")
    restored = EvidenceItem.model_validate(dumped)

    assert restored == item
    assert restored.file_path == "src/auth.py"


def test_evidence_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _item(extra_field=True)


def test_evidence_rejects_invalid_source() -> None:
    with pytest.raises(ValidationError):
        _item(source="unknown")


def test_evidence_rejects_absolute_path() -> None:
    with pytest.raises(ValidationError):
        _item(file_path="/etc/passwd")


def test_evidence_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError):
        _item(file_path="../src/auth.py")


def test_evidence_rejects_shell_meaningful_characters() -> None:
    with pytest.raises(ValidationError):
        _item(file_path="src/auth.py;rm")


def test_evidence_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        _item(confidence=1.2)


def test_evidence_rejects_inverted_line_range() -> None:
    with pytest.raises(ValidationError):
        _item(line_start=20, line_end=10)


def test_knowledge_search_result_evidence_items_is_additive() -> None:
    result = KnowledgeSearchResult(
        query="login",
        answer="Check src/auth.py.",
        citations=[
            KnowledgeCitation(
                document_id="doc-1",
                source_name="repo",
                title="auth.py",
                relative_path="src/auth.py",
                line_start=1,
                line_end=2,
                snippet="login",
                score=12.0,
            )
        ],
        evidence_items=[_item()],
        answer_trace=KnowledgeAnswerTrace(
            source_name="repo",
            source_path="repo",
            selected_sources=["repo"],
            strategy="repository_semantic_retrieval",
            route_kind="code_debug",
            route_reason="test",
            top_k=1,
            indexed_document_count=1,
            selected_paths=["src/auth.py"],
            matched_tokens=["login"],
            token_coverage=1.0,
            top_score=12.0,
            citation_count=1,
            hallucination_risk="low",
            rationale="test",
        ),
        packaged_context="[repo:src/auth.py:1-2]\nlogin",
    )

    dumped = result.model_dump(mode="json")

    assert dumped["citations"][0]["relative_path"] == "src/auth.py"
    assert dumped["evidence_items"][0]["source"] == "rag_lexical"
