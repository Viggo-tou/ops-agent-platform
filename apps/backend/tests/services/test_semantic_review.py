"""Tests for the R1 semantic_review gate.

Covers schema validation, anti-hallucination grounding, JSON extraction,
and threshold pass/fail logic. Uses an injectable llm_caller stub —
no real network calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.semantic_review import (  # noqa: E402
    SemanticReviewError,
    _extract_json_object,
    _files_in_diff,
    _is_finding_grounded,
    _RawFinding,
    _normalize_for_match,
    build_user_prompt,
    evaluate_semantic_review,
    parse_review_output,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        anthropic_api_key="test-key",
        anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-haiku-4-5",
        deepseek_api_key="test-key",
        deepseek_base_url="https://api.deepseek.com/anthropic",
        deepseek_model="deepseek-v4-pro",
    )


# Sample diff used across multiple tests
_SAMPLE_DIFF = """diff --git a/src/Foo.kt b/src/Foo.kt
--- a/src/Foo.kt
+++ b/src/Foo.kt
@@ -1,2 +1,4 @@
 class Foo {
+  val homeAddress: String? = null
+  fun loadHomeAddress() = SessionManager.get()
 }
"""


# --- _extract_json_object ---------------------------------------------------

def test_extract_json_strips_markdown_fence():
    raw = "```json\n{\"a\": 1}\n```"
    assert _extract_json_object(raw) == '{"a": 1}'


def test_extract_json_handles_bare_object():
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_finds_object_in_prose():
    raw = "Sure, here's the result:\n{\"completeness_pct\": 50}\nDone."
    out = _extract_json_object(raw)
    assert out.startswith('{"completeness_pct')


def test_extract_json_empty_returns_empty():
    assert _extract_json_object("") == ""


# --- parse_review_output ----------------------------------------------------

def test_parse_review_minimal_valid():
    raw = '{"completeness_pct": 75, "summary": "ok", "findings": []}'
    out = parse_review_output(raw)
    assert out.completeness_pct == 75
    assert out.findings == []


def test_parse_review_rejects_invalid_severity():
    raw = json.dumps({
        "completeness_pct": 50,
        "summary": "",
        "findings": [{
            "file": "x.kt", "line_start": 1, "line_end": 1,
            "severity": "critical",  # not in enum
            "category": "general", "description": "x",
            "evidence_quote": "", "suggested_fix": "",
        }],
    })
    with pytest.raises(SemanticReviewError):
        parse_review_output(raw)


def test_parse_review_rejects_completeness_above_100():
    raw = '{"completeness_pct": 200, "findings": []}'
    with pytest.raises(SemanticReviewError):
        parse_review_output(raw)


def test_parse_review_invalid_json_raises():
    with pytest.raises(SemanticReviewError):
        parse_review_output("not json")


# --- _files_in_diff ---------------------------------------------------------

def test_files_in_diff_extracts_both_a_and_b_paths():
    out = _files_in_diff(_SAMPLE_DIFF)
    assert "src/Foo.kt" in out


def test_files_in_diff_handles_multiple():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+a=1\n"
        "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n+b=2\n"
    )
    out = _files_in_diff(diff)
    assert {"x.py", "y.py"}.issubset(out)


# --- _is_finding_grounded ---------------------------------------------------

def _finding(**overrides):
    base = dict(
        file="src/Foo.kt", line_start=1, line_end=1,
        severity="high", category="general",
        description="x", evidence_quote="", suggested_fix="",
    )
    base.update(overrides)
    return _RawFinding(**base)


def test_grounded_when_quote_substring_in_diff():
    f = _finding(evidence_quote="loadHomeAddress")
    assert _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


def test_NOT_grounded_when_quote_not_in_diff():
    """Anti-hallucination: LLM made up a quote."""
    f = _finding(evidence_quote="thisStringDoesNotExistInTheDiff")
    assert not _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


def test_NOT_grounded_when_file_not_in_diff():
    f = _finding(file="src/Other.kt", evidence_quote="loadHomeAddress")
    assert not _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


def test_low_severity_no_quote_allowed():
    """Advisory low-severity findings can lack a quote (style hints)."""
    f = _finding(severity="low", evidence_quote="")
    assert _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


def test_high_severity_without_quote_NOT_grounded():
    """High-severity claims MUST be cited. Otherwise it's hallucination."""
    f = _finding(severity="high", evidence_quote="")
    assert not _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


def test_short_quote_NOT_grounded():
    """Quote of <5 chars is too noisy to verify."""
    f = _finding(evidence_quote="x")
    assert not _is_finding_grounded(
        f, diff_normalized=_normalize_for_match(_SAMPLE_DIFF),
        diff_files={"src/Foo.kt"},
    )


# --- evaluate_semantic_review (full pipeline with llm_caller stub) ---------

def test_evaluate_passes_high_completeness_no_high_findings():
    raw_response = json.dumps({
        "completeness_pct": 92,
        "summary": "Implementation looks complete.",
        "findings": [],
    })
    report = evaluate_semantic_review(
        spec_text="Add homeAddress field",
        diff=_SAMPLE_DIFF,
        file_contents={"src/Foo.kt": "class Foo { val homeAddress = null }"},
        settings=_settings(),
        pass_threshold=80,
        llm_caller=lambda _p: raw_response,
    )
    assert report.passed is True
    assert report.completeness_pct == 92
    assert report.findings == ()


