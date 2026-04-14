from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.diff_repair import repair_diff  # noqa: E402


class DiffRepairTests(unittest.TestCase):
    def test_repair_wrong_hunk_counts(self) -> None:
        diff = """diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -1,2 +1,2 @@
 one
 two
 three
-old
+new
"""

        result = repair_diff(diff)

        self.assertIn("@@ -1,4 +1,4 @@", result.repaired_diff)
        self.assertIn("corrected hunk line counts", " ".join(result.repairs_applied))

    def test_repair_multiple_files(self) -> None:
        diff = """diff --git a/app/one.py b/app/one.py
--- a/app/one.py
+++ b/app/one.py
@@ -1,1 +1,1 @@
-old
+new
diff --git a/app/two.py b/app/two.py
--- a/app/two.py
+++ b/app/two.py
@@ -3,1 +3,1 @@
-before
+after
"""

        result = repair_diff(diff)

        self.assertEqual(result.file_count, 2)
        self.assertIn("@@ -1,1 +1,1 @@", result.repaired_diff)
        self.assertIn("@@ -3,1 +3,1 @@", result.repaired_diff)
        self.assertIn("\n\ndiff --git a/app/two.py b/app/two.py\n", result.repaired_diff)

    def test_repair_trailing_text_stripped(self) -> None:
        diff = """diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -1,1 +1,1 @@
-old
+new
Here is the explanation for the patch.
"""

        result = repair_diff(diff)

        self.assertNotIn("Here is the explanation", result.repaired_diff)
        self.assertIn("stripped trailing non-diff text", " ".join(result.repairs_applied))

    def test_repair_missing_separator(self) -> None:
        diff = """diff --git a/app/one.py b/app/one.py
--- a/app/one.py
+++ b/app/one.py
@@ -1,1 +1,1 @@
-old
+new
diff --git a/app/two.py b/app/two.py
--- a/app/two.py
+++ b/app/two.py
@@ -1,1 +1,1 @@
-before
+after
"""

        result = repair_diff(diff)

        self.assertIn("\n\ndiff --git a/app/two.py b/app/two.py\n", result.repaired_diff)
        self.assertIn("added blank separators", " ".join(result.repairs_applied))

    def test_repair_with_context_files_fixes_offset(self) -> None:
        diff = """diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -1,2 +1,2 @@
 beta
-gamma
+GAMMA
"""
        context_files = {"app/example.py": "alpha\nbeta\ngamma\ndelta\n"}

        result = repair_diff(diff, context_files=context_files)

        self.assertIn("@@ -2,2 +1,2 @@", result.repaired_diff)
        self.assertIn("corrected hunk start", " ".join(result.repairs_applied))

    def test_repair_clean_diff_unchanged(self) -> None:
        diff = """diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -1 +1,2 @@
 old
+new
"""

        result = repair_diff(diff)

        self.assertEqual(result.repaired_diff, diff)
        self.assertEqual(result.repairs_applied, [])
        self.assertEqual(result.file_count, 1)

    def test_repair_preserves_blank_context_lines(self) -> None:
        diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,5 +1,6 @@
 def main():
     x = 1
 
+    setup()
     y = 2
     return x + y
"""

        result = repair_diff(diff)

        self.assertEqual(result.repaired_diff, diff)
        self.assertEqual(result.repairs_applied, [])
        self.assertEqual(result.file_count, 1)
        self.assertIn("\n \n+    setup()\n", result.repaired_diff)

    def test_repair_multi_hunk_with_blank_lines(self) -> None:
        diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -7,7 +7,6 @@
 import os
 import sys
 import json
-import unused_module
 import re
 import logging
 import pathlib
@@ -20,6 +19,7 @@
 }
 
 def main():
+    setup()
     x = 1
     y = 2
     return x + y
"""

        result = repair_diff(diff)

        self.assertEqual(result.repaired_diff, diff)
        self.assertEqual(result.repairs_applied, [])
        self.assertEqual(result.file_count, 1)
        self.assertIn("@@ -7,7 +7,6 @@", result.repaired_diff)
        self.assertIn("@@ -20,6 +19,7 @@", result.repaired_diff)
        self.assertIn("\n }\n \n def main():", result.repaired_diff)


if __name__ == "__main__":
    unittest.main()
