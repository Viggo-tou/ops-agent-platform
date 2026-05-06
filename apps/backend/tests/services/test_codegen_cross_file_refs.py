"""L4e: cross-file ref consistency tests."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codegen_self_validate import (  # noqa: E402
    self_validate,
    validate_cross_file_refs,
)


def test_l4e_v28_oscillation_caught():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,6 +1,6 @@\n"
        " package example\n"
        " data class Job(\n"
        "-    val jobLocation: String,\n"
        "+    val location: String,\n"
        "     val title: String,\n"
        " )\n"
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,5 +1,5 @@\n"
        " package example\n"
        " fun bind(job: Job) {\n"
        "     val display = job.jobLocation\n"
        "     println(display)\n"
        " }\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is False
    assert "jobLocation" in err
    assert "JobPostingFragment.kt" in err


def test_l4e_clean_rename_with_consistent_update_passes():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,6 +1,6 @@\n"
        " package example\n"
        " data class Job(\n"
        "-    val jobLocation: String,\n"
        "+    val location: String,\n"
        "     val title: String,\n"
        " )\n"
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,5 +1,5 @@\n"
        " package example\n"
        " fun bind(job: Job) {\n"
        "-    val display = job.jobLocation\n"
        "+    val display = job.location\n"
        "     println(display)\n"
        " }\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is True
    assert err == ""


def test_l4e_single_file_diff_passes():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,6 +1,6 @@\n"
        " package example\n"
        " data class Job(\n"
        "-    val jobLocation: String,\n"
        "+    val location: String,\n"
        "     val title: String,\n"
        " )\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is True
    assert err == ""


def test_l4e_python_skipped():
    diff = (
        "diff --git a/model.py b/model.py\n"
        "--- a/model.py\n"
        "+++ b/model.py\n"
        "@@ -1,2 +1,2 @@\n"
        " class Model:\n"
        "-    def foo(self):\n"
        "+    def bar(self):\n"
        "diff --git a/view.py b/view.py\n"
        "--- a/view.py\n"
        "+++ b/view.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def render(obj):\n"
        "     return obj.foo()\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is True
    assert err == ""


def test_l4e_kotlin_with_no_removals_passes():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,3 +1,4 @@\n"
        " package example\n"
        " data class Job(val title: String)\n"
        "+val location = \"Remote\"\n"
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,3 +1,4 @@\n"
        " package example\n"
        "+fun bind(job: Job) = job.title\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is True
    assert err == ""


def test_l4e_unrelated_dot_reference_does_not_trigger():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,6 +1,6 @@\n"
        " package example\n"
        " data class Job(\n"
        "-    val jobLocation: String,\n"
        "+    val location: String,\n"
        "     val title: String,\n"
        " )\n"
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,5 +1,5 @@\n"
        " package example\n"
        " fun bind(obj: Other) {\n"
        "     val display = obj.someOtherField\n"
        "     println(display)\n"
        " }\n"
    )

    ok, err = validate_cross_file_refs(diff)

    assert ok is True
    assert err == ""

