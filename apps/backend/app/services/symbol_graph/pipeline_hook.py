"""Convenience adapter the orchestrator uses to run RefValidityGate
against a set of changed files post-codegen, against the rest-of-repo
context.
"""

from __future__ import annotations

from pathlib import Path

from app.services.symbol_graph import python_extractor  # noqa: F401 — auto-register
from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.gate import RefValidityReport, validate_refs
from app.services.symbol_graph.registry import get_extractor_for_path


def build_graph_for_repo(
    *,
    repo_root: Path,
    file_paths: tuple[str, ...],
) -> tuple[SymbolGraph, int]:
    """Ingest each file.  Returns ``(graph, files_skipped_count)``.

    Skipped files are those with no registered extractor — graceful,
    not an error.
    """
    graph = SymbolGraph.empty()
    skipped = 0

    for path in file_paths:
        full = repo_root / path
        extractor = get_extractor_for_path(path)
        if extractor is None:
            skipped += 1
            continue
        try:
            source = full.read_bytes()
        except (OSError, PermissionError):
            skipped += 1
            continue
        symbols = extractor.extract(path=path, source=source)
        graph.ingest(path=path, symbols=symbols)

    return graph, skipped


def check_changed_files(
    *,
    repo_root: Path,
    all_repo_files: tuple[str, ...],
    changed_files: tuple[str, ...],
) -> RefValidityReport:
    """Build graph from *all_repo_files*, then validate only refs that
    appear in *changed_files*.

    This is the post-edit gate the orchestrator runs after codegen.
    """
    graph, skipped = build_graph_for_repo(
        repo_root=repo_root,
        file_paths=all_repo_files,
    )

    report = validate_refs(graph=graph, only_files=changed_files)

    # Carry forward the skip count from the build phase
    return RefValidityReport(
        passed=report.passed,
        violations=report.violations,
        refs_checked=report.refs_checked,
        files_covered=report.files_covered,
        files_skipped=skipped,
    )
