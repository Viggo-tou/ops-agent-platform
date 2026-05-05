"""Python plug-in for SymbolExtractor.

Extracts:
  - decls: top-level def, async def, class, top-level assignments
           (kind="function"/"class"/"variable")
  - refs:  imports `from foo.bar import baz` -> Ref(name="baz", expected_kind=None)
           bare imports `import foo` -> Ref(name="foo", expected_kind="module")
           Note: this round we DON'T extract Name() usages — too noisy.
           Imports alone catch the "missing module" / "missing symbol" bug class.

Auto-registers itself via register_extractor("py", PythonExtractor()) at module
import time, so `from app.services.symbol_graph import python_extractor` is the
hook caller uses to enable Python coverage.
"""

from __future__ import annotations
import ast
from app.services.symbol_graph.protocol import (
    Decl, Ref, ExtractedSymbols, SymbolExtractor,
)
from app.services.symbol_graph.registry import register_extractor


class PythonExtractor:
    @property
    def language(self) -> str:
        return "python"

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            # File doesn't parse — return empty rather than crash. The
            # compile_gate will catch real syntax errors separately; we
            # just skip ref-extraction for this one file.
            return ExtractedSymbols(decls=(), refs=())

        decls: list[Decl] = []
        refs: list[Ref] = []

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decls.append(Decl(name=node.name, kind="function", file=path,
                                  line=node.lineno))
            elif isinstance(node, ast.ClassDef):
                decls.append(Decl(name=node.name, kind="class", file=path,
                                  line=node.lineno))
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        decls.append(Decl(name=tgt.id, kind="variable",
                                          file=path, line=node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    refs.append(Ref(name=alias.name, expected_kind="module",
                                    file=path, line=node.lineno))
            elif isinstance(node, ast.ImportFrom):
                # `from a.b import c` -> ref to c (kind unknown)
                # We DON'T track the module path itself here — too many false
                # positives; resolving `a.b` requires sys.path semantics.
                for alias in node.names:
                    refs.append(Ref(name=alias.name, expected_kind=None,
                                    file=path, line=node.lineno))

        return ExtractedSymbols(decls=tuple(decls), refs=tuple(refs))


# Auto-register on module import
register_extractor("py", PythonExtractor())
