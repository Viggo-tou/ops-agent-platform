"""Tests for python_extractor.py — the Python plug-in.

Uses real Python source *byte strings*, not actual project files.
"""

from __future__ import annotations

from app.services.symbol_graph.python_extractor import PythonExtractor


def _extract(source: str, path: str = "test.py") -> tuple[list, list]:
    """Convenience: run the extractor on *source* and return (decls, refs)."""
    ex = PythonExtractor()
    result = ex.extract(path=path, source=source.encode("utf-8"))
    return list(result.decls), list(result.refs)


class TestEmptySource:
    def test_empty_source(self):
        decls, refs = _extract("")
        assert decls == []
        assert refs == []

    def test_only_comments(self):
        decls, refs = _extract("# just a comment\n# another\n")
        assert decls == []
        assert refs == []


class TestFunctionDef:
    def test_def_foo(self):
        decls, refs = _extract("def foo(): pass\n")
        assert len(decls) == 1
        assert decls[0].name == "foo"
        assert decls[0].kind == "function"
        assert decls[0].file == "test.py"
        assert decls[0].line == 1
        assert refs == []

    def test_async_def(self):
        decls, refs = _extract("async def bar(): pass\n")
        assert len(decls) == 1
        assert decls[0].name == "bar"
        assert decls[0].kind == "function"


class TestClassDef:
    def test_class_bar(self):
        decls, refs = _extract("class Bar: pass\n")
        assert len(decls) == 1
        assert decls[0].name == "Bar"
        assert decls[0].kind == "class"


class TestVariableAssignment:
    def test_x_equals_1(self):
        decls, refs = _extract("x = 1\n")
        assert len(decls) == 1
        assert decls[0].name == "x"
        assert decls[0].kind == "variable"

    def test_multiple_targets(self):
        decls, refs = _extract("a = b = 1\n")
        assert len(decls) == 2
        names = {d.name for d in decls}
        assert names == {"a", "b"}

    def test_tuple_unpack_skipped(self):
        """Tuple unpack like a, b = 1, 2 — targets are ast.Tuple, not ast.Name."""
        decls, refs = _extract("a, b = 1, 2\n")
        # a, b is a Tuple node, not Name nodes — so 0 decls for now
        assert len(decls) == 0


class TestImport:
    def test_import_os(self):
        decls, refs = _extract("import os\n")
        assert decls == []
        assert len(refs) == 1
        assert refs[0].name == "os"
        assert refs[0].expected_kind == "module"

    def test_import_multiple(self):
        decls, refs = _extract("import os, sys\n")
        assert len(refs) == 2
        names = {r.name for r in refs}
        assert names == {"os", "sys"}


class TestImportFrom:
    def test_from_import_c_d(self):
        decls, refs = _extract("from a.b import c, d\n")
        assert len(refs) == 2
        assert refs[0].name == "c"
        assert refs[0].expected_kind is None
        assert refs[1].name == "d"
        assert refs[1].expected_kind is None

    def test_from_import_single(self):
        decls, refs = _extract("from foo import bar\n")
        assert len(refs) == 1
        assert refs[0].name == "bar"
        assert refs[0].expected_kind is None


class TestSyntaxError:
    def test_syntax_error_returns_empty(self):
        decls, refs = _extract("def broken(: pass\n")
        assert decls == []
        assert refs == []

    def test_syntax_error_does_not_crash(self):
        # Just making sure no exception escapes
        ex = PythonExtractor()
        result = ex.extract(path="x.py", source=b"@#$%^&*(")
        assert result.decls == ()
        assert result.refs == ()


class TestCombinedSource:
    def test_combined_counts(self):
        source = """\
def foo(): pass
class Bar: pass
x = 1
import os
from a.b import c, d
"""
        decls, refs = _extract(source)
        # 2 declarations: foo (function), Bar (class), x (variable) = 3
        assert len(decls) == 3
        kinds = {d.kind for d in decls}
        assert kinds == {"function", "class", "variable"}
        # 3 refs: os (module), c (None), d (None)
        assert len(refs) == 3


class TestLanguageProperty:
    def test_language_is_python(self):
        ex = PythonExtractor()
        assert ex.language == "python"
