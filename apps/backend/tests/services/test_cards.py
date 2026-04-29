from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.core.db import create_knowledge_fts_table  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.knowledge_card import KnowledgeCard  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.services.cards import (  # noqa: E402
    CARD_PROMPT_VERSION,
    CardGenerationError,
    CardGenerator,
    upsert_card,
)
from app.services.knowledge import KnowledgeService, _upsert_fts  # noqa: E402
from app.services.knowledge_synthesis import KnowledgeSynthesizer  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    create_knowledge_fts_table(session)
    session.commit()
    try:
        yield session, SessionLocal
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _settings(**overrides: object) -> Settings:
    values = {
        "minimax_api_key": "test-key",
        "knowledge_source_name": "repo",
        "knowledge_source_path": str(BACKEND_ROOT),
        "knowledge_upload_root": str(BACKEND_ROOT / "missing-test-uploads"),
        "knowledge_synthesis_enabled": False,
        "knowledge_rerank_enabled": False,
        "knowledge_query_rewrite_enabled": False,
        "cc_agentic_enabled": False,
        "knowledge_fts5_enabled": True,
        "knowledge_cards_model": "MiniMax-M2.7",
        "knowledge_cards_concurrency": 1,
    }
    values.update(overrides)
    return Settings(**values)


def _add_doc(session, *, relative_path: str = "src/Login.js", content: str = "export default function Login() {}") -> KnowledgeDocument:
    encoded = content.encode("utf-8")
    document = KnowledgeDocument(
        source_name="repo",
        relative_path=relative_path,
        title=Path(relative_path).name,
        extension=Path(relative_path).suffix or ".js",
        language="javascript",
        size_bytes=len(encoded),
        line_count=max(1, len(content.splitlines())),
        content_hash=hashlib.sha256(encoded).hexdigest(),
        metadata_json={},
        content=content,
    )
    session.add(document)
    session.flush()
    return document


def test_card_generator_produces_markdown_card(monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    session, _ = db_session
    document = _add_doc(session)
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {"content": "**File**: src/Login.js\n**Purpose**: Handles login."}}]
    }
    response.raise_for_status.return_value = None
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    monkeypatch.setattr("app.services.cards.httpx.Client", Mock(return_value=client))

    card_text, model_name = CardGenerator(_settings()).generate(document=document)

    assert card_text.startswith("**File**: src/Login.js")
    assert "**Purpose**: Handles login." in card_text
    assert model_name == "MiniMax-M2.7"


def test_card_generator_handles_empty_content(db_session) -> None:
    session, _ = db_session
    document = _add_doc(session, content="")

    card_text, _ = CardGenerator(_settings()).generate(document=document)

    assert card_text == "**File**: src/Login.js\n**Purpose**: (empty / non-code file)"


def test_card_generator_handles_llm_error(monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    session, _ = db_session
    document = _add_doc(session)
    monkeypatch.setattr(
        "app.services.cards.httpx.Client",
        Mock(side_effect=httpx.HTTPError("network down")),
    )

    with pytest.raises(CardGenerationError):
        CardGenerator(_settings()).generate(document=document)

    assert session.query(KnowledgeCard).count() == 0


def test_build_cards_skips_existing_when_flag_set(monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    session, SessionLocal = db_session
    document = _add_doc(session)
    upsert_card(session, document=document, card_text="existing", model_name="test-model")
    session.commit()
    import scripts.build_cards as build_cards_module

    monkeypatch.setattr(build_cards_module, "SessionLocal", SessionLocal)

    summary = build_cards_module.build_cards(settings=_settings(), skip_existing=True, concurrency=1)

    assert summary.generated == 0
    assert summary.skipped == 1


def test_build_cards_regens_when_content_hash_changed(monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    session, SessionLocal = db_session
    document = _add_doc(session, content="export default function Login() { return 1 }")
    upsert_card(session, document=document, card_text="old", model_name="test-model")
    document.content = "export default function Login() { return 2 }"
    document.content_hash = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
    session.commit()
    import scripts.build_cards as build_cards_module

    monkeypatch.setattr(build_cards_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        build_cards_module.CardGenerator,
        "generate",
        Mock(return_value=("new card", "test-model")),
    )

    summary = build_cards_module.build_cards(settings=_settings(), skip_existing=True, concurrency=1)

    assert summary.generated == 1
    session.expire_all()
    assert session.query(KnowledgeCard).one().card_text == "new card"


def test_search_includes_card_text_in_fts5_match(db_session) -> None:
    session, _ = db_session
    document = _add_doc(
        session,
        relative_path="src/Widget.js",
        content="export default function Widget() {}",
    )
    upsert_card(
        session,
        document=document,
        card_text="**Domain**: auth\n**Notes**: authenticates against firebase admin node",
        model_name="test-model",
    )
    session.commit()
    service = KnowledgeService(session)
    service.settings = _settings()

    result = service.search_repositories(query="firebase admin node", top_k=1)

    assert result.citations[0].document_id == document.id
    assert result.citations[0].card_text is not None


def test_synthesis_format_evidence_includes_card_block() -> None:
    from app.schemas.knowledge import KnowledgeCitation

    citation = KnowledgeCitation(
        document_id="doc-1",
        source_name="repo",
        title="Login.js",
        relative_path="src/Login.js",
        line_start=1,
        line_end=2,
        snippet="export default function Login() {}",
        card_text="**Purpose**: Handles login.",
        score=10.0,
        metadata={},
    )

    evidence = KnowledgeSynthesizer(_settings(knowledge_synthesis_max_snippet_chars=100))._format_evidence([citation])

    assert "[CARD]\n**Purpose**: Handles login.\n[CONTENT]" in evidence
    assert evidence.index("[CARD]") < evidence.index("[CONTENT]")


def test_delete_document_removes_card(db_session) -> None:
    session, _ = db_session
    document = _add_doc(session)
    upsert_card(session, document=document, card_text="card", model_name="test-model")
    session.commit()
    service = KnowledgeService(session)
    service.settings = _settings()

    service.delete_document(document_id=document.id)

    assert session.query(KnowledgeCard).count() == 0


def test_trace_records_cards_available_and_used_counts(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, _ = db_session
    document = _add_doc(session, content="function Login() { return 'login failure' }")
    upsert_card(session, document=document, card_text="**Purpose**: Handles auth login.", model_name="test-model")
    session.commit()
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(return_value="LLM answer."),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_synthesis_enabled=True)

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer_trace.cards_available_count == 1
    assert result.answer_trace.cards_used_count == 1
