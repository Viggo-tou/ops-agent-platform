"""AST-aware structural truncation for Python source (Tier 2).

Why this exists. ``evidence_pack.truncate_file`` does naive byte
truncation: it keeps the first ``max_per_file_bytes`` bytes and drops
the rest. That's catastrophic for files like ``django/db/models/sql/
query.py`` (2000+ lines) — the truncated head holds imports + class
headers but no function body, so the model has nothing to edit. The
SWE-bench task we hit on 2026-05-09 produced a 0-character diff for
exactly this reason.

This module truncates at AST boundaries instead: keep imports + module-
level constants + class signatures whole; keep small function bodies
whole; replace big function bodies with a placeholder. The output is
syntactically valid Python so further tooling can still parse it.

Public entry point: :func:`truncate_python_source`. Returns a
:class:`TruncationResult` with the rebuilt text + bookkeeping.

Non-Python files: this module declines (returns the source unchanged
with the original size). Callers should fall back to byte truncation.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class TruncationResult:
    text: str
    bytes_kept: int
    symbols_kept_whole: list[str] = field(default_factory=list)
    symbols_truncated: list[str] = field(default_factory=list)
    elided_lines: int = 0
    used_ast: bool = False


# Default: keep any function ≤ this many lines whole, even when no
# explicit keep-list is provided. Empirically chosen — most utility
# helpers and short methods fit; bug fixes nearly always live inside
# something larger that gets pinned via keep_symbols.
_SMALL_BODY_LINES = 30


def truncate_python_source(
    source: str,
    *,
    max_bytes: int,
    keep_symbols: list[str] | None = None,
    small_body_lines: int = _SMALL_BODY_LINES,
) -> TruncationResult:
    """Truncate a Python source string at AST boundaries.

    ``max_bytes`` is a soft target. The algorithm:

    1. If the source already fits, return it unchanged.
    2. Try to parse. If parsing fails, return the source unchanged
       (caller falls back to byte truncation).
    3. For each top-level node, keep imports / assignments / aliases
       whole. For classes, keep the header and small methods whole;
       for big methods, replace body with a placeholder. For module-
       level functions, same rule.
    4. ``keep_symbols`` pins specific function/method names so they're
       always kept whole, even when over ``small_body_lines``.

    The final text may still exceed ``max_bytes`` — when every body
    is already either pinned or small. That's acceptable: an over-
    budget rendering of an actual function body is always more useful
    to the model than an in-budget rendering of imports alone. Caller
    can re-truncate at the byte level as a last resort.
    """
    keep = set(keep_symbols or [])
    encoded = source.encode("utf-8")
    if len(encoded) <= max_bytes:
        return TruncationResult(
            text=source,
            bytes_kept=len(encoded),
            used_ast=False,
        )

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return TruncationResult(
            text=source,
            bytes_kept=len(encoded),
            used_ast=False,
        )

    lines = source.splitlines(keepends=True)
    out_lines: list[str] = []
    kept_whole: list[str] = []
    truncated: list[str] = []
    elided_total = 0
    last_emitted_end = 0  # 1-indexed line; 0 means nothing emitted yet

    def emit_range(start: int, end: int) -> None:
        """Emit lines[start-1:end] (1-indexed, inclusive)."""
        nonlocal last_emitted_end
        if start <= 0 or end <= 0 or start > end:
            return
        # Clip to file bounds.
        start = max(start, 1)
        end = min(end, len(lines))
        # If a gap exists between last emit and start, keep the gap
        # lines too (blank lines, comments between top-level defs).
        if last_emitted_end + 1 < start:
            for ln in lines[last_emitted_end : start - 1]:
                out_lines.append(ln)
        out_lines.extend(lines[start - 1 : end])
        last_emitted_end = end

    def signature_end_line(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
        """Return the last line of the def's signature (the line ending
        with the colon). Body starts on the next line. We approximate
        with the docstring's start - 1 if a docstring exists, else the
        first body statement's line - 1.
        """
        if not func_node.body:
            return func_node.lineno
        first_stmt = func_node.body[0]
        # Signature ends on the line *before* the first body statement.
        return max(func_node.lineno, first_stmt.lineno - 1)

    def emit_truncated_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        indent: str,
    ) -> None:
        """Emit signature + (if present) docstring + a placeholder."""
        nonlocal elided_total
        sig_end = signature_end_line(node)
        emit_range(node.lineno, sig_end)
        # Docstring detection: first body stmt is an Expr(Constant(str)).
        body = node.body or []
        body_start_line = node.body[0].lineno if body else (sig_end + 1)
        body_end_line = node.end_lineno or body_start_line
        had_doc = False
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc_node = body[0]
            doc_end = doc_node.end_lineno or doc_node.lineno
            emit_range(doc_node.lineno, doc_end)
            body_start_line = doc_end + 1
            had_doc = True
        elided = max(0, body_end_line - body_start_line + 1)
        if elided <= 0:
            return
        elided_total += elided
        # Indent the placeholder to match the function body's expected
        # indent level. Use the docstring or first non-doc stmt indent
        # as the source of truth; fall back to indent + 4 spaces.
        if had_doc and len(node.body) > 1:
            sample_line = lines[node.body[1].lineno - 1] if node.body[1].lineno - 1 < len(lines) else ""
        elif body and not had_doc:
            sample_line = lines[body[0].lineno - 1] if body[0].lineno - 1 < len(lines) else ""
        else:
            sample_line = ""
        body_indent = ""
        for ch in sample_line:
            if ch in (" ", "\t"):
                body_indent += ch
            else:
                break
        if not body_indent:
            body_indent = indent + "    "
        out_lines.append(
            f"{body_indent}# ... {elided} line(s) elided by ast_truncate ...\n"
        )
        # If the function body just `pass`'d, emit `pass` so the file
        # still parses cleanly without our placeholder.
        out_lines.append(f"{body_indent}pass\n")
        # Skip past the original body in lines[].
        nonlocal_last(body_end_line)

    def nonlocal_last(end: int) -> None:
        nonlocal last_emitted_end
        last_emitted_end = max(last_emitted_end, end)

    def function_body_line_count(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> int:
        if not node.body:
            return 0
        body_end = node.end_lineno or node.lineno
        body_start = node.body[0].lineno
        return max(0, body_end - body_start + 1)

    def should_keep_whole(
        name: str,
        body_lines: int,
    ) -> bool:
        if name in keep:
            return True
        return body_lines <= small_body_lines

    def handle_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        indent: str,
    ) -> None:
        body_lines = function_body_line_count(node)
        if should_keep_whole(node.name, body_lines):
            emit_range(node.lineno, node.end_lineno or node.lineno)
            kept_whole.append(node.name)
        else:
            emit_truncated_function(node, indent)
            truncated.append(node.name)

    def handle_class(node: ast.ClassDef) -> None:
        # Class header: lineno through the line of the first body stmt - 1.
        if not node.body:
            emit_range(node.lineno, node.end_lineno or node.lineno)
            return
        first = node.body[0]
        header_end = max(node.lineno, first.lineno - 1)
        emit_range(node.lineno, header_end)
        # Walk class body. Keep non-funcdef stmts whole; recurse into
        # methods.
        method_indent = ""
        sample_line = lines[first.lineno - 1] if first.lineno - 1 < len(lines) else ""
        for ch in sample_line:
            if ch in (" ", "\t"):
                method_indent += ch
            else:
                break
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                handle_function(child, method_indent)
            else:
                emit_range(child.lineno, child.end_lineno or child.lineno)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            handle_function(node, indent="")
        elif isinstance(node, ast.ClassDef):
            handle_class(node)
        else:
            emit_range(node.lineno, node.end_lineno or node.lineno)

    # Append any trailing lines (blank lines / comments after the last
    # top-level node).
    if last_emitted_end < len(lines):
        out_lines.extend(lines[last_emitted_end:])

    text = "".join(out_lines)
    return TruncationResult(
        text=text,
        bytes_kept=len(text.encode("utf-8")),
        symbols_kept_whole=kept_whole,
        symbols_truncated=truncated,
        elided_lines=elided_total,
        used_ast=True,
    )
