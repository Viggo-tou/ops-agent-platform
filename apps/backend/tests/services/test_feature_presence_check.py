"""Stage X.8.b: feature_presence_check unit tests.

Captures the P69-17 failure mode where final shipped file lacks the
feature implementation despite LLM gates passing the diff text.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.feature_presence_check import (  # noqa: E402
    derive_required_tokens,
    evaluate_feature_presence,
)


def test_derive_required_tokens_from_objective():
    out = derive_required_tokens(
        objective="Pre-fill job location with user's saved homeAddress; allow edit"
    )
    assert "homeAddress" in out
    assert "Pre" in out or "fill" in out or "location" in out


def test_derive_required_tokens_dedup_and_skip_stopwords():
    out = derive_required_tokens(
        objective="The user should save the home address",
        spec_text="must allow user to edit address",
    )
    assert "the" not in [t.lower() for t in out]
    assert "should" not in [t.lower() for t in out]
    # Identifiers preserved
    assert any(tok in out for tok in ("user", "address", "edit", "save", "home"))


def test_derive_required_tokens_includes_must_touch_basenames():
    out = derive_required_tokens(
        objective="Add map picker",
        must_touch_files=["app/src/main/java/com/example/handyman/JobPostingFragment.kt"],
    )
    assert "JobPostingFragment" in out


def test_p69_17_failure_mode_caught():
    """The P69-17 case: must_touch=JobPostingFragment.kt, post-apply file
    is BASELINE without LaunchedEffect/getHomeAddress. Required tokens
    include 'homeAddress' (from objective). Check must FLAG this."""
    must_touch = ["app/src/main/java/com/example/handyman/JobPostingFragment.kt"]
    baseline = """package com.example.handyman
class JobPostingFragment {
    fun onCreate() { /* no home address logic */ }
}"""
    required = derive_required_tokens(
        objective="pre-fill location with saved homeAddress on first load"
    )
    result = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: baseline},
        required_tokens=required,
    )
    assert result.feature_absent is True
    assert "homeaddress" not in (
        " ".join(result.matched_per_file[must_touch[0]]).lower()
    )


def test_passes_when_required_tokens_appear_in_file():
    must_touch = ["src/Foo.kt"]
    body = """class Foo { fun loadHomeAddress() {} fun setAddress(a:String){} }"""
    required = ["loadHomeAddress", "setAddress", "homeAddress"]
    result = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: body},
        required_tokens=required,
    )
    assert result.feature_absent is False


def test_no_must_touch_skips_check():
    result = evaluate_feature_presence(
        must_touch_files=[],
        file_contents={},
        required_tokens=["foo"],
    )
    assert result.feature_absent is False
    assert "skipping" in result.reason


def test_no_required_tokens_skips_check():
    result = evaluate_feature_presence(
        must_touch_files=["a.kt"],
        file_contents={"a.kt": "anything"},
        required_tokens=[],
    )
    assert result.feature_absent is False
    assert "skipping" in result.reason


def test_suffix_tolerant_path_matching():
    """When must_touch is repo-relative but file_contents has source-name prefix."""
    body = "class X { fun foo() {} }"
    result = evaluate_feature_presence(
        must_touch_files=["src/X.kt"],
        file_contents={"myrepo/src/X.kt": body},
        required_tokens=["foo"],
    )
    assert result.feature_absent is False
    assert "foo" in result.matched_per_file["src/X.kt"]


def test_unmatched_listed_when_file_missing():
    result = evaluate_feature_presence(
        must_touch_files=["missing.kt"],
        file_contents={},
        required_tokens=["foo"],
    )
    assert result.feature_absent is True
    assert "missing.kt" in result.unmatched_required_files
