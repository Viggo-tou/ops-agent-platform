"""Small structural-edit helpers for diagnostic-scoped repair.

This is intentionally not a full AST refactor engine.  The first use case is
compile repair after Kotlin/Compose patches produce parser/scope breakage:
the model proposes a constrained JSON edit plan, and the harness locates and
applies it inside a narrow region before producing the final diff.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Region:
    kind: str
    start_line: int
    end_line: int
    name: str = ""


@dataclass(frozen=True)
class KotlinLocation:
    imports_region: Region
    nearest_function: Region | None
    nearest_block: Region | None
    allowed_region: Region


@dataclass(frozen=True)
class StructuralEditError:
    reason: str
    operation: str = ""


@dataclass
class StructuralEditResult:
    ok: bool
    content: str
    diff: str = ""
    errors: list[StructuralEditError] = field(default_factory=list)
    applied_operations: list[str] = field(default_factory=list)


_FUNC_RE = re.compile(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IMPORT_RE = re.compile(r"^\s*import\s+[A-Za-z_][A-Za-z0-9_.*]*(?:\s+as\s+\w+)?\s*$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_structural_edit_response(text: str) -> dict[str, Any]:
    """Parse model output that should contain exactly one JSON object."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty structural edit response")
    fenced = _JSON_FENCE_RE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("structural edit response does not contain a JSON object")
        raw = raw[start : end + 1]
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("structural edit response must be a JSON object")
    edits = value.get("edits")
    if edits is not None and not isinstance(edits, list):
        raise ValueError("structural edit response field 'edits' must be a list")
    return value


def locate_kotlin_regions(
    source: str,
    *,
    line: int = 0,
    anchor_substring: str = "",
) -> KotlinLocation:
    """Return import/function/block regions around a Kotlin diagnostic line.

    Line numbers are 1-based.  The locator uses text structure first, with a
    tree-sitter-backed validation available separately.  This keeps it useful
    in environments where tree-sitter-kotlin is not installed.
    """
    lines = source.splitlines()
    total = len(lines)
    target_line = _clamp_line(line or _first_anchor_line(lines, anchor_substring) or 1, total)

    imports_region = _imports_region(lines)
    nearest_function = _nearest_function(lines, target_line)
    nearest_block = _nearest_block(lines, target_line)
    allowed = nearest_block or nearest_function or Region("file", 1, total)
    return KotlinLocation(
        imports_region=imports_region,
        nearest_function=nearest_function,
        nearest_block=nearest_block,
        allowed_region=allowed,
    )


def apply_structural_edit_plan(
    *,
    file_path: str,
    original_content: str,
    plan: dict[str, Any],
    protected_symbols: list[str] | None = None,
) -> StructuralEditResult:
    """Apply a constrained structural edit plan to one file in memory."""
    target_file = str(plan.get("file") or file_path).strip()
    if target_file and target_file.replace("\\", "/") != file_path.replace("\\", "/"):
        return StructuralEditResult(
            ok=False,
            content=original_content,
            errors=[StructuralEditError("plan targets a different file")],
        )

    edits = plan.get("edits") or []
    if not isinstance(edits, list):
        return StructuralEditResult(
            ok=False,
            content=original_content,
            errors=[StructuralEditError("plan edits must be a list")],
        )

    content = original_content
    applied: list[str] = []
    errors: list[StructuralEditError] = []
    for raw_edit in edits:
        if not isinstance(raw_edit, dict):
            errors.append(StructuralEditError("edit must be an object"))
            continue
        op = str(raw_edit.get("operation") or "").strip()
        before = content
        if op == "add_import":
            content, err = _op_add_import(content, raw_edit)
        elif op in {"replace_block", "replace_call_expression"}:
            content, err = _op_replace_block(content, raw_edit)
        elif op == "insert_into_function":
            content, err = _op_insert_into_function(content, raw_edit)
        elif op == "wrap_firebase_snapshot_children":
            content, err, _firebase_ops = _repair_firebase_task_shapes(
                content,
                line=int(raw_edit.get("anchor_line") or 0),
            )
        else:
            err = f"unsupported operation: {op or '(blank)'}"
        if err:
            errors.append(StructuralEditError(err, operation=op))
            content = before
            continue
        if content != before:
            applied.append(op)

    validation_errors = validate_kotlin_structure(content)
    for reason in validation_errors:
        errors.append(StructuralEditError(reason, operation="structural_validation"))

    for symbol in protected_symbols or []:
        if symbol and not re.search(r"\b" + re.escape(symbol) + r"\b", content):
            errors.append(
                StructuralEditError(
                    f"protected symbol disappeared: {symbol}",
                    operation="protected_symbols",
                )
            )

    if errors:
        return StructuralEditResult(ok=False, content=original_content, errors=errors)

    diff = _unified_diff(file_path, original_content, content)
    return StructuralEditResult(
        ok=bool(applied) and bool(diff.strip()),
        content=content,
        diff=diff,
        applied_operations=applied,
    )


