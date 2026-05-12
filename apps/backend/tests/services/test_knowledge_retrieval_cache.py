"""Unit tests for RetrievalCache (Stage 28 — T-KB-RETRIEVAL-CACHE)."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.knowledge_retrieval_cache import KnowledgeRetrievalCache
from app.services.knowledge_retrieval_cache import RetrievalCache


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[KnowledgeRetrievalCache.__table__])
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def settings():
    return SimpleNamespace(
        knowledge_retrieval_cache_enabled=True,
        knowledge_retrieval_cache_ttl_seconds=3600,
        knowledge_retrieval_cache_max_entries=1000,
    )


def test_cache_miss_returns_none(db_session, settings):
    cache = RetrievalCache(db_session, settings)
    assert cache.get("hello world", "src") is None


def test_cache_put_then_get_returns_value(db_session, settings):
    cache = RetrievalCache(db_session, settings)
    payload = {"answer": "42", "citations": []}
    cache.put("what is the answer", "src", payload)
    got = cache.get("what is the answer", "src")
    assert got == payload


def test_cache_get_after_ttl_returns_none(db_session, settings):
    cache = RetrievalCache(db_session, settings)
    cache.put("ephemeral", "src", {"x": 1}, ttl=1)
    time.sleep(1.2)
    assert cache.get("ephemeral", "src") is None


def test_cache_invalidate_source_removes_entries(db_session, settings):
    cache = RetrievalCache(db_session, settings)
    cache.put("q1", "alpha", {"v": 1})
    cache.put("q2", "alpha", {"v": 2})
    cache.put("q3", "beta", {"v": 3})
    deleted = cache.invalidate_source("alpha")
    assert deleted == 2
    assert cache.get("q1", "alpha") is None
    assert cache.get("q3", "beta") is not None


def test_cache_disabled_via_env_flag(db_session, settings):
    """When disabled, put still works (caller-side guard) but get on absent key is None."""
    settings.knowledge_retrieval_cache_enabled = False
    cache = RetrievalCache(db_session, settings)
    assert cache.get("anything", "src") is None  # missing → None even when "enabled"


def test_cache_key_normalization(db_session, settings):
    cache = RetrievalCache(db_session, settings)
    cache.put("Hello World", "Src", {"v": "ok"})
    # case + whitespace variants should hit the same slot
    assert cache.get("hello world", "src") == {"v": "ok"}
    assert cache.get("  HELLO   WORLD  ", "SRC") == {"v": "ok"}
