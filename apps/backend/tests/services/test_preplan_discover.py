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


def _source_doc(source_name: str, path: str, content: str) -> KnowledgeDocument:
    doc = _doc(path, content)
    doc.source_name = source_name
    return doc


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


def test_fallback_targets_dashboard_cleanup_by_intent_groups() -> None:
    paths = _fallback_plan_candidate_source_paths(
        [
            {"path": "src/pages/ServiceAnalytics.js", "score": 29.0, "matched_terms": ["analytics", "dummy", "charts", "currentuser"]},
            {"path": "src/data/mockUsers.js", "score": 28.0, "matched_terms": ["mock", "admin", "staff", "master"]},
            {"path": "src/pages/AdminSettings.js", "score": 27.0, "matched_terms": ["admin", "staff", "roles", "currentuser"]},
            {"path": "src/context/UserContext.js", "score": 26.0, "matched_terms": ["session_owner", "currentuser", "localstorage"]},
            {"path": "src/pages/UserVerification.js", "score": 25.0, "matched_terms": ["currentuser", "localstorage", "previous"]},
            {"path": "src/pages/HandymanVerification.js", "score": 24.0, "matched_terms": ["currentuser", "localstorage", "logged"]},
            {"path": "src/pages/Dashboard.js", "score": 20.0, "matched_terms": ["dashboard", "currentuser", "localstorage"]},
        ],
        issue_text=(
            'Hardcoded username "Minij", dummy data in analytics charts, '
            'previous logged-in user cache, roles simplified to "Admin" '
            'and "Staff", remove "master admin".'
        ),
    )

    assert paths == [
        "src/pages/AdminSettings.js",
        "src/data/mockUsers.js",
        "src/pages/ServiceAnalytics.js",
        "src/context/UserContext.js",
        "src/pages/Dashboard.js",
    ]


def test_preplan_discovery_prioritizes_quoted_dashboard_role_anchors() -> None:
    db = _db()
    try:
        db.add_all(
            [
                _source_doc(
                    "hosteddashboard",
                    "src/pages/AdminSettings.js",
                    'const roleOptions = ["Master Admin", "Staff Member"];',
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/data/mockUsers.js",
                    'role: "master_admin", firstName: "Master", lastName: "Admin"',
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/pages/ServiceAnalytics.js",
                    "analytics charts mock dummy ratingDistribution data",
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/pages/JobManagement.js",
                    "generic dummy job data",
                ),
            ]
        )
        db.commit()

        rows = preplan_discover_files(
            issue_text=(
                'Hardcoded username "Minij", dummy data in analytics charts, '
                'roles simplified to "Admin" and "Staff", remove "master admin".'
            ),
            source_name="hosteddashboard",
            db=db,
            top_n=3,
        )

        paths = [row.path for row in rows]
        assert "src/pages/AdminSettings.js" in paths
        assert "src/data/mockUsers.js" in paths
        assert "src/pages/ServiceAnalytics.js" in paths
    finally:
        db.close()


def test_preplan_discovery_prefers_react_session_owner_for_cache_issues() -> None:
    db = _db()
    try:
        db.add_all(
            [
                _source_doc(
                    "hosteddashboard",
                    "src/pages/UserVerification.js",
                    'const currentUser = JSON.parse(localStorage.getItem("currentUser"));',
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/pages/HandymanVerification.js",
                    'const currentUser = JSON.parse(localStorage.getItem("currentUser"));',
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/context/UserContext.js",
                    (
                        "const UserContext = createContext();\n"
                        "export const UserProvider = ({ children }) => {\n"
                        "const [currentUser, setCurrentUser] = useState(null);\n"
                        "const storedUser = localStorage.getItem(\"currentUser\");\n"
                        "}\n"
                    ),
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/pages/Dashboard.js",
                    'const currentUser = JSON.parse(localStorage.getItem("currentUser"));',
                ),
                _source_doc(
                    "hosteddashboard",
                    "src/pages/Login.js",
                    'setCurrentUser(user); localStorage.setItem("currentUser", JSON.stringify(user));',
                ),
            ]
        )
        db.commit()

        rows = preplan_discover_files(
            issue_text=(
                "Fix caching issue where the dashboard shows the previous "
                "logged-in user after login."
            ),
            source_name="hosteddashboard",
            db=db,
            top_n=3,
        )

        paths = [row.path for row in rows]
        assert paths[0] == "src/context/UserContext.js"
        assert "src/pages/Dashboard.js" in paths
        assert "src/pages/UserVerification.js" not in paths
    finally:
        db.close()
