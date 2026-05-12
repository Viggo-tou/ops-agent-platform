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
import sys
from app.services.symbol_graph.protocol import (
    Decl, Ref, ExtractedSymbols, SymbolExtractor,
)
from app.services.symbol_graph.registry import register_extractor


# Python stdlib + commonly-installed third-party packages whose decls
# live outside the project source tree. Imports starting with any of
# these names produce no Ref — same rationale as the Kotlin extractor's
# external-import filter (see kotlin_extractor._EXTERNAL_IMPORT_PREFIXES).
# Symbol-graph ref-validity is for *internal* cross-file consistency,
# not "does this package exist somewhere on PYTHONPATH".
_STDLIB_TOP_LEVEL: frozenset[str] = frozenset({
    *getattr(sys, "stdlib_module_names", set()),
    # py3.10+ has stdlib_module_names; for safety include common names:
    "abc", "argparse", "ast", "asyncio", "base64", "collections",
    "concurrent", "contextlib", "copy", "csv", "dataclasses", "datetime",
    "decimal", "enum", "errno", "fnmatch", "functools", "glob", "hashlib",
    "hmac", "http", "importlib", "inspect", "io", "ipaddress", "itertools",
    "json", "logging", "math", "multiprocessing", "operator", "os",
    "pathlib", "pickle", "platform", "queue", "random", "re", "shlex",
    "shutil", "signal", "socket", "sqlite3", "ssl", "stat", "string",
    "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "traceback", "types", "typing", "unittest", "urllib", "uuid",
    "warnings", "weakref", "xml", "zipfile", "zlib",
})

_THIRD_PARTY_TOP_LEVEL: frozenset[str] = frozenset({
    "fastapi", "pydantic", "sqlalchemy", "starlette", "uvicorn",
    "httpx", "requests", "aiohttp",
    "pytest", "pytest_asyncio",
    "numpy", "pandas", "scipy", "matplotlib", "torch", "tensorflow",
    "sklearn", "PIL", "lxml",
    "yaml", "toml", "click", "rich",
    "redis", "kafka",
    "anthropic", "openai",
    "tree_sitter", "tree_sitter_kotlin", "tree_sitter_xml",
    "tree_sitter_python",
})


def _is_external_python_import(top_level: str) -> bool:
    """True if a Python import targets a module outside the project tree.
    The check uses the *top-level* package name only (e.g. for
    `from app.services.foo import bar`, the top-level is `app`).
    """
    if not top_level:
        return False
    return (top_level in _STDLIB_TOP_LEVEL) or (top_level in _THIRD_PARTY_TOP_LEVEL)


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
                    top = alias.name.split(".", 1)[0]
                    if _is_external_python_import(top):
                        continue
                    refs.append(Ref(name=alias.name, expected_kind="module",
                                    file=path, line=node.lineno))
            elif isinstance(node, ast.ImportFrom):
                # `from a.b import c` -> ref to c (kind unknown).
                # We DON'T track the module path itself here — too many
                # false positives; resolving `a.b` requires sys.path
                # semantics. External-package imports are skipped entirely.
                top = (node.module or "").split(".", 1)[0]
                if _is_external_python_import(top):
                    continue
                for alias in node.names:
                    refs.append(Ref(name=alias.name, expected_kind=None,
                                    file=path, line=node.lineno))

        return ExtractedSymbols(decls=tuple(decls), refs=tuple(refs))


# Auto-register on module import
register_extractor("py", PythonExtractor())
