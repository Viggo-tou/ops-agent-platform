"""Plan A: evidence_bundle anchor matching via FTS5.

Validates the new FTS5-backed anchor matcher in
``app.services.evidence_bundle._find_files_via_fts5`` and the
``build_evidence_bundle`` integration:

  * tokenization splits CamelCase + drops stopwords
  * AND query is preferred for precision; OR fallback boosts recall
  * substring fallback fires when FTS5 returns nothing
  * anchor_strategy telemetry records which strategy hit each anchor
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: F401, E402
from app.models.base import Base  # noqa: E402
from app.services.evidence_bundle import (  # noqa: E402
    _find_files_via_fts5,
    _tokenize_for_fts5,
    build_evidence_bundle,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        evidence_must_touch_excluded_extensions="",
        evidence_must_touch_excluded_path_segments="",
        evidence_must_touch_excluded_filenames="",
        evidence_must_touch_include_configs=True,
    )


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    # Create the FTS5 virtual table mirroring the real production schema.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE knowledge_document_fts USING fts5("
            "document_id UNINDEXED, source_name UNINDEXED, "
            "relative_path, title, content, card_text, "
            "tokenize = 'porter unicode61 remove_diacritics 2')"
        ))
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_doc(db: Session, *, doc_id: str, source: str, rel_path: str, content: str) -> None:
    """Insert a knowledge_document row + matching FTS5 row."""
    db.execute(
        text(
            "INSERT INTO knowledge_document "
            "(id, source_name, relative_path, title, extension, language, "
            " size_bytes, line_count, content_hash, content, created_at, updated_at) "
            "VALUES (:id,:sn,:rp,:t,:ext,:lang,:sz,:lc,:ch,:c,"
            "        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "id": doc_id, "sn": source, "rp": rel_path, "t": rel_path,
            "ext": Path(rel_path).suffix.lstrip("."), "lang": "kotlin",
            "sz": len(content.encode("utf-8")), "lc": content.count("\n") + 1,
            "ch": "x" * 16, "c": content,
        },
    )
    # FTS5 rowid must equal the document.id rowid for the JOIN to work.
    rowid = db.execute(
        text("SELECT rowid FROM knowledge_document WHERE id=:id"),
        {"id": doc_id},
    ).scalar()
    db.execute(
        text(
            "INSERT INTO knowledge_document_fts "
            "(rowid, document_id, source_name, relative_path, title, content, card_text) "
            "VALUES (:rid,:did,:sn,:rp,:t,:c,:ct)"
        ),
        {"rid": rowid, "did": doc_id, "sn": source, "rp": rel_path,
         "t": rel_path, "c": content, "ct": ""},
    )
    db.flush()


# --- Tokenizer ---------------------------------------------------------------

def test_tokenize_splits_camel_case():
    assert _tokenize_for_fts5("homeAddress") == ["home", "address"]
    # "flow" is intentionally a stopword; pick a phrase whose tokens are
    # all kept so the camel-case split itself is what we're verifying.
    assert _tokenize_for_fts5("JobPostingService") == ["job", "posting", "service"]


def test_tokenize_drops_known_filler_word_flow():
    # "flow" is in _FTS5_STOPWORDS (UI filler) — must be dropped.
    assert "flow" not in _tokenize_for_fts5("JobPostingFlow")


def test_tokenize_drops_stopwords():
    # "the" and "of" are stopwords; "view" is too (UI filler word).
    tokens = _tokenize_for_fts5("the home address of the user")
    assert "the" not in tokens
    assert "of" not in tokens
    assert "home" in tokens and "address" in tokens and "user" in tokens


def test_tokenize_drops_short_words():
    tokens = _tokenize_for_fts5("a b cc home")
    # "a" len=1 dropped, "b" len=1 dropped, "cc" len=2 kept (but is "cc" a stopword? no).
    assert "a" not in tokens
    assert "b" not in tokens
    assert "cc" in tokens
    assert "home" in tokens


def test_tokenize_returns_empty_for_pure_stopwords():
    assert _tokenize_for_fts5("the of and") == []


# --- FTS5 search -------------------------------------------------------------

def test_fts5_and_query_finds_doc_with_both_tokens(db_session: Session):
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/UserProfile.kt",
              content="fun saveHomeAddress() { /* persists user home address */ }")
    hits, strategy = _find_files_via_fts5(db_session, "myapp", "home address")
    assert "src/UserProfile.kt" in hits
    assert strategy == "fts5_and"


def test_fts5_or_query_falls_through_when_and_fails(db_session: Session):
    # Doc has 'home' but not 'address' — AND should fail, OR should still hit.
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/Greeter.kt",
              content="fun goHome() { println(\"home\") }")
    hits, strategy = _find_files_via_fts5(db_session, "myapp", "home address")
    assert "src/Greeter.kt" in hits
    assert strategy == "fts5_or"


def test_fts5_returns_empty_when_no_hits(db_session: Session):
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/Unrelated.kt",
              content="fun launch() {}")
    hits, strategy = _find_files_via_fts5(db_session, "myapp", "homeAddress")
    assert hits == {}
    assert strategy == ""


def test_fts5_filters_by_source_name(db_session: Session):
    _seed_doc(db_session, doc_id="d1", source="appA",
              rel_path="src/A.kt", content="fun homeAddress() {}")
    _seed_doc(db_session, doc_id="d2", source="appB",
              rel_path="src/B.kt", content="fun homeAddress() {}")
    hits_a, _ = _find_files_via_fts5(db_session, "appA", "homeAddress")
    hits_b, _ = _find_files_via_fts5(db_session, "appB", "homeAddress")
    assert "src/A.kt" in hits_a and "src/B.kt" not in hits_a
    assert "src/B.kt" in hits_b and "src/A.kt" not in hits_b


def test_fts5_camel_case_anchor_tokenized(db_session: Session):
    """JobPostingFlow as anchor → split into job/posting/flow → AND match."""
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/Job.kt",
              content="class Job { fun postingFlow() {} }")
    hits, strategy = _find_files_via_fts5(db_session, "myapp", "JobPostingFlow")
    assert "src/Job.kt" in hits
    assert strategy in ("fts5_and", "fts5_or")


# --- build_evidence_bundle integration --------------------------------------

def test_build_uses_fts5_when_db_provided(db_session: Session, tmp_path: Path):
    """When db+source_name are supplied, FTS5 wins over substring scan."""
    # Seed FTS5 with file that does NOT physically exist on disk —
    # FTS5 hit should drive the bundle even if filesystem scan would fail.
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/HomeScreen.kt",
              content="val homeAddr = profile.homeAddress")
    bundle = build_evidence_bundle(
        request_text="Pre-fill the 'homeAddress' field on signup",
        normalized_request=None,
        source_tree=tmp_path,  # empty — substring scan would find nothing
        grounding_terms=["homeAddress"],
        planner_must_touch=[],
        has_destructive_verb=False,
        settings=_settings(),
        db=db_session,
        source_name="myapp",
    )
    assert "homeAddress" in bundle.anchor_hits
    # anchor_strategy must show this came from FTS5 (and/or), not substring
    assert bundle.anchor_strategy["homeAddress"].startswith("fts5_")
    assert any("HomeScreen.kt" in p for p in bundle.candidate_files)


def test_build_falls_back_to_substring_when_no_fts5_match(
    db_session: Session, tmp_path: Path
):
    """When FTS5 has no docs at all, substring scan must still work."""
    real_file = tmp_path / "src" / "Foo.kt"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("fun homeAddress() = ''", encoding="utf-8")

    bundle = build_evidence_bundle(
        request_text="check homeAddress wiring",
        normalized_request=None,
        source_tree=tmp_path,
        grounding_terms=["homeAddress"],
        planner_must_touch=[],
        has_destructive_verb=False,
        settings=_settings(),
        db=db_session,
        source_name="myapp",
    )
    assert bundle.anchor_strategy.get("homeAddress") == "substring"
    assert any("Foo.kt" in p for p in bundle.candidate_files)


def test_build_works_without_db(tmp_path: Path):
    """Legacy callers (no db) must still get substring-scan behavior."""
    (tmp_path / "x.py").write_text("home_address = 1", encoding="utf-8")
    bundle = build_evidence_bundle(
        request_text="touch home_address",
        normalized_request=None,
        source_tree=tmp_path,
        grounding_terms=["home_address"],
        planner_must_touch=[],
        has_destructive_verb=False,
        settings=_settings(),
    )
    assert "x.py" in bundle.candidate_files
    assert bundle.anchor_strategy.get("home_address") == "substring"


def test_build_payload_contains_anchor_strategy(
    db_session: Session, tmp_path: Path
):
    _seed_doc(db_session, doc_id="d1", source="myapp",
              rel_path="src/A.kt", content="val homeAddress = ''")
    bundle = build_evidence_bundle(
        request_text="touch homeAddress",
        normalized_request=None,
        source_tree=tmp_path,
        grounding_terms=["homeAddress"],
        planner_must_touch=[],
        has_destructive_verb=False,
        settings=_settings(),
        db=db_session,
        source_name="myapp",
    )
    payload = bundle.to_payload()
    assert "anchor_strategy" in payload
    assert payload["anchor_strategy"]["homeAddress"].startswith("fts5_")
