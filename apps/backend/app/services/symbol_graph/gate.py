from __future__ import annotations
from dataclasses import dataclass, field
from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.protocol import Ref


@dataclass(frozen=True)
class RefValidityViolation:
    ref: Ref
    reason: str  # "no_decl_found" | "kind_mismatch" | ...


@dataclass(frozen=True)
class RefValidityReport:
    passed: bool
    violations: tuple[RefValidityViolation, ...]
    refs_checked: int
    files_covered: int
    files_skipped: int  # files with no registered extractor

    def to_payload(self) -> dict:
        return {
            "passed": self.passed,
            "violations": [
                {
                    "ref_name": v.ref.name,
                    "ref_file": v.ref.file,
                    "ref_line": v.ref.line,
                    "expected_kind": v.ref.expected_kind,
                    "reason": v.reason,
                }
                for v in self.violations
            ],
            "refs_checked": self.refs_checked,
            "files_covered": self.files_covered,
            "files_skipped": self.files_skipped,
        }


def validate_refs(
    *,
    graph: SymbolGraph,
    only_files: tuple[str, ...] | None = None,
) -> RefValidityReport:
    """Walk all refs (or refs in `only_files` if provided), assert each
    resolves to a decl. Return report. Caller decides what to do with
    violations (orchestrator: turn into REVIEW_FAILED event).
    """
    violations: list[RefValidityViolation] = []
    refs_checked = 0
    files_with_extractors: set[str] = set()

    # Determine which refs to check
    candidate_refs: list[Ref] = []
    if only_files is not None:
        for fname in only_files:
            candidate_refs.extend(graph.refs_by_file.get(fname, []))
        # Files covered = those from only_files that appear anywhere in the graph
        for fname in only_files:
            if fname in graph.refs_by_file or fname in graph.decls_by_file:
                files_with_extractors.add(fname)
    else:
        for ref_list in graph.refs_by_file.values():
            candidate_refs.extend(ref_list)
        files_with_extractors = set(graph.decls_by_file.keys()) | set(graph.refs_by_file.keys())

    for ref in candidate_refs:
        refs_checked += 1
        # Unfiltered lookup so we can distinguish "name not declared anywhere"
        # from "name declared but with wrong kind". graph.resolve() applies
        # the expected_kind filter, which collapses both cases into [].
        all_decls_for_name = graph.decls_by_name.get(ref.name, [])
        if not all_decls_for_name:
            violations.append(RefValidityViolation(ref=ref, reason="no_decl_found"))
            continue
        if ref.expected_kind is not None:
            kind_match = any(d.kind == ref.expected_kind for d in all_decls_for_name)
            if not kind_match:
                violations.append(RefValidityViolation(ref=ref, reason="kind_mismatch"))

    return RefValidityReport(
        passed=len(violations) == 0,
        violations=tuple(violations),
        refs_checked=refs_checked,
        files_covered=len(files_with_extractors),
        files_skipped=0,  # Will be set by caller/pipeline; gate doesn't track skips
    )
