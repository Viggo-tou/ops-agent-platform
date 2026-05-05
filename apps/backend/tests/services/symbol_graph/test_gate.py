"""Tests for gate.py — validate_refs()."""

from __future__ import annotations

from app.services.symbol_graph.gate import (
    RefValidityReport,
    RefValidityViolation,
    validate_refs,
)
from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.protocol import Decl, ExtractedSymbols, Ref


class TestEmptyGraph:
    def test_empty_graph_passed_true(self):
        g = SymbolGraph.empty()
        report = validate_refs(graph=g)
        assert report.passed is True
        assert report.refs_checked == 0
        assert len(report.violations) == 0
        assert report.files_covered == 0


class TestMatchingRef:
    def test_one_ref_matching_decl_passed_true(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="function", file="a.py")
        r = Ref(name="foo", expected_kind="function", file="b.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=()))
        g.ingest(path="b.py", symbols=ExtractedSymbols(decls=(), refs=(r,)))

        report = validate_refs(graph=g)
        assert report.passed is True
        assert report.refs_checked == 1
        assert len(report.violations) == 0


class TestNoMatchingDecl:
    def test_ref_no_decl_found(self):
        g = SymbolGraph.empty()
        r = Ref(name="missing_func", expected_kind=None, file="a.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(), refs=(r,)))

        report = validate_refs(graph=g)
        assert report.passed is False
        assert report.refs_checked == 1
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.ref is r
        assert v.reason == "no_decl_found"


class TestKindMismatch:
    def test_ref_expects_function_decl_is_class(self):
        g = SymbolGraph.empty()
        d = Decl(name="MyClass", kind="class", file="a.py")
        r = Ref(name="MyClass", expected_kind="function", file="b.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=()))
        g.ingest(path="b.py", symbols=ExtractedSymbols(decls=(), refs=(r,)))

        report = validate_refs(graph=g)
        assert report.passed is False
        assert len(report.violations) == 1
        assert report.violations[0].reason == "kind_mismatch"


class TestOnlyFilesFilter:
    def test_violation_hidden_when_file_not_in_only_files(self):
        g = SymbolGraph.empty()
        # file_a has a broken ref
        r_bad = Ref(name="nope", expected_kind=None, file="file_a.py")
        g.ingest(path="file_a.py", symbols=ExtractedSymbols(decls=(), refs=(r_bad,)))

        # file_b has a clean ref
        d = Decl(name="ok", kind="function", file="file_c.py")
        r_ok = Ref(name="ok", expected_kind="function", file="file_b.py")
        g.ingest(path="file_b.py", symbols=ExtractedSymbols(decls=(), refs=(r_ok,)))
        g.ingest(path="file_c.py", symbols=ExtractedSymbols(decls=(d,), refs=()))

        report = validate_refs(graph=g, only_files=("file_b.py",))
        assert report.passed is True
        assert report.refs_checked == 1


class TestToPayload:
    def test_to_payload_contains_all_keys(self):
        import json

        g = SymbolGraph.empty()
        r = Ref(name="x", expected_kind=None, file="f.py")
        g.ingest(path="f.py", symbols=ExtractedSymbols(decls=(), refs=(r,)))

        report = validate_refs(graph=g)
        payload = report.to_payload()
        assert "passed" in payload
        assert "refs_checked" in payload
        assert "files_covered" in payload
        assert "files_skipped" in payload
        assert "violations" in payload
        assert payload["passed"] is False
        assert payload["refs_checked"] == 1

        # Must be JSON-serializable
        encoded = json.dumps(payload)
        assert isinstance(encoded, str)


class TestRefValidityViolationFrozen:
    def test_frozen(self):
        import pytest

        r = Ref(name="x", expected_kind=None, file="f.py")
        v = RefValidityViolation(ref=r, reason="no_decl_found")
        with pytest.raises(Exception):
            v.reason = "other"  # type: ignore[misc]


class TestRefValidityReportFrozen:
    def test_frozen(self):
        import pytest

        report = RefValidityReport(
            passed=True,
            violations=(),
            refs_checked=0,
            files_covered=0,
            files_skipped=0,
        )
        with pytest.raises(Exception):
            report.passed = False  # type: ignore[misc]
