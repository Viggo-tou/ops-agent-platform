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
