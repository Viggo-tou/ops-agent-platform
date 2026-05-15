from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.models.base import Base
from app.models.knowledge_document import KnowledgeDocument
from app.agents.service import _fallback_plan_candidate_source_paths
from app.services.preplan_discover import preplan_discover_files


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, future=True)()


def _doc(path: str, content: str) -> KnowledgeDocument:
    return KnowledgeDocument(
        source_name="handymanapp",
        relative_path=path,
        title=path.rsplit("/", 1)[-1],
        extension="." + path.rsplit(".", 1)[-1],
        language="kotlin",
        size_bytes=len(content),
        line_count=content.count("\n") + 1,
        content_hash=path,
        metadata_json={},
        content=content,
    )


def test_preplan_discovery_prioritizes_low_frequency_issue_terms() -> None:
    db = _db()
    try:
        for idx in range(8):
            db.add(
                _doc(
                    f"app/src/main/java/com/example/handyman/Generic{idx}Fragment.kt",
                    "firebase user data logged out fragment activity\n" * 4,
                )
            )
        db.add(
            _doc(
                "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
                'incrementMetric("serviceAnalytics/2025/$year/$month/newCustomers")',
            )
        )
        db.commit()

        rows = preplan_discover_files(
            issue_text=(
                "Hardcoded username, dummy data in analytics charts, "
                "and previous logged-in user cache must be cleaned up."
            ),
            source_name="handymanapp",
            db=db,
            top_n=3,
        )

        assert rows
        assert rows[0].path == (
            "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt"
        )
    finally:
        db.close()


def test_fallback_targets_keep_lower_scored_new_surface_terms() -> None:
    paths = _fallback_plan_candidate_source_paths(
        [
            {
                "path": "app/src/main/java/com/example/handyman/HandymanJobBoardFragment.kt",
                "score": 25.0,
                "matched_terms": ["username", "data", "logged"],
            },
            {
                "path": "app/src/main/java/com/example/handyman/ServiceCategoryFragment.kt",
                "score": 24.0,
                "matched_terms": ["username", "data", "logged"],
            },
            {
                "path": "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
                "score": 12.0,
                "matched_terms": ["analytics", "data"],
            },
        ],
        limit=3,
    )

    assert "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt" in paths
