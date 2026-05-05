"""SymbolGraph framework: language-agnostic cross-file reference validity gate.

Public API re-exported for convenience. Import the submodules directly
if you need fine-grained access.
"""

from __future__ import annotations

from app.services.symbol_graph.protocol import (
    Decl,
    ExtractedSymbols,
    Ref,
    SymbolExtractor,
)
from app.services.symbol_graph.graph import SymbolGraph
from app.services.symbol_graph.registry import (
    get_extractor_for_path,
    register_extractor,
    registered_extensions,
)
from app.services.symbol_graph.gate import (
    RefValidityReport,
    RefValidityViolation,
    validate_refs,
)

__all__ = [
    "Decl",
    "ExtractedSymbols",
    "Ref",
    "SymbolExtractor",
    "SymbolGraph",
    "get_extractor_for_path",
    "register_extractor",
    "registered_extensions",
    "RefValidityReport",
    "RefValidityViolation",
    "validate_refs",
]
