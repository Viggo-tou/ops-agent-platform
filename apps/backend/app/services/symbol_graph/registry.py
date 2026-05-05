from __future__ import annotations
from pathlib import Path
from app.services.symbol_graph.protocol import SymbolExtractor

_REGISTRY: dict[str, SymbolExtractor] = {}


def register_extractor(extension: str, extractor: SymbolExtractor) -> None:
    """Register extractor for a file extension (lowercase, no leading dot).
    Overwrites any existing registration for the same extension."""
    norm = extension.lower().lstrip(".")
    if not norm:
        raise ValueError("extension must be a non-empty string")
    if not isinstance(extractor, SymbolExtractor):
        raise TypeError(f"{extractor!r} does not satisfy SymbolExtractor protocol")
    _REGISTRY[norm] = extractor


def get_extractor_for_path(path: str) -> SymbolExtractor | None:
    """Return the registered extractor for a file path's extension, or None.
    Returning None means: graceful skip — not all files are extractor-covered."""
    ext = Path(path).suffix.lstrip(".").lower()
    return _REGISTRY.get(ext) if ext else None


def registered_extensions() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _clear_registry_for_tests() -> None:
    """Test-only: wipe registry between tests to keep them isolated."""
    _REGISTRY.clear()
