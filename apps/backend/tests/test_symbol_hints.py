"""Tests for symbol_hints — candidate symbol extraction (Tier 2)."""
from __future__ import annotations

from app.services.symbol_hints import extract_candidate_symbols


def test_extracts_underscore_prefixed_name():
    hints = extract_candidate_symbols(
        "Fix the _arithmetic_mask method in the NDDataRef class."
    )
    assert "_arithmetic_mask" in hints


def test_extracts_backtick_quoted_name():
    hints = extract_candidate_symbols(
        "The `process_query` function fails when no_pks is empty."
    )
    assert "process_query" in hints


def test_extracts_camelcase_call():
    hints = extract_candidate_symbols(
        "Should call NDDataRef(data, mask) before propagating."
    )
    assert "NDDataRef" in hints


def test_repeated_snake_case_passes():
    hints = extract_candidate_symbols(
        "fix_arithmetic should call fix_arithmetic with the original mask."
    )
    assert "fix_arithmetic" in hints


def test_singleton_snake_case_dropped():
    """A snake_case name that appears once in prose is too likely to be
    a regular English compound word; require 2+ occurrences."""
    hints = extract_candidate_symbols(
        "When the bug fires there is a stack trace that mentions some_routine somewhere."
    )
    assert "some_routine" not in hints


def test_filter_against_file_contents_drops_fabricated():
    """If file_contents is provided, candidates not present in any file
    are dropped — protects against the model pinning fabricated names."""
    text = "Fix _arithmetic_mask and also _imaginary_method."
    files = {
        "x.py": "def _arithmetic_mask(self):\n    pass\n",
    }
    hints = extract_candidate_symbols(text, file_contents=files)
    assert "_arithmetic_mask" in hints
    assert "_imaginary_method" not in hints  # not in files


def test_stopwords_filtered():
    hints = extract_candidate_symbols(
        "The fix should fix the issue with the test file."
    )
    # All of these are either stopwords or singletons; nothing snuck in.
    assert "fix" not in hints
    assert "issue" not in hints
    assert "test" not in hints


def test_empty_input():
    assert extract_candidate_symbols("") == []
    assert extract_candidate_symbols("   ") == []


def test_caps_at_8():
    text = " ".join(f"_method_{i}" for i in range(20))
    hints = extract_candidate_symbols(text)
    assert len(hints) <= 8


def test_dedup_preserves_first_occurrence():
    hints = extract_candidate_symbols(
        "_helper used by _helper. Then NDArray and NDArray() and NDArray()."
    )
    assert hints.count("_helper") == 1
    assert hints.count("NDArray") == 1


def test_minimum_length_3():
    hints = extract_candidate_symbols("a b _x _y")
    # Length-2 names dropped.
    assert "_x" not in hints
    assert "_y" not in hints
