"""Tests for graph.py — SymbolGraph."""

from __future__ import annotations

from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.protocol import Decl, ExtractedSymbols, Ref


class TestEmptyGraph:
    def test_empty_graph_resolve_returns_empty(self):
        g = SymbolGraph.empty()
        r = Ref(name="foo", expected_kind=None, file="a.py")
        assert g.resolve(r) == []

    def test_empty_graph_reverse_dependents_empty(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="function", file="a.py")
        assert g.reverse_dependents(d) == []


class TestSingleFileIngest:
    def test_ingest_one_decl_one_ref(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="function", file="a.py", line=1)
        r = Ref(name="foo", expected_kind="function", file="a.py", line=3)
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=(r,)))

        resolved = g.resolve(r)
        assert len(resolved) == 1
        assert resolved[0] is d

    def test_reverse_dependents_single_file(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="function", file="a.py", line=1)
        r = Ref(name="foo", expected_kind="function", file="a.py", line=5)
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=(r,)))

        deps = g.reverse_dependents(d)
        assert len(deps) == 1
        assert deps[0] is r


class TestCrossFile:
    def test_ref_in_filea_resolves_to_decl_in_fileb(self):
        g = SymbolGraph.empty()

        # file_a has a ref to "helper"
        r = Ref(name="helper", expected_kind=None, file="file_a.py", line=2)
        g.ingest(path="file_a.py", symbols=ExtractedSymbols(decls=(), refs=(r,)))

        # file_b declares "helper"
        d = Decl(name="helper", kind="function", file="file_b.py", line=1)
        g.ingest(path="file_b.py", symbols=ExtractedSymbols(decls=(d,), refs=()))

        resolved = g.resolve(r)
        assert len(resolved) == 1
        assert resolved[0] is d

    def test_reverse_dependents_cross_file(self):
        g = SymbolGraph.empty()

        d = Decl(name="util", kind="function", file="util.py", line=1)
        g.ingest(path="util.py", symbols=ExtractedSymbols(decls=(d,), refs=()))

        r1 = Ref(name="util", expected_kind="function", file="main.py", line=3)
        r2 = Ref(name="util", expected_kind="function", file="test.py", line=7)
        g.ingest(path="main.py", symbols=ExtractedSymbols(decls=(), refs=(r1,)))
        g.ingest(path="test.py", symbols=ExtractedSymbols(decls=(), refs=(r2,)))

        deps = g.reverse_dependents(d)
        assert len(deps) == 2
        assert r1 in deps
        assert r2 in deps


class TestExpectedKindFilter:
    def test_expected_kind_function_matches_function_decl(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="function", file="a.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=()))
        r = Ref(name="foo", expected_kind="function", file="b.py")
        assert len(g.resolve(r)) == 1

    def test_expected_kind_function_does_not_match_class_decl(self):
        g = SymbolGraph.empty()
        d = Decl(name="foo", kind="class", file="a.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d,), refs=()))
        r = Ref(name="foo", expected_kind="function", file="b.py")
        assert g.resolve(r) == []

    def test_expected_kind_none_matches_any_kind(self):
        g = SymbolGraph.empty()
        d1 = Decl(name="foo", kind="function", file="a.py")
        d2 = Decl(name="foo", kind="class", file="b.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d1,), refs=()))
        g.ingest(path="b.py", symbols=ExtractedSymbols(decls=(d2,), refs=()))
        r = Ref(name="foo", expected_kind=None, file="c.py")
        assert len(g.resolve(r)) == 2


class TestMultipleDeclsSameName:
    def test_resolve_returns_all(self):
        g = SymbolGraph.empty()
        d1 = Decl(name="helper", kind="function", file="a.py")
        d2 = Decl(name="helper", kind="function", file="b.py")
        g.ingest(path="a.py", symbols=ExtractedSymbols(decls=(d1,), refs=()))
        g.ingest(path="b.py", symbols=ExtractedSymbols(decls=(d2,), refs=()))

        r = Ref(name="helper", expected_kind="function", file="c.py")
        resolved = g.resolve(r)
        assert len(resolved) == 2
        assert d1 in resolved
        assert d2 in resolved


class TestIngestSource:
    def test_ingest_source_returns_false_for_unregistered_extension(self):
        g = SymbolGraph.empty()
        result = g.ingest_source(path="readme.txt", source=b"hello")
        assert result is False
