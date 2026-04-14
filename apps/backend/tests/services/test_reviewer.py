from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.reviewer import DiffReviewer, ReviewContext  # noqa: E402


def _diff_for(path: str, added_line: str = "new line") -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 old line
+{added_line}
"""


class DiffReviewerTests(unittest.TestCase):
    def test_clean_diff_passes(self) -> None:
        result = DiffReviewer().review(
            ReviewContext(
                diff=_diff_for("app/main.py"),
                test_result={"overall_passed": True},
            )
        )

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.violations, [])
        self.assertEqual(result.rules_checked, 4)

    def test_secret_pattern_blocks(self) -> None:
        result = DiffReviewer().review(
            ReviewContext(
                diff=_diff_for("app/settings.py", 'API_KEY = "sk-abc123"'),
            )
        )

        self.assertEqual(result.verdict, "block")
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0].rule_name, "no-secrets")

    def test_variable_assignment_not_blocked(self) -> None:
        """Variable assignments (not hardcoded secrets) should not trigger no-secrets."""
        result = DiffReviewer().review(
            ReviewContext(
                diff=_diff_for("app/auth.py", "token = request.headers.get('Authorization')"),
            )
        )

        self.assertEqual(result.verdict, "pass")

    def test_protected_path_blocks(self) -> None:
        result = DiffReviewer().review(ReviewContext(diff=_diff_for("migrations/0001_init.py")))

        self.assertEqual(result.verdict, "block")
        self.assertEqual(result.violations[0].rule_name, "protected-paths")

    def test_failing_tests_block(self) -> None:
        result = DiffReviewer().review(
            ReviewContext(
                diff=_diff_for("app/main.py"),
                test_result={"overall_passed": False},
            )
        )

        self.assertEqual(result.verdict, "block")
        self.assertEqual(result.violations[0].rule_name, "tests-must-pass")

    def test_max_diff_size_blocks(self) -> None:
        result = DiffReviewer(max_diff_size=100).review(
            ReviewContext(diff=_diff_for("app/large.py", "x" * 200))
        )

        self.assertEqual(result.verdict, "block")
        self.assertEqual(result.violations[0].rule_name, "max-diff-size")

    def test_parse_changed_files(self) -> None:
        diff = """diff --git a/apps/backend/app/foo.py b/apps/backend/app/foo.py
--- a/apps/backend/app/foo.py
+++ b/apps/backend/app/foo.py
@@ -1 +1,2 @@
 old
+new
diff --git a/apps/web/src/new.tsx b/apps/web/src/new.tsx
--- /dev/null
+++ b/apps/web/src/new.tsx
@@ -0,0 +1 @@
+new
diff --git a/docs/old.md b/docs/old.md
--- a/docs/old.md
+++ /dev/null
@@ -1 +0,0 @@
-old
"""

        changed_files = DiffReviewer.parse_changed_files(diff)

        self.assertEqual(
            changed_files,
            [
                "apps/backend/app/foo.py",
                "apps/web/src/new.tsx",
                "docs/old.md",
            ],
        )

    def test_no_test_result_skips_test_rule(self) -> None:
        result = DiffReviewer().review(ReviewContext(diff=_diff_for("app/main.py")))

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.rules_checked, 3)

    def test_custom_protected_paths(self) -> None:
        reviewer = DiffReviewer(protected_paths=["**/custom/**"])

        custom_result = reviewer.review(ReviewContext(diff=_diff_for("custom/foo.py")))
        migration_result = reviewer.review(ReviewContext(diff=_diff_for("migrations/0001.py")))

        self.assertEqual(custom_result.verdict, "block")
        self.assertEqual(custom_result.violations[0].rule_name, "protected-paths")
        self.assertEqual(migration_result.verdict, "pass")


if __name__ == "__main__":
    unittest.main()
