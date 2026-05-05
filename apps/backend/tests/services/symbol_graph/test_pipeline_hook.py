"""Tests for pipeline_hook.py — orchestrator-facing helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.services.symbol_graph.pipeline_hook import (
    build_graph_for_repo,
    check_changed_files,
)
from app.services.symbol_graph.python_extractor import PythonExtractor
from app.services.symbol_graph.registry import (
    _clear_registry_for_tests,
    register_extractor,
)
from app.services.symbol_graph.protocol import (
    ExtractedSymbols,
    SymbolExtractor,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _SpyExtractor:
    """Records every call so we can assert what was extracted."""

    def __init__(self):
        self.calls: list[tuple[str, bytes]] = []

    @property
    def language(self) -> str:
        return "spy"

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        self.calls.append((path, source))
        return ExtractedSymbols(decls=(), refs=())


@pytest.fixture(autouse=True)
def _isolate_registry():
    # Clear, then re-register the python extractor explicitly. The
    # `python_extractor` module auto-registers on first import via the
    # standard module cache, so a bare `import python_extractor` after
    # the clear is a no-op (cached). Tests that need the python plug-in
    # must re-register manually after the wipe.
    _clear_registry_for_tests()
    register_extractor("py", PythonExtractor())
    yield
    _clear_registry_for_tests()


@pytest.fixture
def tmp_repo():
    """Create a temporary directory with a few .py files and a .txt file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("def foo(): pass", encoding="utf-8")
        (root / "b.py").write_text("import os", encoding="utf-8")
        (root / "c.py").write_text("x = 1", encoding="utf-8")
        (root / "notes.txt").write_text("hello world", encoding="utf-8")
        yield root


# ---------------------------------------------------------------------------
# build_graph_for_repo
# ---------------------------------------------------------------------------

class TestBuildGraphForRepo:
    def test_py_files_ingested_txt_skipped(self, tmp_repo):
        # Need the python extractor registered
        from app.services.symbol_graph import python_extractor  # noqa: F401

        graph, skipped = build_graph_for_repo(
            repo_root=tmp_repo,
            file_paths=("a.py", "b.py", "c.py", "notes.txt"),
        )

        # 1 .txt file skipped
        assert skipped == 1
        # graph should have decls from a.py and c.py, refs from b.py
        assert "foo" in graph.decls_by_name
        assert "x" in graph.decls_by_name
        assert "os" in graph.refs_by_name

    def test_skipped_count_for_unregistered_extension(self, tmp_repo):
        graph, skipped = build_graph_for_repo(
            repo_root=tmp_repo,
            file_paths=("notes.txt",),
        )
        assert skipped == 1
        assert graph.decls_by_name == {}
        assert graph.refs_by_name == {}

    def test_nonexistent_path_skipped(self, tmp_repo):
        graph, skipped = build_graph_for_repo(
            repo_root=tmp_repo,
            file_paths=("ghost.py",),
        )
        assert skipped == 1


# ---------------------------------------------------------------------------
# check_changed_files
# ---------------------------------------------------------------------------

class TestCheckChangedFiles:
    def test_broken_ref_in_changed_file_causes_failure(self, tmp_repo):
        from app.services.symbol_graph import python_extractor  # noqa: F401

        # all_repo_files: a.py declares foo, c.py declares x
        # changed_files: broken.py has a ref to "nonexistent"
        broken = tmp_repo / "broken.py"
        broken.write_text("import nonexistent_module_xyz\n", encoding="utf-8")

        report = check_changed_files(
            repo_root=tmp_repo,
            all_repo_files=("a.py", "b.py", "c.py", "broken.py"),
            changed_files=("broken.py",),
        )

        assert report.passed is False
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.ref.file == "broken.py"
        assert v.reason == "no_decl_found"

    def test_changed_file_has_no_refs(self, tmp_repo):
        from app.services.symbol_graph import python_extractor  # noqa: F401

        report = check_changed_files(
            repo_root=tmp_repo,
            all_repo_files=("a.py", "b.py", "c.py"),
            changed_files=("a.py",),  # a.py has decls but no refs
        )
        assert report.passed is True
        assert report.refs_checked == 0

    def test_txt_file_in_changed_files_just_skipped(self, tmp_repo):
        from app.services.symbol_graph import python_extractor  # noqa: F401

        report = check_changed_files(
            repo_root=tmp_repo,
            all_repo_files=("a.py", "b.py", "c.py", "notes.txt"),
            changed_files=("notes.txt",),
        )
        # .txt has no extractor — no refs extracted, so no violations
        assert report.passed is True

    def test_files_skipped_count(self, tmp_repo):
        from app.services.symbol_graph import python_extractor  # noqa: F401

        report = check_changed_files(
            repo_root=tmp_repo,
            all_repo_files=("a.py", "b.py", "c.py", "notes.txt"),
            changed_files=("a.py",),
        )
        # notes.txt has no extractor, so skipped=1
        assert report.files_skipped == 1
