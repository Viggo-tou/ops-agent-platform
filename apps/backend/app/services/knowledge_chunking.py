from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

ChunkKind = Literal["function", "method", "class", "module", "line_window"]


@dataclass(frozen=True)
class SymbolRange:
    start_line: int
    end_line: int
    enclosing_symbol: str | None
    chunk_kind: ChunkKind


@dataclass(frozen=True)
class SnippetChunk:
    line_start: int
    line_end: int
    snippet: str
    enclosing_symbol: str | None
    chunk_kind: ChunkKind
    truncated: bool


PYTHON_SYMBOL_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
JS_CLASS_TYPES = {"class", "class_declaration"}
JS_METHOD_TYPES = {"method_definition", "method_signature", "abstract_method_signature"}
JS_FUNCTION_TYPES = {
    "function",
    "function_declaration",
    "generator_function",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
}
JS_SYMBOL_TYPES = JS_CLASS_TYPES | JS_METHOD_TYPES | JS_FUNCTION_TYPES
AST_EXTENSIONS = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}


def build_snippet(
    *,
    content: str,
    extension: str,
    target_line: int,
    min_lines: int = 5,
    max_lines: int = 150,
    fallback_radius: int = 10,
) -> SnippetChunk:
    lines = content.splitlines() or [content]
    total_lines = len(lines)
    bounded_target = _clamp(target_line, 1, total_lines)
    min_lines = max(1, min_lines)
    max_lines = max(1, max_lines)

    symbol_range = extract_enclosing_symbol(
        content=content,
        extension=extension,
        target_line=bounded_target,
    )
    if symbol_range is None:
        start, end = _expand_range(
            start_line=max(1, bounded_target - max(0, fallback_radius)),
            end_line=min(total_lines, bounded_target + max(0, fallback_radius)),
            target_line=bounded_target,
            total_lines=total_lines,
            min_lines=min_lines,
        )
        full_start, full_end = start, end
        start, end, truncated = _cap_range(
            start_line=start,
            end_line=end,
            target_line=bounded_target,
            max_lines=max_lines,
        )
        snippet = _join_lines(lines, start, end)
        if truncated:
            snippet = _append_truncation_marker(
                snippet=snippet,
                symbol_name=None,
                full_start=full_start,
                full_end=full_end,
            )
        return SnippetChunk(
            line_start=start,
            line_end=end,
            snippet=snippet,
            enclosing_symbol=None,
            chunk_kind="line_window",
            truncated=truncated,
        )

    start, end = _expand_range(
        start_line=symbol_range.start_line,
        end_line=symbol_range.end_line,
        target_line=bounded_target,
        total_lines=total_lines,
        min_lines=min_lines,
    )
    start, end, truncated = _cap_range(
        start_line=start,
        end_line=end,
        target_line=bounded_target,
        max_lines=max_lines,
    )
    snippet = _join_lines(lines, start, end)
    if truncated:
        snippet = _append_truncation_marker(
            snippet=snippet,
            symbol_name=symbol_range.enclosing_symbol,
            full_start=symbol_range.start_line,
            full_end=symbol_range.end_line,
        )

    return SnippetChunk(
        line_start=start,
        line_end=end,
        snippet=snippet,
        enclosing_symbol=symbol_range.enclosing_symbol,
        chunk_kind=symbol_range.chunk_kind,
        truncated=truncated,
    )


def extract_enclosing_symbol(
    *,
    content: str,
    extension: str,
    target_line: int,
) -> SymbolRange | None:
    normalized_extension = extension.lower()
    if normalized_extension not in AST_EXTENSIONS:
        return None
    lines = content.splitlines() or [content]
    total_lines = len(lines)
    bounded_target = _clamp(target_line, 1, total_lines)

    if normalized_extension == ".py":
        return _extract_python_symbol(content, bounded_target, total_lines)
    return _extract_tree_sitter_symbol(
        content=content,
        extension=normalized_extension,
        target_line=bounded_target,
        total_lines=total_lines,
    )


def _extract_python_symbol(content: str, target_line: int, total_lines: int) -> SymbolRange | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, PYTHON_SYMBOL_TYPES):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None:
            continue
        if start <= target_line <= end:
            candidates.append(node)

    if not candidates:
        return SymbolRange(1, total_lines, None, "module")

    node = min(
        candidates,
        key=lambda item: (
            int(getattr(item, "end_lineno", total_lines)) - int(getattr(item, "lineno", 1)),
            int(getattr(item, "lineno", 1)),
        ),
    )
    start = int(getattr(node, "lineno", 1))
    end = int(getattr(node, "end_lineno", start))

    if isinstance(node, ast.ClassDef):
        return SymbolRange(start, end, f"class {node.name}", "class")

    kind: ChunkKind = "method" if _has_python_class_parent(node, parents) else "function"
    return SymbolRange(start, end, getattr(node, "name", None), kind)


