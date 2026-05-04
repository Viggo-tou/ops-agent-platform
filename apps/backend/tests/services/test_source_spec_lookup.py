"""Unit tests for source_spec_lookup."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.source_spec_lookup import lookup_source_path  # noqa: E402


def _settings(specs: str | None):
    return SimpleNamespace(knowledge_source_specs=specs)


def test_lookup_returns_path_for_configured_source():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"alpha={tmp}|Customer-facing app description"
        out = lookup_source_path("alpha", _settings(specs))
        assert out == tmp


def test_lookup_returns_path_without_description():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"alpha={tmp}"
        out = lookup_source_path("alpha", _settings(specs))
        assert out == tmp


def test_lookup_case_insensitive():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"AlphaSrc={tmp}|desc"
        assert lookup_source_path("alphasrc", _settings(specs)) == tmp
        assert lookup_source_path("ALPHASRC", _settings(specs)) == tmp


def test_lookup_returns_none_for_unknown_source():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"alpha={tmp}|desc"
        assert lookup_source_path("beta", _settings(specs)) is None


def test_lookup_returns_none_when_path_does_not_exist():
    specs = "alpha=D:/nonexistent/path/here|desc"
    assert lookup_source_path("alpha", _settings(specs)) is None


def test_lookup_returns_none_with_no_specs_configured():
    assert lookup_source_path("alpha", _settings(None)) is None
    assert lookup_source_path("alpha", _settings("")) is None


def test_lookup_returns_none_for_empty_source_name():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"alpha={tmp}"
        assert lookup_source_path("", _settings(specs)) is None
        assert lookup_source_path("   ", _settings(specs)) is None


def test_lookup_with_multiple_sources_picks_correct_one():
    with tempfile.TemporaryDirectory() as alpha_dir:
        with tempfile.TemporaryDirectory() as beta_dir:
            specs = f"alpha={alpha_dir}|first;beta={beta_dir}|second"
            assert lookup_source_path("alpha", _settings(specs)) == alpha_dir
            assert lookup_source_path("beta", _settings(specs)) == beta_dir


def test_lookup_skips_malformed_entries():
    with tempfile.TemporaryDirectory() as tmp:
        specs = f"malformed_no_equals;alpha={tmp}|desc"
        assert lookup_source_path("alpha", _settings(specs)) == tmp
