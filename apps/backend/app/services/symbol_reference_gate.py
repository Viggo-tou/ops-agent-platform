"""Symbol + reference gate: verify diff modifies both definitions and references.

T-041-05: When the diff changes a symbol definition (function, class, variable,
import), verify that references to that symbol are also addressed. A patch that
renames a function but doesn't update its callers is incomplete.

This is a lightweight, regex-based analysis (no AST parser dependency).
It works across JS/TS/Python/Kotlin/Java by scanning for common patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SymbolFinding:
    rule: str
    severity: str
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SymbolReport:
    verdict: str  # "pass" | "warn"
    findings: tuple[SymbolFinding, ...]

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "message": f.message, "evidence": f.evidence}
                for f in self.findings
            ],
        }


_DEF_PATTERNS = [
    re.compile(r"^[-]\s*(?:export\s+)?(?:function|const|let|var|class)\s+(\w+)", re.M),
    re.compile(r"^[-]\s*(?:def|class)\s+(\w+)", re.M),
    re.compile(r"^[-]\s*(?:fun|val|var|class|object)\s+(\w+)", re.M),
    re.compile(r"^[-]\s*(?:public|private|protected)?\s*(?:static\s+)?(?:void|int|String|boolean|class)\s+(\w+)", re.M),
]

_NEW_DEF_PATTERNS = [
    re.compile(r"^[+]\s*(?:export\s+)?(?:function|const|let|var|class)\s+(\w+)", re.M),
    re.compile(r"^[+]\s*(?:def|class)\s+(\w+)", re.M),
    re.compile(r"^[+]\s*(?:fun|val|var|class|object)\s+(\w+)", re.M),
    re.compile(r"^[+]\s*(?:public|private|protected)?\s*(?:static\s+)?(?:void|int|String|boolean|class)\s+(\w+)", re.M),
]

_IMPORT_PATTERN = re.compile(
    r"^[-]\s*(?:import|from|require)\s+.*?['\"]([^'\"]+)['\"]|"
    r"^[-]\s*(?:import|from)\s+(\w+)",
    re.M,
)

_MIN_SYMBOL_LEN = 4
_IGNORE_SYMBOLS = frozenset({
    "self", "this", "true", "false", "null", "none", "undefined",
    "return", "import", "export", "from", "class", "function",
    "const", "void", "static", "public", "private",
})


def check_symbol_references(
    *,
    diff: str,
    source_tree: Path | None,
) -> SymbolReport:
    """Check that removed/renamed symbol definitions have their references updated."""
    findings: list[SymbolFinding] = []

    if not diff.strip() or source_tree is None or not source_tree.exists():
        return SymbolReport(verdict="pass", findings=())

    removed_symbols = _extract_removed_symbols(diff)
    added_symbols = _extract_added_symbols(diff)

    renamed_pairs: list[tuple[str, str]] = []
    orphaned_removals: list[str] = []

    for sym in removed_symbols:
        candidates = [a for a in added_symbols if _looks_like_rename(sym, a)]
        if candidates:
            renamed_pairs.append((sym, candidates[0]))
        else:
            if _symbol_has_references_in_tree(sym, source_tree, diff):
                orphaned_removals.append(sym)

    for old_name, new_name in renamed_pairs:
        refs_in_tree = _count_references_in_tree(old_name, source_tree)
        refs_updated_in_diff = _count_reference_updates_in_diff(old_name, new_name, diff)
        if refs_in_tree > 0 and refs_updated_in_diff == 0:
            findings.append(SymbolFinding(
                rule="symbol_rename_orphan",
                severity="warn",
                message=(
                    f"Symbol '{old_name}' renamed to '{new_name}' but "
                    f"{refs_in_tree} reference(s) in source tree not updated in diff."
                ),
                evidence={
                    "old_name": old_name,
                    "new_name": new_name,
                    "refs_in_tree": refs_in_tree,
                    "refs_updated_in_diff": refs_updated_in_diff,
                },
            ))

    for sym in orphaned_removals:
        refs = _count_references_in_tree(sym, source_tree)
        if refs > 2:
            findings.append(SymbolFinding(
                rule="symbol_removal_dangling",
                severity="warn",
                message=(
                    f"Symbol '{sym}' definition removed but {refs} reference(s) "
                    f"remain in the source tree and are not addressed in the diff."
                ),
                evidence={"symbol": sym, "refs_remaining": refs},
            ))

    verdict = "warn" if findings else "pass"
    return SymbolReport(verdict=verdict, findings=tuple(findings))


def _extract_removed_symbols(diff: str) -> set[str]:
    symbols: set[str] = set()
    for pattern in _DEF_PATTERNS:
        for m in pattern.finditer(diff):
            sym = m.group(1)
            if sym and len(sym) >= _MIN_SYMBOL_LEN and sym.lower() not in _IGNORE_SYMBOLS:
                symbols.add(sym)
    return symbols


def _extract_added_symbols(diff: str) -> set[str]:
    symbols: set[str] = set()
    for pattern in _NEW_DEF_PATTERNS:
        for m in pattern.finditer(diff):
            sym = m.group(1)
            if sym and len(sym) >= _MIN_SYMBOL_LEN and sym.lower() not in _IGNORE_SYMBOLS:
                symbols.add(sym)
    return symbols


def _looks_like_rename(old: str, new: str) -> bool:
    if old == new:
        return False
    old_l, new_l = old.lower(), new.lower()
    if old_l in new_l or new_l in old_l:
        return True
    common = len(set(old_l) & set(new_l))
    total = len(set(old_l) | set(new_l))
    return total > 0 and common / total > 0.6


def _symbol_has_references_in_tree(symbol: str, source_tree: Path, diff: str) -> bool:
    return _count_references_in_tree(symbol, source_tree) > 1


def _count_references_in_tree(symbol: str, source_tree: Path) -> int:
    from app.services.spec_conformance import _find_files_containing_anchor
    hits = _find_files_containing_anchor(source_tree, symbol)
    return sum(hits.values())


def _count_reference_updates_in_diff(old_name: str, new_name: str, diff: str) -> int:
    count = 0
    for line in diff.splitlines():
        if line.startswith("-") and old_name in line and not line.startswith("---"):
            count += 1
        if line.startswith("+") and new_name in line and not line.startswith("+++"):
            count += 1
    return count
