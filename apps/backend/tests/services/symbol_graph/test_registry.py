"""Tests for registry.py — plug-in registry."""

from __future__ import annotations

import pytest

from app.services.symbol_graph.protocol import (
    ExtractedSymbols,
    SymbolExtractor,
)
from app.services.symbol_graph.registry import (
    _clear_registry_for_tests,
    get_extractor_for_path,
    register_extractor,
    registered_extensions,
)


class _FakeExtractor:
    """Minimal SymbolExtractor-compliant object for tests."""

    @property
    def language(self) -> str:
        return "fake"

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        return ExtractedSymbols(decls=(), refs=())


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Wipe the registry before every test so tests don't leak."""
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


class TestRegisterAndRetrieve:
    def test_register_and_retrieve(self):
        ex = _FakeExtractor()
        register_extractor("py", ex)
        result = get_extractor_for_path("foo.py")
        assert result is ex

    def test_retrieve_unknown_extension_returns_none(self):
        assert get_extractor_for_path("foo.xyz") is None

    def test_retrieve_no_extension_returns_none(self):
        assert get_extractor_for_path("Makefile") is None


class TestDuplicateExtension:
    def test_duplicate_overwrites(self):
        ex1 = _FakeExtractor()
        ex2 = _FakeExtractor()
        register_extractor("py", ex1)
        register_extractor("py", ex2)
        assert get_extractor_for_path("test.py") is ex2


class TestNonConforming:
    def test_non_conforming_raises_typeerror(self):
        with pytest.raises(TypeError):
            register_extractor("py", object())  # type: ignore[arg-type]

    def test_empty_extension_raises_valueerror(self):
        with pytest.raises(ValueError):
            register_extractor("", _FakeExtractor())

    def test_dot_only_extension_raises_valueerror(self):
        with pytest.raises(ValueError):
            register_extractor(".", _FakeExtractor())


class TestRegisteredExtensions:
    def test_empty_initially(self):
        assert registered_extensions() == ()

    def test_after_register(self):
        register_extractor("py", _FakeExtractor())
        assert registered_extensions() == ("py",)

    def test_sorted(self):
        register_extractor("js", _FakeExtractor())
        register_extractor("py", _FakeExtractor())
        assert registered_extensions() == ("js", "py")


class TestCaseInsensitive:
    def test_uppercase_extension(self):
        ex = _FakeExtractor()
        register_extractor("PY", ex)
        assert get_extractor_for_path("foo.py") is ex
        assert get_extractor_for_path("foo.PY") is ex
        assert get_extractor_for_path("foo.Py") is ex

    def test_dotted_extension(self):
        ex = _FakeExtractor()
        register_extractor(".py", ex)
        assert get_extractor_for_path("foo.py") is ex