def apply_kotlin_diagnostic_fast_fixes(
    *,
    file_path: str,
    original_content: str,
    error_text: str = "",
    line: int = 0,
    protected_symbols: list[str] | None = None,
) -> StructuralEditResult | None:
    """Apply deterministic Kotlin repair primitives for common diagnostics.

    These are not task-specific patches. They cover Kotlin/Compose/Firebase
    shapes that are cheap to identify from the current file and safer to fix
    in the harness than by another raw-diff LLM call.
    """
    content = original_content
    applied: list[str] = []
    errors: list[StructuralEditError] = []

    if _should_add_coroutines_launch_import(error_text, content):
        before = content
        content, err = _op_add_import(
            content,
            {"content": "import kotlinx.coroutines.launch"},
        )
        if err:
            errors.append(StructuralEditError(err, operation="add_import"))
        elif content != before:
            applied.append("add_import:kotlinx.coroutines.launch")

    if _should_add_compose_runtime_import(
        error_text,
        content,
        symbol="rememberCoroutineScope",
    ):
        before = content
        content, err = _op_add_import(
            content,
            {"content": "import androidx.compose.runtime.rememberCoroutineScope"},
        )
        if err:
            errors.append(StructuralEditError(err, operation="add_import"))
        elif content != before:
            applied.append("add_import:androidx.compose.runtime.rememberCoroutineScope")

    if _should_add_android_view_import(error_text, content):
        before = content
        content, err = _op_add_import(
            content,
            {"content": "import androidx.compose.ui.viewinterop.AndroidView"},
        )
        if err:
            errors.append(StructuralEditError(err, operation="add_import"))
        elif content != before:
            applied.append("add_import:androidx.compose.ui.viewinterop.AndroidView")

    if _should_repair_missing_try_for_kotlin_catch(error_text, content):
        before = content
        content, err = _repair_missing_try_for_kotlin_catch(content, line=line)
        if err:
            errors.append(
                StructuralEditError(
                    err,
                    operation="insert_missing_try_for_catch",
                )
            )
        elif content != before:
            applied.append("insert_missing_try_for_catch")

    if _should_repair_nullable_geocoder_addresses(error_text, content):
        before = content
        content, err = _repair_nullable_geocoder_addresses(content)
        if err:
            errors.append(
                StructuralEditError(
                    err,
                    operation="make_geocoder_addresses_nullable_safe",
                )
            )
        elif content != before:
            applied.append("make_geocoder_addresses_nullable_safe")

    if _should_repair_lifecycle_owner_in_disposable_effect(error_text, content):
        before = content
        content, err = _repair_lifecycle_owner_in_disposable_effect(content)
        if err:
            errors.append(
                StructuralEditError(
                    err,
                    operation="hoist_lifecycle_owner_from_disposable_effect",
                )
            )
        elif content != before:
            applied.append("hoist_lifecycle_owner_from_disposable_effect")

    if _should_repair_marker_receiver_in_mapview_apply(error_text, content):
        before = content
        content, err = _repair_marker_receiver_in_mapview_apply(content)
        if err:
            errors.append(
                StructuralEditError(
                    err,
                    operation="qualify_marker_receiver_in_mapview_apply",
                )
            )
        elif content != before:
            applied.append("qualify_marker_receiver_in_mapview_apply")

    if _should_repair_firebase_snapshot_children(error_text, content):
        before = content
        content, err, firebase_ops = _repair_firebase_task_shapes(content, line=line)
        if err:
            errors.append(
                StructuralEditError(
                    err,
                    operation="wrap_firebase_snapshot_children",
                )
            )
        elif content != before:
            applied.extend(firebase_ops or ["wrap_firebase_snapshot_children"])

    if not applied and not errors:
        return None

    validation_errors = validate_kotlin_structure(content)
    for reason in validation_errors:
        errors.append(StructuralEditError(reason, operation="structural_validation"))

    for symbol in protected_symbols or []:
        if symbol and not re.search(r"\b" + re.escape(symbol) + r"\b", content):
            errors.append(
                StructuralEditError(
                    f"protected symbol disappeared: {symbol}",
                    operation="protected_symbols",
                )
            )

    if errors:
        return StructuralEditResult(ok=False, content=original_content, errors=errors)

    diff = _unified_diff(file_path, original_content, content)
    return StructuralEditResult(
        ok=bool(diff.strip()),
        content=content,
        diff=diff,
        applied_operations=applied,
    )


