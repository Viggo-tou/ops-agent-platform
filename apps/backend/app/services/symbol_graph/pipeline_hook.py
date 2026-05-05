"""Convenience adapter the orchestrator uses to run RefValidityGate
against a set of changed files post-codegen, against the rest-of-repo
context.
"""

from __future__ import annotations

from pathlib import Path

from app.services.symbol_graph import python_extractor  # noqa: F401 — auto-register
from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.gate import RefValidityReport, validate_refs
from app.services.symbol_graph.protocol import Decl, ExtractedSymbols
from app.services.symbol_graph.registry import get_extractor_for_path


# File-based resource directories: any file under one of these is its own
# Decl by virtue of existing. Generic across any Android-conventions repo
# (and analogous for any "directory == namespace, filename == identifier"
# layout). The kind is the directory name (drawable / layout / menu / ...).
# Without this, Android resource refs like @drawable/foo / @layout/bar
# always fail ref-validity because the extractor only emits Decls from
# `<string name="X"/>`-style tags inside XML, and drawable/layout are
# typically stand-alone files.
_FILE_BASED_RESOURCE_KINDS: tuple[str, ...] = (
    "drawable", "layout", "menu", "navigation",
    "anim", "animator", "color", "font", "interpolator", "mipmap",
    "raw", "transition", "xml",
)


def _emit_file_based_resource_decls(
    repo_root: Path, file_paths: tuple[str, ...],
) -> ExtractedSymbols:
    """Walk ``file_paths`` and emit a Decl for every Android-style
    resource file ``res/<KIND>/<NAME>.<ext>`` (or
    ``res/<KIND>-qualifier/<NAME>.<ext>`` — drawable-hdpi, layout-land,
    etc.). Decl name = filename stem; kind = directory name (the
    qualifier is stripped). The file path doubles as the source.

    Multiple files producing the same `(name, kind)` (e.g. drawable-hdpi/foo.png
    and drawable-mdpi/foo.png) emit one Decl each — the gate's resolver
    accepts any matching kind, so duplicates don't hurt.
    """
    decls: list[Decl] = []
    for rel in file_paths:
        norm = rel.replace("\\", "/")
        parts = norm.split("/")
        # Look for the first occurrence of "res" then read the next segment
        # as `<KIND>` or `<KIND>-<qualifier>`.
        if "res" not in parts:
            continue
        ri = parts.index("res")
        if ri + 2 >= len(parts):  # need res/<KIND>/<filename>
            continue
        kind_with_qualifier = parts[ri + 1]
        # `drawable`, `drawable-hdpi`, `layout-land` -> base kind
        kind = kind_with_qualifier.split("-", 1)[0]
        if kind not in _FILE_BASED_RESOURCE_KINDS:
            continue
        filename = parts[-1]
        # Strip extension (.xml, .png, .9.png, .jpg, .webp, .svg, ...)
        stem = filename
        for suffix in (".9.png", ".png", ".jpg", ".jpeg", ".webp",
                       ".svg", ".xml", ".gif", ".ttf", ".otf"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            # Generic single-suffix strip
            if "." in stem:
                stem = stem.rsplit(".", 1)[0]
        if not stem:
            continue
        decls.append(Decl(
            name=stem,
            kind=kind,
            file=norm,
            line=0,
            metadata={"file_based_resource": True},
        ))
    return ExtractedSymbols(decls=tuple(decls), refs=())


def build_graph_for_repo(
    *,
    repo_root: Path,
    file_paths: tuple[str, ...],
) -> tuple[SymbolGraph, int]:
    """Ingest each file.  Returns ``(graph, files_skipped_count)``.

    Skipped files are those with no registered extractor AND not a
    file-based resource — graceful, not an error.
    """
    graph = SymbolGraph.empty()
    skipped = 0

    # First pass: emit file-based resource decls (Android drawables /
    # layouts / menus / ...). These don't need an extractor; the file's
    # existence in res/<kind>/<name>.<ext> IS the declaration.
    file_resource_symbols = _emit_file_based_resource_decls(
        repo_root, file_paths,
    )
    if file_resource_symbols.decls:
        graph.ingest(path="<file_based_resources>",
                     symbols=file_resource_symbols)
    file_based_paths: set[str] = {
        d.file for d in file_resource_symbols.decls
    }

    for path in file_paths:
        full = repo_root / path
        extractor = get_extractor_for_path(path)
        if extractor is None:
            # File-based resource files (e.g. drawable PNGs) already
            # contributed their Decl above; don't count those as skipped.
            if path.replace("\\", "/") not in file_based_paths:
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
