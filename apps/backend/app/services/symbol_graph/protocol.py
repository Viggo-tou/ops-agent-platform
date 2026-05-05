from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Decl:
    """A name declared in a file that other files may reference."""
    name: str           # canonical name (e.g. "homeAddress", "MyClass.foo")
    kind: str           # extractor-defined: "function" | "class" | "import" | "resource" | ...
    file: str           # workdir-relative POSIX path
    line: int = 0       # 1-indexed; 0 means "unknown" (extractor couldn't pin a line)
    metadata: dict = field(default_factory=dict)  # extractor-specific extras


@dataclass(frozen=True)
class Ref:
    """A name used in a file that should resolve to a Decl somewhere."""
    name: str
    expected_kind: str | None  # None = any kind acceptable; else must match Decl.kind
    file: str
    line: int = 0       # 1-indexed; 0 means "unknown"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedSymbols:
    decls: tuple[Decl, ...]
    refs: tuple[Ref, ...]


@runtime_checkable
class SymbolExtractor(Protocol):
    """Pluggable per-language extractor. Implementations register themselves
    via registry.register_extractor(extension, extractor).
    """
    @property
    def language(self) -> str: ...

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols: ...