def _has_python_class_parent(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            return True
        parent = parents.get(parent)
    return False


def _extract_tree_sitter_symbol(
    *,
    content: str,
    extension: str,
    target_line: int,
    total_lines: int,
) -> SymbolRange | None:
    try:
        from tree_sitter import Parser
    except Exception:  # noqa: BLE001
        return None

    language = _load_tree_sitter_language(extension)
    if language is None:
        return None

    try:
        parser = Parser(language)
        source_bytes = content.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception:  # noqa: BLE001
        return None

    root = tree.root_node
    if getattr(root, "has_error", False):
        return None

    candidates: list[object] = []

    def visit(node: object) -> None:
        start_line = int(node.start_point[0]) + 1
        end_line = int(node.end_point[0]) + 1
        if start_line <= target_line <= end_line:
            if node.type in JS_SYMBOL_TYPES:
                candidates.append(node)
            for child in node.children:
                visit(child)

    visit(root)
    if not candidates:
        return SymbolRange(1, total_lines, None, "module")

    node = min(
        candidates,
        key=lambda item: (
            int(item.end_point[0]) - int(item.start_point[0]),
            int(item.start_point[0]),
        ),
    )
    start = int(node.start_point[0]) + 1
    end = int(node.end_point[0]) + 1
    kind = _tree_sitter_kind(node)
    symbol_name = _tree_sitter_symbol_name(node, source_bytes)
    if kind == "class" and symbol_name and not symbol_name.startswith("class "):
        symbol_name = f"class {symbol_name}"
    return SymbolRange(start, end, symbol_name, kind)


def _load_tree_sitter_language(extension: str) -> object | None:
    from tree_sitter import Language

    try:
        if extension in {".js", ".jsx", ".mjs", ".cjs"}:
            import tree_sitter_javascript

            return Language(tree_sitter_javascript.language())
        if extension == ".ts":
            import tree_sitter_typescript

            return Language(tree_sitter_typescript.language_typescript())
        if extension == ".tsx":
            import tree_sitter_typescript

            return Language(tree_sitter_typescript.language_tsx())
    except Exception:  # noqa: BLE001
        return None
    return None


def _tree_sitter_kind(node: object) -> ChunkKind:
    if node.type in JS_CLASS_TYPES:
        return "class"
    if node.type in JS_METHOD_TYPES:
        return "method"
    return "function"


def _tree_sitter_symbol_name(node: object, source_bytes: bytes) -> str | None:
    name_node = _child_by_field_name(node, "name")
    if name_node is not None:
        return _node_text(name_node, source_bytes)

    parent = getattr(node, "parent", None)
    while parent is not None:
        if parent.type in {"variable_declarator", "assignment_expression"}:
            field_name = "name" if parent.type == "variable_declarator" else "left"
            field_node = _child_by_field_name(parent, field_name)
            if field_node is not None:
                return _node_text(field_node, source_bytes)
        if parent.type in {"pair", "property_signature", "public_field_definition", "field_definition"}:
            field_node = _child_by_field_name(parent, "key") or _child_by_field_name(parent, "name")
            if field_node is not None:
                return _node_text(field_node, source_bytes)
        parent = getattr(parent, "parent", None)
    return None


def _child_by_field_name(node: object, field_name: str) -> object | None:
    try:
        return node.child_by_field_name(field_name)
    except Exception:  # noqa: BLE001
        return None


def _node_text(node: object, source_bytes: bytes) -> str | None:
    try:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8").strip()
    except Exception:  # noqa: BLE001
        return None


def _expand_range(
    *,
    start_line: int,
    end_line: int,
    target_line: int,
    total_lines: int,
    min_lines: int,
) -> tuple[int, int]:
    start = _clamp(start_line, 1, total_lines)
    end = _clamp(end_line, start, total_lines)
    while end - start + 1 < min_lines and (start > 1 or end < total_lines):
        if start > 1:
            start -= 1
        if end - start + 1 >= min_lines:
            break
        if end < total_lines:
            end += 1
    if not (start <= target_line <= end):
        start = min(start, target_line)
        end = max(end, target_line)
    return start, end


def _cap_range(
    *,
    start_line: int,
    end_line: int,
    target_line: int,
    max_lines: int,
) -> tuple[int, int, bool]:
    if end_line - start_line + 1 <= max_lines:
        return start_line, end_line, False

    capped_start = start_line
    capped_end = start_line + max_lines - 1
    if target_line > capped_end:
        half_window = max_lines // 2
        capped_start = max(start_line, target_line - half_window)
        capped_end = capped_start + max_lines - 1
        if capped_end > end_line:
            capped_end = end_line
            capped_start = max(start_line, capped_end - max_lines + 1)
    return capped_start, capped_end, True


def _join_lines(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1:end_line]).strip()


def _append_truncation_marker(
    *,
    snippet: str,
    symbol_name: str | None,
    full_start: int,
    full_end: int,
) -> str:
    label = symbol_name or "module"
    marker = f"... [truncated, full symbol: {label}, lines {full_start}-{full_end}]"
    return f"{snippet.rstrip()}\n{marker}".strip()


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))
