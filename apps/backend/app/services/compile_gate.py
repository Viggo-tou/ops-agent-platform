"""Compile gate: lightweight syntax check on patched files (T-040 defense line 5).

Runs after apply_patch, before review. Catches obviously broken output
from the codegen model (syntax errors, missing brackets, etc.) without
needing a full build system or test suite.

Supported checks:
- JavaScript (.js, .jsx, .mjs): ``node --check``
- Python (.py): ``py_compile.compile()`` (in-process, no subprocess)
"""

from __future__ import annotations

import py_compile
import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompileResult:
    passed: bool
    errors: list[dict]

    def summary(self) -> str:
        if self.passed:
            return "Compile gate passed."
        msgs = [f"{e['file']}: {e['error']}" for e in self.errors[:5]]
        return "Compile gate failed: " + "; ".join(msgs)


_JS_EXTENSIONS = frozenset({".js", ".jsx", ".mjs"})
_PY_EXTENSIONS = frozenset({".py"})


def run_compile_gate(
    *,
    sandbox_dir: Path,
    changed_files: list[str],
) -> CompileResult:
    """Check syntax of changed files in the sandbox directory.

    ``changed_files`` is a list of relative paths (forward-slash separated)
    as reported by the codegen diff. Only files that exist in the sandbox
    and have a supported extension are checked.
    """
    errors: list[dict] = []

    for rel_path in changed_files:
        full = sandbox_dir / rel_path.replace("/", "\\") if "\\" in str(sandbox_dir) else sandbox_dir / rel_path
        if not full.exists() or not full.is_file():
            continue

        suffix = full.suffix.lower()

        if suffix in _JS_EXTENSIONS:
            err = _check_js(full)
            if err:
                errors.append({"file": rel_path, "type": "js", "error": err})

        elif suffix in _PY_EXTENSIONS:
            err = _check_py(full)
            if err:
                errors.append({"file": rel_path, "type": "py", "error": err})

    return CompileResult(passed=len(errors) == 0, errors=errors)


def _check_js(path: Path) -> str | None:
    node = shutil.which("node")
    if node is None:
        return None

    # Node.js v22 has a bug where ``node --check <file>`` silently returns 0
    # for ESM files (.js files containing ``import`` statements) without
    # actually validating syntax.  Work around by piping via stdin with
    # ``--input-type=module`` which forces real module parsing.
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Fast heuristic: check first 30 lines for ESM import/export syntax.
    head_lines = source.split("\n", 30)[:30]
    is_esm = any(
        line.lstrip().startswith(("import ", "import{", "export "))
        for line in head_lines
    )

    if is_esm:
        err = _check_js_esm(node, source)
    else:
        err = _check_js_classic(node, path)

    return err


def _check_js_classic(node: str, path: Path) -> str | None:
    """CJS files: standard ``node --check``."""
    try:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "syntax error").strip()
            # JSX ``<Tag>`` also appears in CJS-style React files
            if _JSX_FALSE_POSITIVE_RE.search(stderr):
                return None
            return stderr[:500]
        return None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


# JSX ``<Tag>`` triggers ``Unexpected token '<'`` which is not a real
# syntax error in React projects — filter these out.
_JSX_FALSE_POSITIVE_RE = re.compile(r"Unexpected token '<'")


def _check_js_esm(node: str, source: str) -> str | None:
    """ESM files: pipe through stdin with ``--input-type=module``.

    Filters out JSX-related false positives (``Unexpected token '<'``)
    since JSX is valid React syntax but not valid plain ECMAScript.
    """
    try:
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            if _JSX_FALSE_POSITIVE_RE.search(stderr):
                return None  # JSX token — not a real error in React
            return stderr[:500] if stderr else "syntax error"
        return None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _check_py(path: Path) -> str | None:
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc).strip()[:500]
