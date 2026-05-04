"""Integration test for KB retrieval cache hook in KnowledgeService.

Verifies acceptance criteria from T-KB-RETRIEVAL-CACHE.md §Acceptance:
  3a) Same query twice → 2nd run hits cache, emits KNOWLEDGE_CACHE_HIT, no
      uncached call on 2nd invocation.
  3b) sync_repositories invalidates that source's cache rows.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.enums import EventType  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.knowledge_retrieval_cache import KnowledgeRetrievalCache  # noqa: E402
from app.schemas.knowledge import (  # noqa: E402
    KnowledgeAnswerTrace,
    KnowledgeCitation,
    KnowledgeSearchResult,
)


def _fixture_result(query: str, source: str) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        query=query,
        answer=f"answer for {query}",
        citations=[
            KnowledgeCitation(
                document_id="doc-1",
                source_name=source,
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
            source_name=source,
            source_path="source",
            selected_sources=[source],
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
            answer_provider="minimax",
        ),
        packaged_context=f"[{source}:src/auth.py:1-2]\nlogin failure",
    )


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def settings():
    return SimpleNamespace(
        knowledge_retrieval_cache_enabled=True,
        knowledge_retrieval_cache_ttl_seconds=3600,
        knowledge_retrieval_cache_max_entries=1000,
        knowledge_source_name="testsource",
    )


def _make_service(db_session, settings):
    """Construct KnowledgeService with FTS + ensure stubbed."""
    from app.services import knowledge as knowledge_module

    with patch.object(knowledge_module, "create_knowledge_fts_table"), \
         patch.object(knowledge_module, "backfill_knowledge_fts_if_empty"), \
         patch.object(knowledge_module, "get_settings", return_value=settings):
        service = knowledge_module.KnowledgeService(db_session)
    return service


def test_repeat_query_hits_cache_and_skips_uncached(db_session, settings):
    """2nd identical query returns cached value + emits KNOWLEDGE_CACHE_HIT."""
    service = _make_service(db_session, settings)
    expected = _fixture_result("login failure", "testsource")

    uncached = MagicMock(return_value=expected)
    service._search_repositories_uncached = uncached

    # First call — uncached path runs, populates cache.
    r1 = service.search_repositories(query="login failure", source_name="testsource")
    assert uncached.call_count == 1
    assert r1.answer == expected.answer

    # Second call — same query/source → cache hit, uncached NOT called again.
    r2 = service.search_repositories(query="login failure", source_name="testsource")
    assert uncached.call_count == 1, "cache hit should skip uncached call"
    assert r2.answer == expected.answer

    # Cache hit event recorded.
    hit_events = (
        db_session.query(Event)
        .filter(Event.event_type == EventType.KNOWLEDGE_CACHE_HIT.value)
        .all()
    )
    assert len(hit_events) >= 1, "expected at least one KNOWLEDGE_CACHE_HIT event"


def test_query_normalization_hits_same_cache_slot(db_session, settings):
    """Whitespace + case variants share the same cache row."""
    service = _make_service(db_session, settings)
    expected = _fixture_result("login failure", "testsource")
    uncached = MagicMock(return_value=expected)
    service._search_repositories_uncached = uncached

    service.search_repositories(query="login failure", source_name="testsource")
    # Variant with extra whitespace + uppercase — should hit the same slot.
    service.search_repositories(query="  LOGIN   FAILURE  ", source_name="testsource")

    assert uncached.call_count == 1, "normalized query should hit existing cache slot"


def test_cache_disabled_skips_cache_layer(db_session, settings):
    """When flag off, every call goes through uncached, no cache rows written."""
    settings.knowledge_retrieval_cache_enabled = False
    service = _make_service(db_session, settings)
    expected = _fixture_result("disabled q", "testsource")
    uncached = MagicMock(return_value=expected)
    service._search_repositories_uncached = uncached

    service.search_repositories(query="disabled q", source_name="testsource")
    service.search_repositories(query="disabled q", source_name="testsource")

    assert uncached.call_count == 2, "cache disabled — both calls must be uncached"
    rows = db_session.query(KnowledgeRetrievalCache).count()
    assert rows == 0, "cache disabled — no rows should be persisted"


def test_sync_repositories_invalidates_cache_for_synced_source(db_session, settings):
    """sync_repositories must drop cache rows for the synced source."""
    service = _make_service(db_session, settings)

    # Pre-populate cache directly via the cache helper.
    service._retrieval_cache.put("q1", "alpha", {"answer": "a"})
    service._retrieval_cache.put("q2", "alpha", {"answer": "a2"})
    service._retrieval_cache.put("q3", "beta", {"answer": "b"})
    assert db_session.query(KnowledgeRetrievalCache).count() == 3

    # Stub source-resolution + per-source sync so sync_repositories runs cleanly.
    alpha_spec = SimpleNamespace(name="alpha", path="/tmp/alpha")
    service._resolve_source_specs = MagicMock(return_value=[alpha_spec])
    service._sync_single_repository = MagicMock(return_value=(0, 0, 0))

    service.sync_repositories(source_name="alpha")

    remaining = db_session.query(KnowledgeRetrievalCache).all()
    remaining_sources = {r.query_hash_inputs for r in remaining}
    # alpha rows should be gone; beta row should remain.
    assert all("source=alpha" not in s for s in remaining_sources)
    assert any("source=beta" in s for s in remaining_sources)
    assert len(remaining) == 1
