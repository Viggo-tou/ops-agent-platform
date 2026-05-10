"""Tests for acceptance_check — structural gate that uses the planner's
declared acceptance_tests to verify the diff actually does what the
plan promised.
"""
from __future__ import annotations

import pytest

from app.services.acceptance_check import (
    AcceptanceReport,
    AcceptanceTest,
    evaluate_acceptance,
)

# A minimal diff used across multiple tests.
_DIFF_ASTROPY = """\
diff --git a/astropy/nddata/mixins/ndarithmetic.py b/astropy/nddata/mixins/ndarithmetic.py
--- a/astropy/nddata/mixins/ndarithmetic.py
+++ b/astropy/nddata/mixins/ndarithmetic.py
@@ -120,6 +120,9 @@ class NDArithmeticMixin:
     def _arithmetic(self, op, other, **kwargs):
         mask = self.mask
+        # operand-without-mask branch
+        if mask is None:
+            return op(self.data, other.data)
         return op(self.data * mask, other.data * mask)

"""

_DIFF_ADD_IMPORT = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,4 @@
 from collections import deque
+from json import loads
 def f():
     pass

"""


def test_diff_contains_pattern_passes_when_in_added_lines():
    tests = [AcceptanceTest(kind="diff_contains_pattern", pattern="if mask is None")]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert report.passed
    assert report.results[0].matched
    assert report.results[0].reason


def test_diff_contains_pattern_fails_when_pattern_only_in_context():
    # `mask = self.mask` is a context line (no leading +), so it
    # should NOT count as "added".
    tests = [AcceptanceTest(kind="diff_contains_pattern", pattern="mask = self.mask")]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert not report.passed
    assert not report.results[0].matched


def test_diff_contains_pattern_in_file_scoped():
    tests = [
        AcceptanceTest(
            kind="diff_contains_pattern_in_file",
            pattern="if mask is None",
            file="astropy/nddata/mixins/ndarithmetic.py",
        )
    ]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert report.passed


def test_diff_contains_pattern_in_file_wrong_file_fails():
    tests = [
        AcceptanceTest(
            kind="diff_contains_pattern_in_file",
            pattern="if mask is None",
            file="some/other/file.py",
        )
    ]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert not report.passed


def test_function_signature_unchanged_passes_when_signature_kept():
    diff = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,4 @@
 def hello(name):
+    name = name.strip()
     return f"hi {name}"
"""
    tests = [
        AcceptanceTest(kind="function_signature_unchanged", function="hello")
    ]
    report = evaluate_acceptance(diff, tests)
    assert report.passed


def test_function_signature_unchanged_fails_when_signature_modified():
    diff = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,3 @@
-def hello(name):
+def hello(name, greeting="hi"):
     return f"hi {name}"
"""
    tests = [
        AcceptanceTest(kind="function_signature_unchanged", function="hello")
    ]
    report = evaluate_acceptance(diff, tests)
    assert not report.passed


def test_function_signature_changed_passes_when_actually_changed():
    diff = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,3 @@
-def hello(name):
+def hello(name, greeting="hi"):
     return f"{greeting} {name}"
"""
    tests = [
        AcceptanceTest(kind="function_signature_changed", function="hello")
    ]
    report = evaluate_acceptance(diff, tests)
    assert report.passed


def test_function_signature_changed_fails_when_signature_kept():
    diff = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,4 @@
 def hello(name):
+    name = name.strip()
     return f"hi {name}"
"""
    tests = [
        AcceptanceTest(kind="function_signature_changed", function="hello")
    ]
    report = evaluate_acceptance(diff, tests)
    assert not report.passed


def test_no_new_file_outside_passes_when_only_inside_scope():
    diff = """\
diff --git a/astropy/nddata/foo.py b/astropy/nddata/foo.py
new file mode 100644
--- /dev/null
+++ b/astropy/nddata/foo.py
@@ -0,0 +1 @@
+content
"""
    tests = [
        AcceptanceTest(kind="no_new_file_outside", scope="astropy/nddata/")
    ]
    report = evaluate_acceptance(diff, tests)
    assert report.passed


def test_no_new_file_outside_fails_when_outside_scope():
    diff = """\
diff --git a/foo.py b/foo.py
new file mode 100644
--- /dev/null
+++ b/foo.py
@@ -0,0 +1 @@
+content
"""
    tests = [
        AcceptanceTest(kind="no_new_file_outside", scope="astropy/nddata/")
    ]
    report = evaluate_acceptance(diff, tests)
    assert not report.passed


def test_import_added_passes():
    tests = [AcceptanceTest(kind="import_added", pattern="from json import loads")]
    report = evaluate_acceptance(_DIFF_ADD_IMPORT, tests)
    assert report.passed


def test_import_added_fails_when_unrelated():
    tests = [AcceptanceTest(kind="import_added", pattern="from os import path")]
    report = evaluate_acceptance(_DIFF_ADD_IMPORT, tests)
    assert not report.passed


def test_multiple_tests_all_evaluated_independently():
    tests = [
        AcceptanceTest(kind="diff_contains_pattern", pattern="if mask is None"),
        AcceptanceTest(kind="diff_contains_pattern", pattern="totally not present"),
    ]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert not report.passed  # one failure → overall fail
    assert report.results[0].matched
    assert not report.results[1].matched


def test_unknown_kind_recorded_as_skipped_not_pass():
    tests = [AcceptanceTest(kind="some_unknown_kind", pattern="anything")]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    # An unknown kind should not silently pass; we surface it for the
    # planner to fix its plan.
    assert not report.passed
    assert "unknown" in report.results[0].reason.lower()


def test_empty_test_list_passes_trivially():
    report = evaluate_acceptance(_DIFF_ASTROPY, [])
    assert report.passed
    assert report.results == []


# --- forbids_pattern_in_diff (Class B counter-measure) ----------------------


_DIFF_HALLUCINATED_FLAG = """\
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -150,6 +150,8 @@ LANGUAGES_BIDI = ["he", "ar", "fa", "ur"]
+# Bypass for the SUBQUERY GROUP BY thing
+SUBQUERY_GROUP_BY_PRESERVE = True
+_ = SUBQUERY_GROUP_BY_PRESERVE
 USE_I18N = True
