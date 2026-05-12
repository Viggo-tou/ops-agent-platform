"""Tests for symbol_hints — candidate symbol extraction (Tier 2)."""
from __future__ import annotations

from app.services.symbol_hints import (
    extract_candidate_symbols,
    extract_keep_symbols_for_files,
)


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


# --- AST cross-reference (regression: astropy-14995 on 2026-05-10) ----------


def test_keep_symbols_indirect_concept_match():
    """Issue text never names the function, but mentions 'mask
    propagation'. Cross-referencing against the file's AST surfaces
    `_arithmetic_mask` because its name contains 'mask'."""
    issue = "NDDataRef mask propagation fails when one operand has no mask."
    files = {
        "ndarithmetic.py": (
            "class NDArithmeticMixin:\n"
            "    def add(self, other):\n"
            "        return self._arithmetic_mask(other)\n"
            "    def _arithmetic_mask(self, other):\n"
            "        return self.mask | other.mask\n"
            "    def unrelated_helper(self):\n"
            "        return None\n"
        ),
    }
    hints = extract_keep_symbols_for_files(issue, files)
    assert "_arithmetic_mask" in hints
    assert "unrelated_helper" not in hints


def test_keep_symbols_includes_direct_and_indirect():
    """Both direct (issue mentions `_helper`) and indirect (function
    name contains issue word) should be merged in result."""
    issue = "Fix the `_helper` and the propagate logic."
    files = {
        "x.py": (
            "def _helper(): pass\n"
            "def propagate_mask(): pass\n"
            "def boring(): pass\n"
        ),
    }
    hints = extract_keep_symbols_for_files(issue, files)
    assert "_helper" in hints
    assert "propagate_mask" in hints
    assert "boring" not in hints


def test_keep_symbols_empty_files_uses_direct_only():
    issue = "Fix `_apply` method."
    hints = extract_keep_symbols_for_files(issue, {})
    assert hints == ["_apply"]


def test_keep_symbols_skips_non_python():
    issue = "fix the mask routine"
    files = {
        "Foo.kt": "fun maskHelper() { }\n",
        "ok.py": "def maskHelper(): pass\n",
    }
    hints = extract_keep_symbols_for_files(issue, files)
    assert "maskHelper" in hints  # only from .py


def test_keep_symbols_handles_syntax_error_gracefully():
    issue = "fix the mask thing"
    files = {"bad.py": "this is not python ###"}
    # Should not raise; returns empty (no parseable functions).
    hints = extract_keep_symbols_for_files(issue, files)
    assert hints == []


def test_keep_symbols_pins_closure_ancestor_chain():
    """Codex rec #2 (closure pinning): when the matched function is a
    nested closure, also pin every enclosing function/class so the
    truncator keeps the parent body that defines the closure's binding
    and registration site.

    Regression target: django-12284 needs `Field.contribute_to_class`
    body because `_get_FIELD_display` is a closure inside it. Pinning
    only the closure name leaves the model with no surrounding
    context."""
    issue = "Model.get_FOO_display() does not work correctly with inherited choices."
    files = {
        "django/db/models/fields/__init__.py": (
            "class Field:\n"
            "    def contribute_to_class(self, cls, name):\n"
            "        self.set_attributes_from_name(name)\n"
            "        self.model = cls\n"
            "        cls._meta.add_field(self)\n"
            "        if self.choices is not None:\n"
            "            def _get_FIELD_display(obj):\n"
            "                value = getattr(obj, self.attname)\n"
            "                return self._choices_to_value(value)\n"
            "            setattr(cls, 'get_%s_display' % name, _get_FIELD_display)\n"
            "    def unrelated_helper(self):\n"
            "        return None\n"
        ),
    }
    hints = extract_keep_symbols_for_files(issue, files)
    # The closure that matched concept word "display" should be pinned.
    assert "_get_FIELD_display" in hints
    # AND its enclosing function should also be pinned.
    assert "contribute_to_class" in hints
    # AND the enclosing class.
    assert "Field" in hints
    # Unrelated helpers should NOT be pinned.
    assert "unrelated_helper" not in hints


def test_ancestor_pin_works_through_regex_fallback():
    """Same expansion when ast.parse rejects the file."""
    issue = "fix the display logic for inherited choices"
    src = (
        '_doc = """unterminated\n'  # trips ast.parse
        "class Field:\n"
        "    def contribute_to_class(self, cls, name):\n"
        "        if self.choices:\n"
        "            def _get_FIELD_display(obj):\n"
        "                return None\n"
    )
    import ast as _ast
    raises = False
    try:
        _ast.parse(src)
    except SyntaxError:
        raises = True
    assert raises, "test premise broken — file accepted by ast.parse"

    hints = extract_keep_symbols_for_files(issue, {"x.py": src})
    assert "_get_FIELD_display" in hints
    assert "contribute_to_class" in hints
    assert "Field" in hints


def test_keep_symbols_regex_fallback_finds_names_when_ast_fails():
    """Real-world: Python 3.14 rejects astropy ndarithmetic.py with a
    triple-quote parity error even though the file runs fine. Regex
    fallback must still surface the function names so cross-reference
    matching works."""
    # Craft a file ast.parse rejects but with clear def lines.
    src = (
        '_doc = """unterminated\n'
        "import os\n"
        "def _arithmetic_mask(self, op):\n"
        "    return self.mask | op.mask\n"
        "def helper():\n"
        "    pass\n"
    )
    # Sanity: confirm ast actually rejects, otherwise this doesn't
    # exercise the fallback.
    import ast as _ast
    raises = False
    try:
        _ast.parse(src)
    except SyntaxError:
        raises = True
    assert raises, "test premise broken — ast.parse accepted source"

    issue = "mask propagation fails"
    hints = extract_keep_symbols_for_files(issue, {"x.py": src})
    assert "_arithmetic_mask" in hints


def test_keep_symbols_caps_total_at_8():
    issue = "fix the foo handler logic"
    files = {
        "x.py": "\n".join(f"def foo_handler_{i}(): pass" for i in range(20))
    }
    hints = extract_keep_symbols_for_files(issue, files)
    assert len(hints) <= 8


def test_keep_symbols_filters_obvious_stopwords():
    """Pure stopwords (the/this/and/etc) should not produce any pins."""
    issue = "the and this when from with that into"
    files = {"x.py": "def the_helper(): pass\ndef and_routine(): pass\n"}
    hints = extract_keep_symbols_for_files(issue, files)
    assert hints == []
