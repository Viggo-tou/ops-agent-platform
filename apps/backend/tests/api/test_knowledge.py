from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app  # noqa: E402
from app.schemas.knowledge import (  # noqa: E402
    KnowledgeAnswerTrace,
    KnowledgeCitation,
    KnowledgeSearchResult,
)


def test_search_response_includes_answer_provider(monkeypatch) -> None:
    result = KnowledgeSearchResult(
        query="login failure",
        answer="Check handymanapp:src/auth.py (lines 1-2).",
        citations=[
            KnowledgeCitation(
                document_id="doc-1",
                source_name="handymanapp",
                title="auth.py",
                relative_path="src/auth.py",
                line_start=1,
                line_end=2,
                snippet="login failure",
                score=12.0,
                metadata={},
            )
        ],
        answer_trace=KnowledgeAnswerTrace(
            source_name="handymanapp",
            source_path="source",
            selected_sources=["handymanapp"],
            strategy="repository_semantic_retrieval",
            route_kind="code_debug",
            route_reason="test",
            top_k=1,
            indexed_document_count=1,
            selected_paths=["src/auth.py"],
            matched_tokens=["login", "failure"],
            token_coverage=1.0,
            top_score=12.0,
            citation_count=1,
            hallucination_risk="low",
            rationale="test",
            answer_provider="minimax",
        ),
        packaged_context="[handymanapp:src/auth.py:1-2]\nlogin failure",
    )
    search = Mock(return_value=result)
    service = Mock()
    service.search_repositories = search
    monkeypatch.setattr("app.api.knowledge.KnowledgeService", Mock(return_value=service))

    response = TestClient(app).get("/api/knowledge/search", params={"query": "login failure"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["answer_trace"]["answer_provider"] == "minimax"
    assert body["citations"][0]["relative_path"] == "src/auth.py"
    assert body["claims"] == []
    assert body["ungrounded_claim_count"] == 0
    search.assert_called_once_with(query="login failure", top_k=None, source_name=None)