"""


def test_forbids_pattern_catches_hallucinated_settings_flag():
    tests = [
        AcceptanceTest(
            kind="forbids_pattern_in_diff",
            pattern=r"^[A-Z_]+ = (True|False)$",
            rationale="ORM/query bug; new boolean flag is not a valid fix",
        )
    ]
    report = evaluate_acceptance(_DIFF_HALLUCINATED_FLAG, tests)
    assert not report.passed
    assert "forbidden pattern" in report.results[0].reason.lower()


def test_forbids_pattern_passes_when_pattern_absent():
    tests = [
        AcceptanceTest(
            kind="forbids_pattern_in_diff",
            pattern=r"^[A-Z_]+ = (True|False)$",
        )
    ]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert report.passed


def test_forbids_pattern_scoped_to_file():
    """File-scoped forbid: only checks added lines in the named file."""
    tests = [
        AcceptanceTest(
            kind="forbids_pattern_in_diff",
            pattern=r"^[A-Z_]+ = True$",
            file="django/conf/other.py",
        )
    ]
    # Pattern would match in global_settings.py but file is "other.py" → pass.
    report = evaluate_acceptance(_DIFF_HALLUCINATED_FLAG, tests)
    assert report.passed


def test_forbids_pattern_invalid_regex_fails_safely():
    tests = [
        AcceptanceTest(
            kind="forbids_pattern_in_diff",
            pattern=r"[",  # invalid regex
        )
    ]
    report = evaluate_acceptance(_DIFF_HALLUCINATED_FLAG, tests)
    assert not report.passed
    assert "did not compile" in report.results[0].reason.lower()


# --- test_must_reference_existing_symbol (Class E counter-measure) ----------


_DIFF_SELF_JUSTIFYING_TEST = """\
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -150,6 +150,7 @@ LANGUAGES_BIDI = ["he", "ar", "fa", "ur"]
+SUBQUERY_GROUP_BY_PRESERVE = True
diff --git a/tests/test_subquery_flag.py b/tests/test_subquery_flag.py
new file mode 100644
--- /dev/null
+++ b/tests/test_subquery_flag.py
@@ -0,0 +1,5 @@
+from django.conf import settings
+
+
+def test_flag_exists():
+    assert settings.SUBQUERY_GROUP_BY_PRESERVE is True
"""


def test_self_justifying_test_caught():
    """The new test only references SUBQUERY_GROUP_BY_PRESERVE — which
    DOES exist in the fix code. So actually this self-justifying case
    is one the symbol-reference check WOULD pass on. The check catches
    a stricter pathology: tests referencing names absent from the fix."""
    tests = [
        AcceptanceTest(
            kind="test_must_reference_existing_symbol",
            scope="tests/",
        )
    ]
    report = evaluate_acceptance(_DIFF_SELF_JUSTIFYING_TEST, tests)
    # SUBQUERY_GROUP_BY_PRESERVE appears both in the fix and the test
    # → the structural check passes. The forbids_pattern test (above)
    # is the one that catches this hallucination class.
    assert report.passed


_DIFF_TEST_WITH_FAKE_SYMBOL = """\
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -200,6 +200,7 @@ class Field:
+        existing_value = self.value_for_db()
diff --git a/tests/test_invented.py b/tests/test_invented.py
new file mode 100644
--- /dev/null
+++ b/tests/test_invented.py
@@ -0,0 +1,4 @@
+def test_my_helper_works():
+    from django.utils.fictitious import HelperThatDoesNotExist
+    assert HelperThatDoesNotExist().run() is True
"""


def test_test_referencing_nothing_in_fix_caught():
    tests = [
        AcceptanceTest(
            kind="test_must_reference_existing_symbol",
            scope="tests/",
        )
    ]
    report = evaluate_acceptance(_DIFF_TEST_WITH_FAKE_SYMBOL, tests)
    assert not report.passed
    assert "tests/test_invented.py" in report.results[0].reason


def test_test_must_reference_no_new_test_files_passes():
    """If the diff doesn't add any test files, the gate is a no-op pass."""
    tests = [
        AcceptanceTest(
            kind="test_must_reference_existing_symbol", scope="tests/"
        )
    ]
    report = evaluate_acceptance(_DIFF_ASTROPY, tests)
    assert report.passed
