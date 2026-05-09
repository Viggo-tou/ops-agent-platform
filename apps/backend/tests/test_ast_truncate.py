"""Tests for AST-aware structural truncation (Tier 2).

Pins the contract that ``truncate_python_source`` keeps imports,
class signatures, and small / pinned function bodies whole, and only
elides large unrelated function bodies. Output must remain
syntactically valid Python.
"""
from __future__ import annotations

import ast

from app.services.ast_truncate import truncate_python_source


def _big_function(name: str, lines: int = 60) -> str:
    body = "\n".join(f"    x_{i} = {i}" for i in range(lines))
    return f"def {name}(arg):\n    \"\"\"docstring for {name}.\"\"\"\n{body}\n    return arg\n"


def test_under_budget_returns_unchanged():
    src = "import os\n\ndef foo():\n    return 1\n"
    result = truncate_python_source(src, max_bytes=10_000)
    assert result.text == src
    assert result.used_ast is False


def test_truncates_big_function_keeps_signature_and_docstring():
    src = "import os\n\n" + _big_function("big_one", lines=80)
    result = truncate_python_source(src, max_bytes=200)
    assert result.used_ast is True
    assert "def big_one(arg):" in result.text
    assert '"""docstring for big_one."""' in result.text
    # Body lines should be elided.
    assert "x_50 = 50" not in result.text
    assert "elided by ast_truncate" in result.text
    assert "big_one" in result.symbols_truncated
    # Output must still parse.
    ast.parse(result.text)


def test_keeps_small_function_whole_even_when_over_budget():
    """A function whose body is ≤ 30 lines is kept whole."""
    short = "def small_fn():\n    return 1 + 2 + 3\n"
    big = _big_function("big_fn", lines=80)
    src = "import os\n\n" + short + "\n" + big
    result = truncate_python_source(src, max_bytes=120)
    assert "small_fn" in result.symbols_kept_whole
    assert "return 1 + 2 + 3" in result.text
    assert "big_fn" in result.symbols_truncated


def test_keep_symbols_pins_a_named_function_whole():
    big = _big_function("focus_fn", lines=80)
    other = _big_function("other_fn", lines=80)
    src = "import os\n\n" + big + "\n" + other
    result = truncate_python_source(
        src, max_bytes=200, keep_symbols=["focus_fn"]
    )
    # focus_fn body must be present verbatim; other_fn body elided.
    assert "x_50 = 50" in result.text  # one of focus_fn's body lines
    assert "focus_fn" in result.symbols_kept_whole
    assert "other_fn" in result.symbols_truncated


def test_class_signature_kept_methods_truncated_individually():
    methods_src = (
        "class Foo:\n"
        "    \"\"\"class docstring.\"\"\"\n"
        "    a = 1\n"
        + _indent(_big_function("method_big", lines=80))
        + "\n"
        + _indent("def method_small(self):\n    return 1\n")
    )
    src = "import os\n\n" + methods_src
    result = truncate_python_source(src, max_bytes=200)
    assert "class Foo:" in result.text
    assert "class docstring" in result.text
    # class-level constant kept
    assert "a = 1" in result.text
    # small method kept whole
    assert "def method_small(self):" in result.text
    assert "return 1" in result.text
    # big method header kept, body elided
    assert "def method_big(arg):" in result.text
    assert "x_50 = 50" not in result.text
    ast.parse(result.text)


def test_imports_always_kept():
    src = (
        "import os\n"
        "import sys\n"
        "from collections import OrderedDict\n\n"
        + _big_function("big_one", lines=80)
    )
    result = truncate_python_source(src, max_bytes=100)
    assert "import os" in result.text
    assert "import sys" in result.text
    assert "from collections import OrderedDict" in result.text


def test_module_level_constants_kept():
    src = (
        "ANSWER = 42\n"
        "NAME = \"foo\"\n\n"
        + _big_function("bigger", lines=80)
    )
    result = truncate_python_source(src, max_bytes=80)
    assert "ANSWER = 42" in result.text
    assert "NAME = \"foo\"" in result.text


def test_syntax_error_passes_through_unchanged():
    bad = "this is not python ::: ###"
    result = truncate_python_source(bad, max_bytes=5)
    assert result.text == bad  # caller falls back to byte truncation
    assert result.used_ast is False


def test_output_parses_after_truncation():
    """The output of truncate should always be syntactically valid
    Python so downstream tooling (codegen prompt builder, AST-based
    feature checks) keeps working."""
    src = (
        "import os\n"
        "from typing import Any\n\n"
        "CONST = 1\n\n"
        + _big_function("a", lines=60)
        + "\n"
        + _big_function("b", lines=60)
        + "\n"
        + "class Bar:\n"
        + _indent("def m(self):\n    return None\n")
    )
    result = truncate_python_source(src, max_bytes=100)
    ast.parse(result.text)
    assert result.elided_lines > 0


def test_async_function_truncated_same_as_sync():
    src = (
        "import os\n\n"
        "async def big_async():\n"
        "    \"\"\"async doc.\"\"\"\n"
        + "\n".join(f"    a = {i}" for i in range(80))
        + "\n"
    )
    result = truncate_python_source(src, max_bytes=80)
    assert "async def big_async():" in result.text
    assert '"""async doc."""' in result.text
    assert "big_async" in result.symbols_truncated
    ast.parse(result.text)


# --- helpers ----------------------------------------------------------------


def _indent(block: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in block.splitlines()) + "\n"
