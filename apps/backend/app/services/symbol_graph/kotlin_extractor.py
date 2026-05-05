"""Kotlin plug-in for SymbolExtractor (tree-sitter backed).

Extracts:
  - decls: class / object / function / property declarations.
           Decl.name = the declared identifier.
           Decl.kind = "class" / "object" / "function" / "variable".
  - refs:  import statements. The last segment of `import a.b.c.Foo`
           becomes Ref(name="Foo", expected_kind="import").
           We do NOT track every Identifier usage in function bodies —
           too noisy and would explode the graph; imports alone catch
           the most actionable cross-file dependency class.

Auto-registers itself for ``.kt`` and ``.kts`` on module import.
"""
from __future__ import annotations

import logging

from app.services.symbol_graph.protocol import (
    Decl,
    ExtractedSymbols,
    Ref,
)
from app.services.symbol_graph.registry import register_extractor

logger = logging.getLogger(__name__)


# Known external (SDK / library) package prefixes — imports starting with
# any of these are NOT emitted as Refs because their decls live outside
# the project source tree (in jars / aar / kotlin stdlib). Without this
# filter the gate produces 50+ false positives per Android Compose file.
#
# This is the conservative list. Anything not matching falls through and
# IS emitted as a Ref — so internal-project cross-file imports
# (com.example.app.utils.* -> com.example.app.foo.Bar) still get checked.
_EXTERNAL_IMPORT_PREFIXES: tuple[str, ...] = (
    "android.",
    "androidx.",
    "com.google.",
    "com.android.",
    "kotlin.",
    "kotlinx.",
    "java.",
    "javax.",
    "org.json.",
    "org.jetbrains.",
    "org.junit.",
    "org.mockito.",
    "org.osmdroid.",
    "dagger.",
    "retrofit2.",
    "okhttp3.",
    "io.reactivex.",
    "rxjava.",
    "io.kotest.",
    "io.mockk.",
)


def _is_external_import(qualified_path: str) -> bool:
    """Return True when a Kotlin import's qualified path points to an
    SDK / library package outside the project. Such imports can never
    resolve to a Decl in the project source tree, so the gate skips
    them to avoid false positives."""
    return any(qualified_path.startswith(p) for p in _EXTERNAL_IMPORT_PREFIXES)


def _load_parser():
    """Build a tree-sitter Kotlin parser. Imported lazily so module
    import doesn't crash environments without tree-sitter."""
    import tree_sitter_kotlin as _ts_kt  # type: ignore
    from tree_sitter import Language, Parser  # type: ignore
    return Parser(Language(_ts_kt.language()))


class KotlinExtractor:
    def __init__(self) -> None:
        self._parser = None  # lazy

    @property
    def language(self) -> str:
        return "kotlin"

    def _get_parser(self):
        if self._parser is None:
            self._parser = _load_parser()
        return self._parser

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        try:
            parser = self._get_parser()
        except Exception as exc:  # noqa: BLE001
            logger.warning("kotlin extractor parser unavailable: %s", exc)
            return ExtractedSymbols(decls=(), refs=())
        try:
            tree = parser.parse(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kotlin parse failed for %s: %s", path, exc)
            return ExtractedSymbols(decls=(), refs=())

        decls: list[Decl] = []
        refs: list[Ref] = []
        self._walk(tree.root_node, source, path, decls, refs)
        return ExtractedSymbols(decls=tuple(decls), refs=tuple(refs))

    def _walk(self, node, source: bytes, path: str,
              decls: list[Decl], refs: list[Ref]) -> None:
        # Tree-sitter-kotlin grammar (1.1.0) node types we care about.
        kt = node.type
        if kt == "class_declaration":
            ident = self._find_named_identifier(node)
            if ident is not None:
                # Object vs class: tree-sitter-kotlin uses "class_declaration"
                # for both `class Foo` and `object Foo`. The leading keyword
                # text disambiguates.
                kind = "object" if self._first_keyword_is(node, source, b"object") else "class"
                decls.append(Decl(
                    name=self._text(ident, source),
                    kind=kind,
                    file=path,
                    line=ident.start_point[0] + 1,
                ))
        elif kt == "function_declaration":
            ident = self._find_named_identifier(node)
            if ident is not None:
                decls.append(Decl(
                    name=self._text(ident, source),
                    kind="function",
                    file=path,
                    line=ident.start_point[0] + 1,
                ))
        elif kt == "property_declaration":
            # Top-level `val x = ...` / `var x = ...` declarations.
            # tree-sitter-kotlin wraps the name in a `variable_declaration`
            # child (which itself contains the identifier).
            ident = None
            vd = self._first_child_of_type(node, "variable_declaration")
            if vd is not None:
                ident = self._find_named_identifier(vd)
            if ident is None:
                ident = self._find_named_identifier(node)
            if ident is not None:
                decls.append(Decl(
                    name=self._text(ident, source),
                    kind="variable",
                    file=path,
                    line=ident.start_point[0] + 1,
                ))
        elif kt == "import":
            # `import a.b.c.Foo` -> Ref to last segment "Foo" with
            # expected_kind=None (accept any decl kind).
            # External SDK / library imports (android.*, androidx.*,
            # kotlin.*, java.*, etc.) are skipped — their decls live
            # outside the project source tree and can never resolve.
            qid = self._first_child_of_type(node, "qualified_identifier")
            if qid is not None:
                qualified = self._text(qid, source)
                if not _is_external_import(qualified):
                    last_ident = self._last_child_of_type(qid, "identifier")
                    if last_ident is not None:
                        refs.append(Ref(
                            name=self._text(last_ident, source),
                            expected_kind=None,
                            file=path,
                            line=last_ident.start_point[0] + 1,
                            metadata={"qualified": qualified},
                        ))
            # Don't recurse into import body
            return

        # Descend (skip recursion when we already handled the structure
        # by `return` above).
        for c in node.children:
            self._walk(c, source, path, decls, refs)

    @staticmethod
    def _text(node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _find_named_identifier(node):
        """Return the first 'identifier' child that names this declaration.
        For class_declaration / function_declaration / property_declaration
        the first 'identifier' child after keywords is the declared name."""
        for c in node.children:
            if c.type == "identifier":
                return c
            if c.type == "simple_identifier":
                return c
        return None

    @staticmethod
    def _first_child_of_type(node, type_name: str):
        for c in node.children:
            if c.type == type_name:
                return c
        return None

    @staticmethod
    def _last_child_of_type(node, type_name: str):
        last = None
        for c in node.children:
            if c.type == type_name:
                last = c
        return last

    @staticmethod
    def _first_keyword_is(node, source: bytes, keyword: bytes) -> bool:
        for c in node.children:
            if c.type == keyword.decode():
                return True
            # Stop scanning once we pass the keyword zone (after first identifier)
            if c.type in ("identifier", "simple_identifier"):
                return False
        return False


# Auto-register on module import for both `.kt` and `.kts`.
_inst = KotlinExtractor()
register_extractor("kt", _inst)
register_extractor("kts", _inst)