def validate_kotlin_structure(source: str) -> list[str]:
    """Cheap structural validation before Gradle compile."""
    errors: list[str] = []
    stripped = _strip_line_comments_and_strings(source)
    for opener, closer, name in (("{", "}", "brace"), ("(", ")", "parenthesis"), ("[", "]", "bracket")):
        balance = 0
        for ch in stripped:
            if ch == opener:
                balance += 1
            elif ch == closer:
                balance -= 1
            if balance < 0:
                errors.append(f"unmatched closing {name}")
                break
        if balance > 0:
            errors.append(f"unclosed {name}")

    saw_declaration = False
    for idx, line in enumerate(source.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("//") or text.startswith("/*") or text.startswith("*"):
            continue
        if text.startswith("package "):
            continue
        if _IMPORT_RE.match(line):
            if saw_declaration:
                errors.append(f"import outside import region at line {idx}")
            continue
        saw_declaration = True

    if _tree_sitter_has_error(source):
        errors.append("tree-sitter parse contains ERROR node")
    return errors


def _op_add_import(content: str, edit: dict[str, Any]) -> tuple[str, str]:
    import_line = str(edit.get("content") or edit.get("import") or "").strip()
    if not import_line:
        return content, "add_import missing content"
    if not import_line.startswith("import "):
        import_line = f"import {import_line}"
    if not _IMPORT_RE.match(import_line):
        return content, f"invalid import line: {import_line}"
    lines = content.splitlines()
    if any(line.strip() == import_line for line in lines):
        return content, ""
    region = _imports_region(lines)
    insert_at = region.end_line  # 1-based line after the last import/package.
    if insert_at < 1:
        insert_at = 1
    new_lines = lines[:insert_at] + [import_line] + lines[insert_at:]
    return _join_like(content, new_lines), ""


def _op_replace_block(content: str, edit: dict[str, Any]) -> tuple[str, str]:
    anchor = str(edit.get("anchor_substring") or "").strip()
    replacement = str(edit.get("content") or "")
    if not anchor:
        return content, "replace_block missing anchor_substring"
    if replacement == "":
        return content, "replace_block missing content"
    lines = content.splitlines()
    anchor_line = _find_unique_line_for_anchor(
        lines,
        anchor,
        int(edit.get("anchor_line") or 0),
    )
    if anchor_line <= 0:
        return content, "anchor not found or ambiguous"
    location = locate_kotlin_regions(content, line=anchor_line, anchor_substring=anchor)
    start, end = _statement_span(lines, anchor_line, location.allowed_region)
    replacement_lines = replacement.splitlines()
    indent = _leading_ws(lines[start - 1])
    replacement_lines = _indent_block(replacement_lines, indent)
    new_lines = lines[: start - 1] + replacement_lines + lines[end:]
    return _join_like(content, new_lines), ""


def _op_insert_into_function(content: str, edit: dict[str, Any]) -> tuple[str, str]:
    insertion = str(edit.get("content") or "")
    if not insertion.strip():
        return content, "insert_into_function missing content"
    lines = content.splitlines()
    location = locate_kotlin_regions(
        content,
        line=int(edit.get("anchor_line") or 0),
        anchor_substring=str(edit.get("anchor_substring") or ""),
    )
    fn = location.nearest_function
    if fn is None or fn.end_line <= fn.start_line:
        return content, "nearest function not found"
    insert_before = fn.end_line
    indent = _leading_ws(lines[max(0, insert_before - 1)])
    insert_lines = _indent_block(insertion.splitlines(), indent + "    ")
    new_lines = lines[: insert_before - 1] + insert_lines + lines[insert_before - 1 :]
    return _join_like(content, new_lines), ""


def _should_add_coroutines_launch_import(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    if "unresolved reference" not in lower or "launch" not in lower:
        return False
    if ".launch" not in content:
        return False
    if re.search(
        r"^\s*import\s+kotlinx\.coroutines\.(?:launch|\*)\s*$",
        content,
        re.MULTILINE,
    ):
        return False
    return "kotlinx.coroutines.CoroutineScope" in content or "rememberCoroutineScope" in content


def _should_add_compose_runtime_import(error_text: str, content: str, *, symbol: str) -> bool:
    lower = (error_text or "").lower()
    if "unresolved reference" not in lower or symbol.lower() not in lower:
        return False
    if symbol not in content:
        return False
    if re.search(
        rf"^\s*import\s+androidx\.compose\.runtime\.(?:{re.escape(symbol)}|\*)\s*$",
        content,
        re.MULTILINE,
    ):
        return False
    return True


def _should_add_android_view_import(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    if "unresolved reference" not in lower or "androidview" not in lower:
        return False
    if "AndroidView" not in content:
        return False
    return not re.search(
        r"^\s*import\s+androidx\.compose\.ui\.viewinterop\.(?:AndroidView|\*)\s*$",
        content,
        re.MULTILINE,
    )


def _should_repair_firebase_snapshot_children(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    diagnostic_signal = any(
        token in lower
        for token in (
            "unresolved reference 'child'",
            "unresolved reference: child",
            "unresolved reference 'snapshot'",
            "unresolved reference: snapshot",
            "no value passed for parameter 'content'",
            "no value passed for parameter content",
            "syntax error",
            "unexpected tokens",
            "expecting an element",
        )
    )
    if not diagnostic_signal:
        return False
    return (
        ".addOnSuccessListener" in content
        and ".ref.updateChildren" in content
        and ".exists()" in content
        and "FirebaseDatabase" in content
    )


def _should_repair_missing_try_for_kotlin_catch(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    catch_reference_signal = any(
        token in lower
        for token in (
            "unresolved reference 'catch'",
            "unresolved reference: catch",
        )
    )
    if not catch_reference_signal and not _diagnostic_line_is_catch(content, lower):
        return False
    if not re.search(r"^\s*}\s*catch\s*\(", content, re.MULTILINE):
        return False
    return ".launch" in content or "launch(" in content


def _diagnostic_line_is_catch(content: str, lower_error_text: str) -> bool:
    if not any(token in lower_error_text for token in ("syntax error", "unexpected tokens")):
        return False
    m = re.search(r"\bline\s*[:=]\s*(\d+)\b", lower_error_text)
    if not m:
        return False
    line_no = int(m.group(1))
    lines = content.splitlines()
    for idx in range(max(1, line_no - 2), min(len(lines), line_no + 2) + 1):
        if re.search(r"^\s*}\s*catch\s*\(", lines[idx - 1]):
            return True
    return False


def _should_repair_nullable_geocoder_addresses(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    if "nullable receiver" not in lower and "only safe" not in lower:
        return False
    if "address" not in lower and "getfromlocation" not in lower:
        return False
    return (
        "getFromLocation" in content
        and ".isNotEmpty()" in content
        and re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\[\s*0\s*\]", content)
        is not None
    )


def _should_repair_lifecycle_owner_in_disposable_effect(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    if "@composable invocations can only happen" not in lower:
        return False
    return (
        "DisposableEffect" in content
        and "LocalLifecycleOwner.current.lifecycle" in content
    )


def _should_repair_marker_receiver_in_mapview_apply(error_text: str, content: str) -> bool:
    lower = (error_text or "").lower()
    if "argument type mismatch" not in lower or "mapview" not in lower:
        return False
    return (
        "MapView(" in content
        and ".apply {" in content
        and "Marker(this)" in content
    )


def _repair_nullable_geocoder_addresses(content: str) -> tuple[str, str]:
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)if\s*\(\s*(?P<list>[A-Za-z_][A-Za-z0-9_]*)"
        r"\.isNotEmpty\(\)\s*\)\s*\{\s*\n"
        r"(?P<value_indent>[ \t]*)val\s+(?P<addr>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*=\s*(?P=list)\s*\[\s*0\s*\]",
        re.MULTILINE,
    )

    def replace(match: re.Match[str]) -> str:
        indent = match.group("indent")
        list_name = match.group("list")
        addr_name = match.group("addr")
        return (
            f"{indent}val {addr_name} = {list_name}?.firstOrNull()\n"
            f"{indent}if ({addr_name} != null) {{"
        )

    new_content, count = pattern.subn(replace, content, count=8)
    if count <= 0:
        return content, "no nullable geocoder address list shape found"
    return new_content, ""


def _repair_lifecycle_owner_in_disposable_effect(content: str) -> tuple[str, str]:
    lines = content.splitlines()
    effect_re = re.compile(r"^(?P<indent>\s*)DisposableEffect\s*\((?P<keys>[^)]*)\)\s*\{")
    lifecycle_re = re.compile(
        r"^\s*val\s+lifecycle\s*=\s*LocalLifecycleOwner\.current\.lifecycle\s*$"
    )
    for effect_idx, raw in enumerate(lines):
        m_effect = effect_re.match(raw)
        if not m_effect:
            continue
        end_line = _matching_brace_end(lines, effect_idx + 1)
        if end_line <= effect_idx + 1:
            continue
        lifecycle_idx = -1
        for idx in range(effect_idx + 1, end_line - 1):
            if lifecycle_re.match(lines[idx]):
                lifecycle_idx = idx
                break
        if lifecycle_idx < 0:
            continue
        previous = lines[effect_idx - 1].strip() if effect_idx > 0 else ""
        if previous == "val lifecycle = LocalLifecycleOwner.current.lifecycle":
            continue
        indent = m_effect.group("indent")
        keys = [part.strip() for part in m_effect.group("keys").split(",") if part.strip()]
        if "lifecycle" not in keys:
            keys.append("lifecycle")
            lines[effect_idx] = f"{indent}DisposableEffect({', '.join(keys)}) {{"
        lifecycle_line = f"{indent}val lifecycle = LocalLifecycleOwner.current.lifecycle"
        new_lines = (
            lines[:effect_idx]
            + [lifecycle_line]
            + lines[effect_idx:lifecycle_idx]
            + lines[lifecycle_idx + 1 :]
        )
        return _join_like(content, new_lines), ""
    return content, "no LocalLifecycleOwner.current inside DisposableEffect block found"


def _repair_marker_receiver_in_mapview_apply(content: str) -> tuple[str, str]:
    pattern = re.compile(r"\bMarker\s*\(\s*this\s*\)")
    new_content, count = pattern.subn("Marker(this@apply)", content, count=16)
    if count <= 0:
        return content, "no Marker(this) call found inside MapView apply"
    return new_content, ""


def _repair_missing_try_for_kotlin_catch(content: str, *, line: int = 0) -> tuple[str, str]:
    lines = content.splitlines()
    catch_re = re.compile(r"^\s*}\s*catch\s*\(")
    catch_indices = [idx for idx, raw in enumerate(lines) if catch_re.search(raw)]
    if line > 0 and catch_indices:
        near = [idx for idx in catch_indices if abs((idx + 1) - line) <= 40]
        if near:
            catch_indices = near

    for catch_idx in catch_indices:
        search_start = max(0, catch_idx - 80)
        for open_idx in range(catch_idx - 1, search_start - 1, -1):
            opener = lines[open_idx]
            if not _is_try_insertion_lambda_opener(opener):
                continue
            between = lines[open_idx + 1 : catch_idx]
            if any(re.search(r"^\s*try\s*\{", raw) for raw in between):
                continue
            next_nonblank = next((raw for raw in between if raw.strip()), "")
            if re.search(r"^\s*try\s*\{", next_nonblank):
                continue
            indent = _leading_ws(opener) + "    "
            new_lines = lines[: open_idx + 1] + [f"{indent}try {{"] + lines[open_idx + 1 :]
            return _join_like(content, new_lines), ""

    return content, "no coroutine/lambda block found for isolated catch"


def _is_try_insertion_lambda_opener(line: str) -> bool:
    stripped = _strip_line_comments_and_strings(line).strip()
    if "{" not in stripped:
        return False
    if re.search(r"\btry\s*\{", stripped):
        return False
    return bool(
        re.search(r"(?:\.|\b)launch\s*\(", stripped)
        or re.search(r"\brunBlocking\s*\(", stripped)
        or re.search(r"\bCoroutineScope\s*\(", stripped)
    )


def _repair_firebase_task_shapes(content: str, *, line: int = 0) -> tuple[str, str, list[str]]:
    """Repair common Firebase Task listener structures after text patch drift."""
    working = content
    applied: list[str] = []
    reasons: list[str] = []

    updated, err = _repair_firebase_success_listener_close_before_failure(
        working,
        line=line,
    )
    if err:
        reasons.append(err)
    elif updated != working:
        working = updated
        applied.append("close_firebase_success_listener_before_failure")

    updated, err = _repair_firebase_snapshot_children_loop(working, line=line)
    if err:
        reasons.append(err)
    elif updated != working:
        working = updated
        applied.append("wrap_firebase_snapshot_children")

    if applied:
        return working, "", applied
    return content, "; ".join(reasons) or "no Firebase task repair shape found", []


def _repair_firebase_success_listener_close_before_failure(
    content: str,
    *,
    line: int = 0,
) -> tuple[str, str]:
    lines = content.splitlines()
    failure_re = re.compile(r"^(?P<indent>\s*)\.addOnFailureListener\s*\{")
    success_re = re.compile(r"^(?P<indent>\s*)\.addOnSuccessListener\s*\{")
    update_re = re.compile(r"(?:\.ref\.)?updateChildren\s*\(")
    candidates = [idx for idx, raw in enumerate(lines) if failure_re.search(raw)]
    if line > 0 and candidates:
        near = [idx for idx in candidates if abs((idx + 1) - line) <= 120]
        if near:
            candidates = near

    inserts: list[tuple[int, str]] = []
    for failure_idx in candidates:
        previous = _previous_nonblank_line(lines, failure_idx)
        if previous and previous.strip() in {"}", "})"}:
            continue
        failure_indent = _leading_ws(lines[failure_idx])
        success_idx = -1
        search_start = max(0, failure_idx - 80)
        for idx in range(failure_idx - 1, search_start - 1, -1):
            success_match = success_re.search(lines[idx])
            if not success_match:
                continue
            if _leading_ws(lines[idx]) != failure_indent:
                continue
            has_update_call = any(
                update_re.search(lines[probe])
                for probe in range(max(0, idx - 4), idx + 1)
            )
            if has_update_call:
                success_idx = idx
                break
        if success_idx < 0:
            continue
        if _brace_balance(lines[success_idx:failure_idx]) <= 0:
            continue
        inserts.append((failure_idx, f"{failure_indent}}}"))

    if not inserts:
        return content, "no Firebase success listener chain missing close found"

    for idx, inserted_line in reversed(inserts):
        lines.insert(idx, inserted_line)
    return _join_like(content, lines), ""


def _repair_firebase_snapshot_children_loop(content: str, *, line: int = 0) -> tuple[str, str]:
    lines = content.splitlines()
    success_re = re.compile(r"\.addOnSuccessListener\s*\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*->")
    candidates: list[int] = []
    for idx, raw in enumerate(lines):
        if success_re.search(raw):
            candidates.append(idx)
    if line > 0 and candidates:
        near = [idx for idx in candidates if abs((idx + 1) - line) <= 80]
        if near:
            candidates = near

    for success_idx in candidates:
        success_match = success_re.search(lines[success_idx])
        if not success_match:
            continue
        snapshot_name = success_match.group(1)
        search_end = min(len(lines), success_idx + 90)
        missing_if_idx = -1
        missing_if_re = re.compile(
            rf"\bif\s*\(\s*!\s*{re.escape(snapshot_name)}\.exists\s*\(\s*\)\s*\)"
        )
        for idx in range(success_idx + 1, search_end):
            if missing_if_re.search(lines[idx]):
                missing_if_idx = idx
                break
        if missing_if_idx < 0:
            continue

        child_idx = -1
        child_name = ""
        child_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.ref\.updateChildren\s*\(")
        for idx in range(success_idx + 1, missing_if_idx):
            m_child = child_re.search(lines[idx])
            if m_child:
                child_idx = idx
                child_name = m_child.group(1)
                break
        if child_idx < 0 or not child_name:
            continue
        loop_re = re.compile(
            rf"for\s*\(\s*{re.escape(child_name)}\s+in\s+{re.escape(snapshot_name)}\.children\s*\)"
        )
        if any(loop_re.search(lines[idx]) for idx in range(success_idx + 1, missing_if_idx)):
            continue

        missing_end_line = _matching_brace_end(lines, missing_if_idx + 1)
        if missing_end_line <= missing_if_idx + 1:
            continue
        missing_end_idx = missing_end_line - 1
        child_block = list(lines[child_idx:missing_if_idx])
        while child_block and not child_block[-1].strip():
            child_block.pop()
        if child_block and child_block[-1].strip() == "}":
            child_indent = len(lines[child_idx]) - len(lines[child_idx].lstrip())
            close_indent = len(child_block[-1]) - len(child_block[-1].lstrip())
            if close_indent <= child_indent:
                child_block.pop()
        if not child_block:
            continue

        missing_body = list(lines[missing_if_idx + 1 : missing_end_idx])
        success_indent = _leading_ws(lines[success_idx])
        if_indent = success_indent + "    "
        loop_indent = success_indent + "        "
        body_indent = success_indent + "            "
        else_body_indent = success_indent + "        "
        new_segment = [
            lines[success_idx],
            f"{if_indent}if ({snapshot_name}.exists()) {{",
            f"{loop_indent}for ({child_name} in {snapshot_name}.children) {{",
            *_indent_block(child_block, body_indent),
            f"{loop_indent}}}",
            f"{if_indent}}} else {{",
            *_indent_block(missing_body, else_body_indent),
            f"{if_indent}}}",
        ]
        new_lines = lines[:success_idx] + new_segment + lines[missing_end_idx + 1 :]
        return _join_like(content, new_lines), ""

    return content, "no broken Firebase snapshot.children listener shape found"


def _imports_region(lines: list[str]) -> Region:
    package_line = 0
    import_lines: list[int] = []
    for idx, line in enumerate(lines, start=1):
        text = line.strip()
        if text.startswith("package "):
            package_line = idx
            continue
        if _IMPORT_RE.match(line):
            import_lines.append(idx)
            continue
        if import_lines and text and not text.startswith("//"):
            break
    if import_lines:
        return Region("imports", import_lines[0], import_lines[-1])
    start = package_line + 1 if package_line else 1
    return Region("imports", start, start)


def _nearest_function(lines: list[str], target_line: int) -> Region | None:
    best: Region | None = None
    for idx, line in enumerate(lines, start=1):
        if idx > target_line:
            break
        m = _FUNC_RE.search(line)
        if not m:
            continue
        end = _matching_brace_end(lines, idx)
        if end >= target_line:
            best = Region("function", idx, end, name=m.group(1))
    return best


def _nearest_block(lines: list[str], target_line: int) -> Region | None:
    stack: list[int] = []
    regions: list[Region] = []
    for idx, raw in enumerate(lines, start=1):
        line = _strip_line_comments_and_strings(raw)
        for ch in line:
            if ch == "{":
                stack.append(idx)
            elif ch == "}" and stack:
                start = stack.pop()
                if start <= target_line <= idx:
                    regions.append(Region("block", start, idx))
    if not regions:
        return None
    return max(regions, key=lambda r: r.start_line)


def _matching_brace_end(lines: list[str], start_line: int) -> int:
    balance = 0
    seen_open = False
    for idx in range(max(start_line, 1), len(lines) + 1):
        for ch in _strip_line_comments_and_strings(lines[idx - 1]):
            if ch == "{":
                balance += 1
                seen_open = True
            elif ch == "}":
                balance -= 1
                if seen_open and balance <= 0:
                    return idx
    return len(lines)


def _statement_span(lines: list[str], anchor_line: int, allowed: Region) -> tuple[int, int]:
    start = max(anchor_line, allowed.start_line)
    end_limit = min(len(lines), allowed.end_line)
    balance = 0
    seen_structure = False
    for idx in range(start, end_limit + 1):
        text = _strip_line_comments_and_strings(lines[idx - 1])
        for ch in text:
            if ch in "({[":
                balance += 1
                seen_structure = True
            elif ch in ")}]":
                balance -= 1
        stripped = text.strip()
        continued = stripped.endswith((".", ",", "&&", "||", "+", "-"))
        if idx > start and balance <= 0 and (seen_structure or not continued):
            return start, idx
        if idx == start and balance <= 0 and not continued:
            return start, start
    return start, end_limit


def _find_unique_line_for_anchor(lines: list[str], anchor: str, anchor_line: int = 0) -> int:
    hits = [idx for idx, line in enumerate(lines, start=1) if anchor in line]
    if not hits:
        return 0
    if len(hits) == 1:
        return hits[0]
    if anchor_line > 0:
        near = [idx for idx in hits if abs(idx - anchor_line) <= 10]
        if len(near) == 1:
            return near[0]
    return 0


def _first_anchor_line(lines: list[str], anchor: str) -> int:
    if not anchor:
        return 0
    hits = [idx for idx, line in enumerate(lines, start=1) if anchor in line]
    return hits[0] if len(hits) == 1 else 0


def _clamp_line(line: int, total: int) -> int:
    if total <= 0:
        return 1
    return max(1, min(line, total))


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _previous_nonblank_line(lines: list[str], before_idx: int) -> str:
    for idx in range(before_idx - 1, -1, -1):
        if lines[idx].strip():
            return lines[idx]
    return ""


def _brace_balance(lines: list[str]) -> int:
    balance = 0
    for raw in lines:
        for ch in _strip_line_comments_and_strings(raw):
            if ch == "{":
                balance += 1
            elif ch == "}":
                balance -= 1
    return balance


def _indent_block(lines: list[str], indent: str) -> list[str]:
    if not lines:
        return []
    min_indent: int | None = None
    for line in lines:
        if not line.strip():
            continue
        width = len(line) - len(line.lstrip())
        min_indent = width if min_indent is None else min(min_indent, width)
    trim = min_indent or 0
    out: list[str] = []
    for line in lines:
        out.append(indent + (line[trim:] if line.strip() else ""))
    return out


def _join_like(original: str, lines: list[str]) -> str:
    text = "\n".join(lines)
    return text + ("\n" if original.endswith("\n") else "")


def _strip_line_comments_and_strings(source: str) -> str:
    out: list[str] = []
    in_string = False
    quote = ""
    escape = False
    i = 0
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if not in_string and ch == "/" and nxt == "/":
            while i < len(source) and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if in_string:
            if ch == "\n":
                in_string = False
                quote = ""
                out.append(ch)
            else:
                out.append(" ")
                if not escape and ch == quote:
                    in_string = False
                    quote = ""
                escape = (ch == "\\" and not escape)
            i += 1
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tree_sitter_has_error(source: str) -> bool:
    try:
        import tree_sitter_kotlin as _ts_kt  # type: ignore
        from tree_sitter import Language, Parser  # type: ignore

        parser = Parser(Language(_ts_kt.language()))
        tree = parser.parse(source.encode("utf-8"))
        return bool(getattr(tree.root_node, "has_error", False))
    except Exception:  # noqa: BLE001
        return False


def _unified_diff(path: str, before: str, after: str) -> str:
    if before == after:
        return ""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    body = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}" if body else ""
