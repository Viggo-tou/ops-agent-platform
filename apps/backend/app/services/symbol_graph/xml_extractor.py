"""XML plug-in for SymbolExtractor (Android resources focused).

Uses lxml — no tree-sitter dependency for XML since the Android
resource grammar is small and well-served by XPath.

Extracts Android resource decls + refs:
  decls: top-level resource elements with a `name` attribute under
         <resources>, e.g. <string name="hello">, <color name="primary"/>,
         <drawable name="logo"/>, <style name="MyTheme"/>, <dimen ...>.
         Decl.kind = the element tag ("string" | "color" | "drawable"
         | "style" | "dimen" | "id" | "bool" | "integer" | "array").
  refs:  every ``@<type>/<name>`` reference inside attribute values or
         text nodes, e.g. ``android:text="@string/welcome"`` -> Ref(
         name="welcome", expected_kind="string").
         Also catches ``@+id/foo`` (id declarations) which we treat as
         a Decl with kind="id" (Android's special on-the-fly id syntax).

Auto-registers itself for ``.xml`` on module import. Note: this is
intentionally generic — works for any Android module's res files,
strings.xml, layouts, drawables, navigation graphs, manifests.
"""
from __future__ import annotations

import logging
import re

from app.services.symbol_graph.protocol import (
    Decl,
    ExtractedSymbols,
    Ref,
)
from app.services.symbol_graph.registry import register_extractor

logger = logging.getLogger(__name__)


# `@string/name`, `@drawable/name`, `@+id/name`, `@android:string/name`...
# We accept letters, digits, underscores in both type and name.
_REF_RE = re.compile(r"@(?:android:)?(?:\+)?(?P<type>[A-Za-z_]\w*)/(?P<name>[A-Za-z_]\w*)")
_DECL_RE = re.compile(r"@\+id/(?P<name>[A-Za-z_]\w*)")


# Whitelist of resource element tags that are "decl-shaped" — i.e. they
# define a named resource that other XML or code can reference. Skips
# elements that exist for layout structure (LinearLayout, etc.) which
# don't define globally-addressable named resources via `<element name=...>`.
_DECL_TAGS = frozenset({
    "string", "string-array", "plurals",
    "color",
    "drawable",
    "style",
    "dimen",
    "bool",
    "integer", "integer-array",
    "array",
    "attr",
    "fraction",
})


class XmlExtractor:
    @property
    def language(self) -> str:
        return "xml"

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        decls: list[Decl] = []
        refs: list[Ref] = []
        try:
            from lxml import etree  # type: ignore
        except ImportError as exc:
            logger.warning("xml extractor requires lxml: %s", exc)
            return ExtractedSymbols(decls=(), refs=())

        # Decls via lxml — robust for the schema we care about.
        try:
            parser = etree.XMLParser(recover=True)  # tolerate malformed XML
            root = etree.fromstring(source, parser=parser)
        except Exception as exc:  # noqa: BLE001
            logger.debug("xml parse failed for %s: %s", path, exc)
            root = None

        if root is not None:
            for el in root.iter():
                tag = etree.QName(el).localname if isinstance(el.tag, str) else None
                if tag is None:
                    continue
                if tag in _DECL_TAGS:
                    name = el.get("name")
                    if name:
                        decls.append(Decl(
                            name=name,
                            kind=tag,
                            file=path,
                            line=el.sourceline or 0,
                            metadata={},
                        ))

        # Refs via regex over the raw bytes — catches both attribute
        # values and text nodes, and works even when lxml's recover-mode
        # discards malformed regions.
        text = source.decode("utf-8", errors="replace")
        for m in _REF_RE.finditer(text):
            ref_type = m.group("type")
            ref_name = m.group("name")
            line = text.count("\n", 0, m.start()) + 1
            refs.append(Ref(
                name=ref_name,
                expected_kind=ref_type,
                file=path,
                line=line,
                metadata={},
            ))
        # `@+id/foo` declares the id on the fly — record as a Decl too.
        for m in _DECL_RE.finditer(text):
            name = m.group("name")
            line = text.count("\n", 0, m.start()) + 1
            decls.append(Decl(
                name=name,
                kind="id",
                file=path,
                line=line,
                metadata={"declared_via": "+id"},
            ))

        return ExtractedSymbols(decls=tuple(decls), refs=tuple(refs))


register_extractor("xml", XmlExtractor())
