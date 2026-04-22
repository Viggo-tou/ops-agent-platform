from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.schemas.knowledge import KnowledgeCitation  # noqa: E402
from app.services.knowledge import KnowledgeService  # noqa: E402
from app.services.knowledge_synthesis import (  # noqa: E402
    KnowledgeSynthesisError,
    KnowledgeSynthesizer,
)


def _settings(**overrides: object) -> Settings:
    values = {
        "minimax_api_key": "test-key",
        "knowledge_synthesis_enabled": True,
        "knowledge_synthesis_model": "minimax-text-01",
        "knowledge_synthesis_timeout_seconds": 3.0,
        "knowledge_synthesis_max_snippet_chars": 1200,
        "knowledge_source_path": None,
        "knowledge_upload_root": "__missing_upload_root__",
    }
    values.update(overrides)
    return Settings(**values)


def _citation(snippet: str = "login failure handler") -> KnowledgeCitation:
    return KnowledgeCitation(
        document_id="doc-1",
        source_name="handymanapp",
        title="auth.py",
        relative_path="src/auth.py",
        line_start=10,
        line_end=14,
        snippet=snippet,
        score=17.5,
        metadata={},
    )


def test_synthesize_success_returns_llm_text(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": "Use src/auth.py lines 10-14."}}]}
    response.raise_for_status.return_value = None
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    client_factory = Mock(return_value=client)
    monkeypatch.setattr("app.services.knowledge_synthesis.httpx.Client", client_factory)

    result = KnowledgeSynthesizer(_settings()).synthesize(
        query="Where is login failure handled?",
        citations=[_citation()],
        hallucination_risk="low",
        route_kind="code_debug",
        language=None,
    )

    assert result == "Use src/auth.py lines 10-14."
    client_factory.assert_called_once_with(timeout=3.0)
    assert client.post.call_args.kwargs["json"]["model"] == "minimax-text-01"


def test_synthesize_no_api_key_raises() -> None:
    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings(minimax_api_key=None)).synthesize(
            query="Where is login failure handled?",
            citations=[_citation()],
            hallucination_risk="low",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.minimaxi.com/v1/text/chatcompletion_v2")
    response = httpx.Response(500, request=request)
    mocked_response = Mock()
    mocked_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "server error",
        request=request,
        response=response,
    )
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = mocked_response
    monkeypatch.setattr("app.services.knowledge_synthesis.httpx.Client", Mock(return_value=client))

    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings()).synthesize(
            query="Where is login failure handled?",
            citations=[_citation()],
            hallucination_risk="low",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_empty_citations_raises() -> None:
    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings()).synthesize(
            query="Where is login failure handled?",
            citations=[],
            hallucination_risk="high",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_respects_max_snippet_chars() -> None:
    synthesizer = KnowledgeSynthesizer(_settings(knowledge_synthesis_max_snippet_chars=12))
    evidence = synthesizer._format_evidence([_citation(snippet="x" * 50)])

    assert "x" * 12 in evidence
    assert "x" * 13 not in evidence
    assert "(truncated)" in evidence


@pytest.fixture()
def db_session(tmp_path: Path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    source_root = tmp_path / "source"
    source_root.mkdir()
    content = "def login_failure_handler():\n    return 'login failure auth path'\n"
    session.add(
        KnowledgeDocument(
            source_name="handymanapp",
            relative_path="src/auth.py",
            title="auth.py",
            extension=".py",
            language="python",
            size_bytes=len(content.encode("utf-8")),
            line_count=2,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            metadata_json={},
            content=content,
        )
    )
    session.commit()
    try:
        yield session, source_root
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_search_repositories_falls_back_to_template_on_synth_error(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, source_root = db_session
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(side_effect=KnowledgeSynthesisError("provider down")),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_source_path=str(source_root))

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer_trace.answer_provider == "template"
    assert "I would start with" in result.answer


def test_search_repositories_uses_minimax_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, source_root = db_session
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(return_value="LLM synthesized answer with handymanapp:src/auth.py (lines 1-2)."),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_source_path=str(source_root))

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer == "LLM synthesized answer with handymanapp:src/auth.py (lines 1-2)."
    assert result.answer_trace.answer_provider == "minimax"

