from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.services.knowledge import KnowledgeService, SourceSpec  # noqa: E402


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
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_default_route_prefers_indexed_js_extensions_without_android_reason(
    db_session,
) -> None:
    content = "export function renderDashboard() { return <main />; }\n"
    db_session.add(
        KnowledgeDocument(
            source_name="reactapp",
            relative_path="src/Dashboard.js",
            title="Dashboard.js",
            extension=".js",
            language="javascript",
            size_bytes=len(content.encode("utf-8")),
            line_count=1,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            metadata_json={},
            content=content,
        )
    )
    db_session.commit()

    route = KnowledgeService(db_session)._route_query(
        query="Where is renderDashboard implemented?",
        source_specs=[SourceSpec(name="reactapp", path=Path("reactapp"))],
    )

    assert route.kind == "code_debug"
    assert route.preferred_extensions == (".js",)
    assert "Kotlin" not in route.reason
    assert "Java" not in route.reason
