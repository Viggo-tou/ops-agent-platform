"""Tests for protocol.py — dataclasses and SymbolExtractor Protocol."""

from __future__ import annotations

import pytest

from app.services.symbol_graph.protocol import (
    Decl,
    ExtractedSymbols,
    Ref,
    SymbolExtractor,
)


class TestDecl:
    def test_frozen(self):
        d = Decl(name="foo", kind="function", file="a.py", line=5)
        with pytest.raises(Exception):
            d.name = "bar"  # type: ignore[misc]

    def test_defaults(self):
        d = Decl(name="x", kind="variable", file="b.py")
        assert d.line == 0
        assert d.metadata == {}

    def test_metadata_persisted(self):
        d = Decl(name="y", kind="class", file="c.py", line=10, metadata={"lang": "en"})
        assert d.metadata == {"lang": "en"}


class TestRef:
    def test_frozen(self):
        r = Ref(name="bar", expected_kind="function", file="a.py", line=3)
        with pytest.raises(Exception):
            r.name = "baz"  # type: ignore[misc]

    def test_expected_kind_none(self):
        r = Ref(name="bar", expected_kind=None, file="b.py", line=1)
        assert r.expected_kind is None

    def test_defaults(self):
        r = Ref(name="z", expected_kind="class", file="c.py")
        assert r.line == 0
        assert r.metadata == {}


class TestExtractedSymbols:
    def test_empty(self):
        es = ExtractedSymbols(decls=(), refs=())
        assert es.decls == ()
        assert es.refs == ()

    def test_with_data(self):
        d = Decl(name="f", kind="function", file="a.py")
        r = Ref(name="f", expected_kind=None, file="b.py")
        es = ExtractedSymbols(decls=(d,), refs=(r,))
        assert len(es.decls) == 1
        assert len(es.refs) == 1

    def test_frozen(self):
        d = Decl(name="f", kind="function", file="a.py")
        es = ExtractedSymbols(decls=(d,), refs=())
        with pytest.raises(Exception):
            es.decls = ()  # type: ignore[misc]


class TestSymbolExtractorProtocol:
    def test_runtime_checkable(self):
        """SymbolExtractor is runtime_checkable — isinstance works with
        objects that implement the protocol."""

        class Good:
            @property
            def language(self) -> str:
                return "test"

            def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
                return ExtractedSymbols(decls=(), refs=())

        assert isinstance(Good(), SymbolExtractor)

    def test_missing_property_fails(self):
        class Bad:
            def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
                return ExtractedSymbols(decls=(), refs=())

        assert not isinstance(Bad(), SymbolExtractor)

    def test_missing_method_fails(self):
        class Bad:
            @property
            def language(self) -> str:
                return "test"

        assert not isinstance(Bad(), SymbolExtractor)
