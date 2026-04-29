from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.core.db import (  # noqa: E402
    backfill_knowledge_fts_if_empty,
    create_knowledge_fts_table,
)
from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.services.knowledge import (  # noqa: E402
    KnowledgeService,
    SourceSpec,
    _build_fts5_query,
    _upsert_fts,
)


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
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _settings(**overrides: object) -> Settings:
    values = {
        "knowledge_source_name": "repo",
        "knowledge_source_path": str(BACKEND_ROOT),
        "knowledge_upload_root": str(BACKEND_ROOT / "missing-test-uploads"),
        "knowledge_synthesis_enabled": False,
        "knowledge_rerank_enabled": False,
        "knowledge_query_rewrite_enabled": False,
        "cc_agentic_enabled": False,
        "knowledge_fts5_enabled": True,
        "knowledge_fts5_pool_multiplier": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _add_doc(
    session,
    *,
    source_name: str = "repo",
    relative_path: str,
    content: str,
    extension: str = ".py",
) -> KnowledgeDocument:
    encoded = content.encode("utf-8")
    document = KnowledgeDocument(
        source_name=source_name,
        relative_path=relative_path,
        title=Path(relative_path).name,
        extension=extension,
        language="python",
        size_bytes=len(encoded),
        line_count=max(1, len(content.splitlines())),
        content_hash=hashlib.sha256(encoded).hexdigest(),
        metadata_json={},
        content=content,
    )
    session.add(document)
    session.flush()
    return document


def _sync_fts(session, document: KnowledgeDocument) -> None:
    _upsert_fts(
        session,
        document_id=document.id,
        source_name=document.source_name,
        relative_path=document.relative_path,
        title=document.title,
        content=document.content,
    )


def _service(session, **settings_overrides: object) -> KnowledgeService:
    service = KnowledgeService(session)
    service.settings = _settings(**settings_overrides)
    return service


def test_create_fts_table_idempotent(db_session) -> None:
    create_knowledge_fts_table(db_session)
    create_knowledge_fts_table(db_session)

    count = db_session.execute(
        text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'knowledge_document_fts'")
    ).scalar_one()

    assert count == 1


def test_upsert_fts_inserts_and_updates(db_session) -> None:
    _upsert_fts(
        db_session,
        document_id="doc-1",
        source_name="repo",
        relative_path="src/auth.py",
        title="auth.py",
        content="old login handler",
    )
    _upsert_fts(
        db_session,
        document_id="doc-1",
        source_name="repo",
        relative_path="src/auth.py",
        title="auth.py",
        content="new admin handler",
    )

    rows = db_session.execute(
        text("SELECT document_id, content FROM knowledge_document_fts WHERE document_id = :id"),
        {"id": "doc-1"},
    ).all()

    assert rows == [("doc-1", "new admin handler")]


def test_backfill_populates_existing_documents(db_session) -> None:
    first = _add_doc(db_session, relative_path="src/auth.py", content="alpha login")
    second = _add_doc(db_session, relative_path="src/config.py", content="firebase config")
    db_session.execute(text("DELETE FROM knowledge_document_fts"))
    db_session.commit()

    inserted = backfill_knowledge_fts_if_empty(db_session)

    assert inserted == 2
    rows = db_session.execute(text("SELECT document_id FROM knowledge_document_fts")).scalars().all()
    assert set(rows) == {first.id, second.id}


def test_backfill_skips_when_already_populated(db_session) -> None:
    first = _add_doc(db_session, relative_path="src/auth.py", content="alpha login")
    second = _add_doc(db_session, relative_path="src/config.py", content="firebase config")
    _sync_fts(db_session, first)
    db_session.commit()

    inserted = backfill_knowledge_fts_if_empty(db_session)

    assert inserted == 0
    rows = db_session.execute(text("SELECT document_id FROM knowledge_document_fts")).scalars().all()
    assert rows == [first.id]
    assert second.id not in rows


def test_search_uses_fts5_when_enabled(db_session) -> None:
    first = _add_doc(db_session, relative_path="src/admin.py", content="admin login admin policy")
    second = _add_doc(db_session, relative_path="src/billing.py", content="invoice export")
    _sync_fts(db_session, first)
    _sync_fts(db_session, second)
    db_session.commit()
    service = _service(db_session)

    result = service.search_repositories(query="admin login", top_k=1)

    assert result.citations[0].relative_path == "src/admin.py"
    assert result.answer_trace.fts5_pool_size == 20
    assert result.answer_trace.fts5_match_count == 1
    assert result.answer_trace.fts5_query is not None


def test_search_falls_back_to_linear_scan_when_fts5_disabled(db_session) -> None:
    document = _add_doc(db_session, relative_path="src/auth.py", content="legacy login handler")
    db_session.commit()
    service = _service(db_session, knowledge_fts5_enabled=False)

    result = service.search_repositories(query="legacy login", top_k=1)

    assert result.citations[0].document_id == document.id
    assert result.answer_trace.fts5_pool_size is None
    assert result.answer_trace.fts5_match_count is None
    assert result.answer_trace.fts5_query is None


def test_search_respects_source_filter(db_session) -> None:
    first = _add_doc(db_session, source_name="repo", relative_path="src/auth.py", content="shared login")
    second = _add_doc(db_session, source_name="other", relative_path="src/auth.py", content="shared login")
    _sync_fts(db_session, first)
    _sync_fts(db_session, second)
    db_session.commit()
    service = _service(db_session, knowledge_source_specs=f"repo={BACKEND_ROOT};other={BACKEND_ROOT}")

    result = service.search_repositories(query="shared login", top_k=5, source_name="other")

    assert result.citations
    assert {citation.source_name for citation in result.citations} == {"other"}


def test_delete_document_removes_fts_row(db_session) -> None:
    document = _add_doc(db_session, relative_path="src/auth.py", content="delete login")
    _sync_fts(db_session, document)
    db_session.commit()
    service = _service(db_session)

    response = service.delete_document(document_id=document.id)

    assert response.removed_documents == 1
    count = db_session.execute(
        text("SELECT COUNT(*) FROM knowledge_document_fts WHERE document_id = :id"),
        {"id": document.id},
    ).scalar_one()
    assert count == 0


def test_fts5_pool_multiplier_setting_applied(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    document = _add_doc(db_session, relative_path="src/auth.py", content="pool login")
    db_session.commit()
    service = _service(db_session, knowledge_fts5_pool_multiplier=7)
    observed: dict[str, int] = {}

    def fake_topk(*, source_names: list[str], fts_query: str, pool_size: int) -> list[KnowledgeDocument]:
        observed["pool_size"] = pool_size
        return [document]

    monkeypatch.setattr(service, "_fts5_topk", fake_topk)

    result = service.search_repositories(query="pool login", top_k=4)

    assert observed["pool_size"] == 28
    assert result.answer_trace.fts5_pool_size == 28
    assert result.answer_trace.fts5_match_count == 1


def test_fts5_query_handles_empty_token_set(db_session) -> None:
    query = _build_fts5_query([], set())
    service = _service(db_session)

    result = service._fts5_topk(source_names=["repo"], fts_query=query, pool_size=20)

    assert query == '"unlikelytoken12345"'
    assert result == []
