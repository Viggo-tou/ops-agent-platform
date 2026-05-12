"""Tests for PatchBudget structural gate.

These tests pin the contract: given a unified diff, count what the
patch does to the workspace and reject when it exceeds budget. No LLM
involvement.
"""
from __future__ import annotations

import pytest

from app.services.patch_budget import (
    PatchBudget,
    PatchBudgetReport,
    evaluate_patch_budget,
)


# Helper: a minimal valid unified diff editing one Python file.
_MINIMAL_DIFF = """\
diff --git a/app/foo.py b/app/foo.py
--- a/app/foo.py
+++ b/app/foo.py
@@ -1,3 +1,4 @@
 def f():
     pass
+    return 0

"""


def test_empty_diff_passes():
    report = evaluate_patch_budget("", PatchBudget())
    assert report.passed
    assert report.violations == []
    assert report.metrics["files_changed"] == 0


def test_minimal_diff_within_budget():
    report = evaluate_patch_budget(_MINIMAL_DIFF, PatchBudget())
    assert report.passed
    assert report.metrics["files_changed"] == 1
    assert report.metrics["added_lines"] == 1
    assert report.metrics["removed_lines"] == 0


def test_max_files_changed_violation():
    diff_chunks = []
    for i in range(5):
        diff_chunks.append(
            f"diff --git a/file{i}.py b/file{i}.py\n"
            f"--- a/file{i}.py\n"
            f"+++ b/file{i}.py\n"
            "@@ -1 +1,2 @@\n"
            " original\n"
            "+added\n"
        )
    diff = "".join(diff_chunks)
    report = evaluate_patch_budget(diff, PatchBudget(max_files_changed=3))
    assert not report.passed
    assert any("files_changed" in v for v in report.violations)
    assert report.metrics["files_changed"] == 5


def test_max_added_lines_violation():
    additions = "".join(f"+line {i}\n" for i in range(50))
    diff = (
        "diff --git a/big.py b/big.py\n"
        "--- a/big.py\n"
        "+++ b/big.py\n"
        f"@@ -1 +1,{50+1} @@\n"
        " keep\n"
        f"{additions}"
    )
    report = evaluate_patch_budget(diff, PatchBudget(max_added_lines=20))
    assert not report.passed
    assert any("added_lines" in v for v in report.violations)
    assert report.metrics["added_lines"] == 50


def test_max_removed_lines_violation():
    removals = "".join(f"-line {i}\n" for i in range(30))
    diff = (
        "diff --git a/big.py b/big.py\n"
        "--- a/big.py\n"
        "+++ b/big.py\n"
        "@@ -1,30 +1 @@\n"
        f"{removals}"
        "+keep\n"
    )
    report = evaluate_patch_budget(diff, PatchBudget(max_removed_lines=10))
    assert not report.passed
    assert any("removed_lines" in v for v in report.violations)


def test_max_new_files_violation():
    diff = (
        "diff --git a/new1.py b/new1.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new1.py\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
        "diff --git a/new2.py b/new2.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new2.py\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
        "diff --git a/new3.py b/new3.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new3.py\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )
    report = evaluate_patch_budget(diff, PatchBudget(max_new_files=2))
    assert not report.passed
    assert any("new_files" in v for v in report.violations)
    assert report.metrics["new_files"] == 3


def test_max_new_imports_per_file_violation_python():
    diff = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1 +1,7 @@\n"
        " original\n"
        "+import os\n"
        "+import sys\n"
        "+from json import loads\n"
        "+from re import match\n"
        "+from pathlib import Path\n"
        "+from collections import Counter\n"
    )
    report = evaluate_patch_budget(diff, PatchBudget(max_new_imports_per_file=3))
    assert not report.passed
    assert any("new_imports" in v for v in report.violations)
    assert report.metrics["max_new_imports_per_file"] == 6


def test_imports_below_per_file_threshold_pass():
    diff = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1 +1,3 @@\n"
        " original\n"
        "+import os\n"
        "+import sys\n"
    )
    report = evaluate_patch_budget(diff, PatchBudget(max_new_imports_per_file=5))
    assert report.passed


def test_multiple_violations_all_reported():
    additions = "".join(f"+line {i}\n" for i in range(50))
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        f"@@ -1 +1,{50+1} @@\n"
        " keep\n"
        f"{additions}"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        f"@@ -1 +1,{50+1} @@\n"
        " keep\n"
        f"{additions}"
    )
    report = evaluate_patch_budget(
        diff,
        PatchBudget(max_files_changed=1, max_added_lines=10),
    )
    assert not report.passed
    # both violations surfaced — caller can show all to LLM in repair prompt
    assert any("files_changed" in v for v in report.violations)
    assert any("added_lines" in v for v in report.violations)


def test_metrics_include_per_file_breakdown():
    diff = _MINIMAL_DIFF + (
        "diff --git a/app/bar.py b/app/bar.py\n"
        "--- a/app/bar.py\n"
        "+++ b/app/bar.py\n"
        "@@ -1,1 +1,3 @@\n"
        " keep\n"
        "+a\n"
        "+b\n"
    )
    report = evaluate_patch_budget(diff, PatchBudget())
    per_file = report.metrics["per_file"]
    assert "app/foo.py" in per_file
    assert "app/bar.py" in per_file
    assert per_file["app/foo.py"]["added"] == 1
    assert per_file["app/bar.py"]["added"] == 2
