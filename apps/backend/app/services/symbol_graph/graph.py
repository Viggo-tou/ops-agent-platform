from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable
from app.services.symbol_graph.protocol import Decl, Ref, ExtractedSymbols
from app.services.symbol_graph.registry import get_extractor_for_path


@dataclass
class SymbolGraph:
    """Cross-file decl/ref index. Build incrementally: add files one at a time."""
    decls_by_name: dict[str, list[Decl]] = field(default_factory=lambda: defaultdict(list))
    refs_by_name: dict[str, list[Ref]] = field(default_factory=lambda: defaultdict(list))
    decls_by_file: dict[str, list[Decl]] = field(default_factory=lambda: defaultdict(list))
    refs_by_file: dict[str, list[Ref]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def empty(cls) -> "SymbolGraph":
        return cls()

    def ingest(self, *, path: str, symbols: ExtractedSymbols) -> None:
        """Add one file's extracted symbols to the graph."""
        for decl in symbols.decls:
            self.decls_by_name[decl.name].append(decl)
            self.decls_by_file[decl.file].append(decl)
        for ref in symbols.refs:
            self.refs_by_name[ref.name].append(ref)
            self.refs_by_file[ref.file].append(ref)

    def ingest_source(self, *, path: str, source: bytes) -> bool:
        """Convenience: dispatch via registry. Returns False if no extractor
        registered for this file's extension (graceful skip)."""
        extractor = get_extractor_for_path(path)
        if extractor is None:
            return False
        symbols = extractor.extract(path=path, source=source)
        self.ingest(path=path, symbols=symbols)
        return True

    def resolve(self, ref: Ref) -> list[Decl]:
        """Find decls matching the ref. Honors expected_kind if set; else any kind."""
        candidates = self.decls_by_name.get(ref.name, [])
        if ref.expected_kind is None:
            return list(candidates)
        return [d for d in candidates if d.kind == ref.expected_kind]

    def reverse_dependents(self, decl: Decl) -> list[Ref]:
        """Files/refs that depend on this decl (blast radius helper)."""
        results: list[Ref] = []
        for ref in self.refs_by_name.get(decl.name, []):
            results.append(ref)
        return results