def test_evaluate_fails_below_threshold():
    raw_response = json.dumps({
        "completeness_pct": 55,
        "summary": "Several gaps remain.",
        "findings": [],
    })
    report = evaluate_semantic_review(
        spec_text="x",
        diff=_SAMPLE_DIFF,
        file_contents=None,
        settings=_settings(),
        pass_threshold=80,
        llm_caller=lambda _p: raw_response,
    )
    assert report.passed is False
    assert report.completeness_pct == 55


def test_evaluate_fails_when_high_severity_present():
    raw_response = json.dumps({
        "completeness_pct": 95,  # high score, but has high finding
        "summary": "",
        "findings": [{
            "file": "src/Foo.kt", "line_start": 2, "line_end": 2,
            "severity": "high", "category": "orphan_ui",
            "description": "Field declared but no setter wires it.",
            "evidence_quote": "loadHomeAddress",  # in diff
            "suggested_fix": "Add setter call in onCreate",
        }],
    })
    report = evaluate_semantic_review(
        spec_text="x", diff=_SAMPLE_DIFF, file_contents=None,
        settings=_settings(), pass_threshold=80,
        llm_caller=lambda _p: raw_response,
    )
    # High completeness doesn't save us if a high finding stands.
    assert report.passed is False
    assert report.high_severity_count() == 1


def test_evaluate_drops_hallucinated_findings():
    """LLM cited a quote that's NOT in the diff. Finding gets dropped,
    completeness stands."""
    raw_response = json.dumps({
        "completeness_pct": 90,
        "summary": "",
        "findings": [{
            "file": "src/Foo.kt", "line_start": 1, "line_end": 1,
            "severity": "high", "category": "fabricated",
            "description": "This finding is fabricated.",
            "evidence_quote": "totallyMadeUpStringNotInDiff",
            "suggested_fix": "",
        }],
    })
    report = evaluate_semantic_review(
        spec_text="x", diff=_SAMPLE_DIFF, file_contents=None,
        settings=_settings(), pass_threshold=80,
        llm_caller=lambda _p: raw_response,
    )
    assert report.passed is True  # high finding dropped → no real high
    assert report.findings_dropped_no_evidence == 1
    assert report.findings == ()


def test_evaluate_empty_diff_passes_trivially():
    report = evaluate_semantic_review(
        spec_text="x", diff="", file_contents=None,
        settings=_settings(), pass_threshold=80,
        llm_caller=lambda _p: "should not be called",
    )
    assert report.passed is True
    assert report.completeness_pct == 100


def test_evaluate_invalid_json_raises():
    with pytest.raises(SemanticReviewError):
        evaluate_semantic_review(
            spec_text="x", diff=_SAMPLE_DIFF, file_contents=None,
            settings=_settings(), pass_threshold=80,
            llm_caller=lambda _p: "not json {{{",
        )


def test_repair_prompt_lines_render():
    report = evaluate_semantic_review(
        spec_text="x", diff=_SAMPLE_DIFF, file_contents=None,
        settings=_settings(), pass_threshold=99,
        llm_caller=lambda _p: json.dumps({
            "completeness_pct": 60,
            "summary": "",
            "findings": [{
                "file": "src/Foo.kt", "line_start": 2, "line_end": 3,
                "severity": "medium", "category": "missing_logic",
                "description": "Setter not wired.",
                "evidence_quote": "loadHomeAddress",
                "suggested_fix": "Add findViewById binding",
            }],
        }),
    )
    lines = report.repair_prompt_lines()
    assert len(lines) == 1
    assert "MEDIUM" in lines[0]
    assert "src/Foo.kt:2-3" in lines[0]
    assert "Setter not wired" in lines[0]
    assert "findViewById binding" in lines[0]


def test_payload_serializable():
    report = evaluate_semantic_review(
        spec_text="x", diff=_SAMPLE_DIFF, file_contents=None,
        settings=_settings(), pass_threshold=80,
        llm_caller=lambda _p: json.dumps({
            "completeness_pct": 88,
            "summary": "Mostly done.",
            "findings": [],
        }),
    )
    payload = report.to_payload()
    json.dumps(payload)  # must round-trip
    assert payload["completeness_pct"] == 88


# --- build_user_prompt sanity ----------------------------------------------

def test_build_user_prompt_includes_spec_diff_files():
    p = build_user_prompt(
        spec_text="implement X",
        diff=_SAMPLE_DIFF,
        file_contents={"src/Foo.kt": "class Foo {}"},
    )
    assert "implement X" in p
    assert "loadHomeAddress" in p
    assert "class Foo" in p
    assert "JSON" in p.upper() or "json" in p


def test_build_user_prompt_caps_file_content():
    huge = "x" * 50000
    p = build_user_prompt(
        spec_text="x", diff=_SAMPLE_DIFF,
        file_contents={"big.txt": huge},
        file_content_cap=2000,
    )
    # Cap ensures the prompt doesn't balloon
    assert len(p) < 30000
