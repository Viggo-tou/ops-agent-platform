from __future__ import annotations

from types import SimpleNamespace

from app.orchestrator.service import _semantic_review_discover_repair_files


def test_semantic_repair_scope_discovers_low_frequency_finding_files(tmp_path) -> None:
    root = tmp_path
    analytics = root / "app/src/main/java/com/example/handyman/CustomerAnalytics.kt"
    analytics.parent.mkdir(parents=True, exist_ok=True)
    analytics.write_text(
        """
        package com.example.handyman

        fun loadDashboard() {
            val path = "serviceAnalytics/2025"
            println(path)
        }
        """,
        encoding="utf-8",
    )
    generic = root / "app/src/main/java/com/example/handyman/GenericFragment.kt"
    generic.write_text(
        """
        package com.example.handyman

        fun saveData() {
            println("firebase user data fragment activity")
        }
        """,
        encoding="utf-8",
    )
    test_file = root / "app/src/test/java/com/example/handyman/AnalyticsTest.kt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("analytics should be ignored in tests", encoding="utf-8")

    files = _semantic_review_discover_repair_files(
        root,
        [
            SimpleNamespace(
                category="general",
                severity="high",
                evidence_quote="diff --git a/app/src/main/java/com/example/Foo.kt",
                description=(
                    "Dummy data in analytics charts not cleared; no changes "
                    "to any analytics chart component."
                ),
                suggested_fix=(
                    "Locate analytics code and replace dummy data with "
                    "real Firebase values."
                ),
            )
        ],
        existing_paths=["app/src/main/java/com/example/handyman/AlreadyChanged.kt"],
    )

    assert files == ["app/src/main/java/com/example/handyman/CustomerAnalytics.kt"]


def test_semantic_repair_scope_ignores_low_ungrounded_findings(tmp_path) -> None:
    root = tmp_path
    analytics = root / "app/src/main/java/com/example/handyman/CustomerAnalytics.kt"
    analytics.parent.mkdir(parents=True, exist_ok=True)
    analytics.write_text("fun load() = println(\"serviceAnalytics/2025\")", encoding="utf-8")

    files = _semantic_review_discover_repair_files(
        root,
        [
            SimpleNamespace(
                category="general",
                severity="low",
                evidence_quote="",
                description="No changes address dummy data in analytics charts.",
                suggested_fix="Locate analytics code and replace dummy data.",
            )
        ],
    )

    assert files == []
