"""Codegen self-validation (Stage A — codegen-quality root-cause fix).

Validates a generated diff applies cleanly + parses BEFORE codegen
returns. Catches hunk drift (P69-17 v4) and structurally broken
output at the source, instead of letting it through to sandbox apply +
compile_gate + repair (which today wastes 3-5 min per failure).

Validation steps:
1. apply_check: write the diff to a temp file, run `git apply --check`
   against the sandbox source. If fails -> hunk drift / context mismatch.
2. parse_check (language-specific):
   - .py: py_compile
   - .js/.jsx/.mjs: node --check via stdin (mirrors compile_gate.py)
   - .kt: kotlinc not available standalone; SKIP for now (gradle in
     compile_gate handles it later; not blocking)
   - other: SKIP

Returns ValidationResult with status + error context for retry prompt.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str  # human-readable
    error_detail: str  # for retry prompt
    apply_check_passed: bool
    parse_check_passed: bool

    def to_payload(self) -> dict:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "error_detail": self.error_detail[:2000],
            "apply_check_passed": self.apply_check_passed,
            "parse_check_passed": self.parse_check_passed,
        }


def validate_diff_applies(
    diff: str,
    source_path: Path,
) -> tuple[bool, str]:
    """Run `git apply --check` against source_path. Return (ok, error)."""
    if not diff.strip():
        return True, ""
    if not source_path or not source_path.exists():
        return True, ""
    git = shutil.which("git")
    if git is None:
        return True, ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".patch",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(diff)
            patch_path = tmp.name
        try:
            result = subprocess.run(
                [git, "apply", "--check", "--ignore-whitespace", patch_path],
                cwd=str(source_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "git apply --check failed").strip()
                return False, err[:2000]
            return True, ""
        finally:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
    except (subprocess.TimeoutExpired, OSError) as exc:
        return True, f"validation skipped: {exc}"


def _list_changed_paths(diff: str) -> list[str]:
    """Extract file paths from `+++ b/...` headers."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            rest = line[4:].strip()
            if rest.startswith("b/"):
                rest = rest[2:]
            if rest and rest != "/dev/null":
                out.append(rest)
    return out


def validate_diff_parses(
    diff: str,
    source_path: Path,
) -> tuple[bool, str]:
    """For each changed file, apply the diff into a scratch tmpdir and
    run language-specific parse. Returns (all_pass, error)."""
    if not diff.strip() or not source_path or not source_path.exists():
        return True, ""
    git = shutil.which("git")
    if git is None:
        return True, ""
    paths = _list_changed_paths(diff)
    if not paths:
        return True, ""

    # Strategy: copy source_path to scratch, apply diff there, then
    # for each .py / .js / .mjs / .jsx file in paths, run parse.
    # Skip .kt (no fast standalone parser).
    parseable = [p for p in paths if p.endswith((".py", ".js", ".mjs", ".jsx"))]
    if not parseable:
        return True, ""

    with tempfile.TemporaryDirectory(prefix="codegen-validate-") as scratch:
        scratch_dir = Path(scratch) / "src"
        try:
            shutil.copytree(str(source_path), str(scratch_dir))
        except (shutil.Error, OSError) as exc:
            return True, f"validation skipped (copy failed): {exc}"

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".patch",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(diff)
            patch_path = tmp.name
        try:
            apply_result = subprocess.run(
                [git, "apply", "--ignore-whitespace", patch_path],
                cwd=str(scratch_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if apply_result.returncode != 0:
                # apply_check should've caught this earlier; surface anyway.
                return False, (apply_result.stderr or "").strip()[:2000]
        finally:
            try:
                os.unlink(patch_path)
            except OSError:
                pass

        for rel in parseable:
            full = scratch_dir / rel
            if not full.is_file():
                continue
            if rel.endswith(".py"):
                ok, err = _check_py(full)
            else:
                ok, err = _check_js(full)
            if not ok:
                return False, f"{rel}: {err}"[:2000]

    return True, ""


def _check_py(path: Path) -> tuple[bool, str]:
    import py_compile
    try:
        py_compile.compile(str(path), doraise=True)
        return True, ""
    except py_compile.PyCompileError as exc:
        return False, str(exc).strip()[:1000]


def _check_js(path: Path) -> tuple[bool, str]:
    node = shutil.which("node")
    if node is None:
        return True, ""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return True, f"read failed: {exc}"
    head = source.split("\n", 30)[:30]
    is_esm = any(
        line.lstrip().startswith(("import ", "import{", "export "))
        for line in head
    )
    args = [node, "--input-type=module", "--check"] if is_esm else [node, "--check", "-"]
    try:
        result = subprocess.run(
            args,
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True, ""
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        # JSX false positive (mirror compile_gate)
        if "Unexpected token '<'" in stderr:
            return True, ""
        return False, stderr[:1000]
    return True, ""


def validate_imports_preserved(diff: str) -> tuple[bool, str]:
    """L4a: check that `import` lines are not silently dropped from
    .kt / .py / .ts / .js files in the diff.

    Empirical: DeepSeek codegen on Kotlin frequently *deletes* the
    original file's `import` block when re-emitting the file body, then
    references symbols (rememberNavController, viewModel, ...) that
    those imports brought in -> compile_gate's "Unresolved reference"
    explosion (P69-17 v26 round 1: 12+ errors all chained from missing
    imports).

    Rule: for each per-file diff section, count `- import ...` (deleted)
    vs `+ import ...` (added) lines. If deleted > added by more than
    a small slack (e.g. 2 — accommodating intentional unused-import
    cleanup), reject.
    """
    if not diff or not diff.strip():
        return True, ""
    # Split into per-file sections
    file_sections: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None:
                file_sections[current_path] = current_lines
            current_lines = []
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else None
        else:
            current_lines.append(line)
    if current_path is not None:
        file_sections[current_path] = current_lines

    bad_files: list[str] = []
    for path, lines in file_sections.items():
        # Only enforce on languages where stale imports = compile error
        if not path.lower().endswith((".kt", ".kts", ".py", ".ts", ".tsx", ".js", ".jsx", ".java")):
            continue
        deleted_imports = 0
        added_imports = 0
        for line in lines:
            stripped = line[1:].lstrip() if line[:1] in "+-" else ""
            if not stripped.startswith("import "):
                continue
            if line.startswith("-"):
                deleted_imports += 1
            elif line.startswith("+"):
                added_imports += 1
        # Slack: allow up to 2 net deletions (intentional cleanup)
        if deleted_imports - added_imports > 2:
            bad_files.append(
                f"{path} (-{deleted_imports} imports / +{added_imports})"
            )
    if bad_files:
        return False, (
            "Diff drops existing import statements without replacement. "
            "DeepSeek-style failure mode — the dropped imports cause "
            "Unresolved reference errors at compile_gate. Affected files: "
            + "; ".join(bad_files)
        )
    return True, ""


def validate_cross_file_refs(diff: str) -> tuple[bool, str]:
    """L4e: catch DeepSeek cross-file rename oscillation.

    Failure mode (v28 P69-17): file A diff removes `val jobLocation`
    but file B diff (or unchanged context inside B's hunks) still
    references `.jobLocation`. Compile_gate then explodes with
    Unresolved reference, repair invents a third name, loop forever.

    Rule: for each Kotlin/Java file in the diff:
      removed_props[file] = {names from `-    val X`, `-    var X`,
                             `-data class Foo(val X`, `-class Foo(val X`,
                             `-    fun X(`, `-    val X by` patterns}
    Then: for every other file in the diff, scan all `+` and ` ` (context)
    lines for `.NAME` token references. If NAME is in the union of
    removed_props of OTHER files AND NAME is NOT in the union of added_props
    of any file in diff, flag as oscillation.

    Skip non-Kotlin/Java languages.
    Same per-file scope rules as validate_imports_preserved.
    """
    if not diff or not diff.strip():
        return True, ""

    file_sections: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None:
                file_sections[current_path] = current_lines
            current_lines = []
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else None
        else:
            current_lines.append(line)
    if current_path is not None:
        file_sections[current_path] = current_lines

    scoped_sections = {
        path: lines
        for path, lines in file_sections.items()
        if path.lower().endswith((".kt", ".kts", ".java"))
    }
    if len(scoped_sections) < 2:
        return True, ""

    prop_re = re.compile(
        r"^[+-]\s*(?:(?:private|public|internal|protected)\s+)?"
        r"(?:override\s+)?(?:val|var)\s+(\w+)"
    )
    fun_re = re.compile(r"^[+-]\s*(?:override\s+)?fun\s+(\w+)\s*\(")
    ctor_re = re.compile(r"^[+-]\s*(?:data\s+)?class\s+\w+[^{\n]*\(([^)]*)\)")
    ctor_prop_re = re.compile(r"(?:val|var)\s+(\w+)")
    ref_re = re.compile(r"\.(\w+)\b")

    def _extract_declarations(lines: list[str], prefix: str) -> set[str]:
        names: set[str] = set()
        header = "+++" if prefix == "+" else "---"
        for line in lines:
            if not line.startswith(prefix) or line.startswith(header):
                continue
            prop_match = prop_re.match(line)
            if prop_match:
                names.add(prop_match.group(1))
            fun_match = fun_re.match(line)
            if fun_match:
                names.add(fun_match.group(1))
            ctor_match = ctor_re.match(line)
            if ctor_match:
                names.update(ctor_prop_re.findall(ctor_match.group(1)))
        return names

    removed_props = {
        path: _extract_declarations(lines, "-")
        for path, lines in scoped_sections.items()
    }
    if not any(removed_props.values()):
        return True, ""

    added_props = {
        path: _extract_declarations(lines, "+")
        for path, lines in scoped_sections.items()
    }
    added_names = {name for names in added_props.values() for name in names}

    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for ref_path, lines in scoped_sections.items():
        removed_by_other_file: dict[str, list[str]] = {}
        for decl_path, names in removed_props.items():
            if decl_path == ref_path:
                continue
            for name in names:
                if name not in added_names:
                    removed_by_other_file.setdefault(name, []).append(decl_path)
        if not removed_by_other_file:
            continue

        for line in lines:
            if line.startswith(("+++", "---")):
                continue
            if not (line.startswith("+") or line.startswith(" ")):
                continue
            for ref_name in ref_re.findall(line):
                if ref_name not in removed_by_other_file:
                    continue
                key = (ref_path, ref_name)
                if key in seen:
                    continue
                seen.add(key)
                declaring_files = ", ".join(sorted(removed_by_other_file[ref_name]))
                problems.append(
                    f"{ref_path} references .{ref_name}, but {ref_name} "
                    f"was removed from {declaring_files} and not re-declared"
                )

    if problems:
        return False, (
            "Cross-file rename oscillation: removed declarations are still "
            "referenced from other files. " + "; ".join(problems)
        )
    return True, ""


def validate_no_rewrite_of_existing(
    diff: str,
    must_touch_files: list[str],
) -> tuple[bool, str]:
    """L5: reject diff that rewrites an existing must_touch file as
    'new file mode 100644'.

    Failure mode (v36 P69-17): DeepSeek emitted
        diff --git a/.../JobPostingFragment.kt b/.../JobPostingFragment.kt
        new file mode 100644
        --- /dev/null
        +++ b/.../JobPostingFragment.kt
    for a file already in plan.must_touch_files. The diff "creates"
    a 100-line replacement that drops the original file's other
    methods (createImageFileUri, OSMDroid init, etc.). Compile and
    symbol_graph pass because the new file is internally consistent.
    Reservations.review flagged 5 items but task still reached
    AWAITING_APPROVAL with broken code.

    Rule: for each `diff --git` section in the diff:
      - extract the +++ b/<path>
      - check if section contains `new file mode 100644` OR
        `^index 0{7,}\\..` (alternate "all-zero parent" indicator)
      - if yes AND path matches any entry in must_touch_files
        (suffix-tolerant: same matching used by L4e), reject.

    Skip cleanly when:
      - diff is empty
      - must_touch_files is empty
      - no new-file sections present
    """
    if not diff or not diff.strip():
        return True, ""
    if not must_touch_files:
        return True, ""

    rewritten: list[str] = []
    for section in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        section = section.strip()
        if not section:
            continue
        # Path: prefer +++ b/... ; fall back to diff --git a/... b/...
        m_path = re.search(r"^\+\+\+ b/(.+?)$", section, flags=re.MULTILINE)
        if m_path:
            path = m_path.group(1).strip()
        else:
            m_path = re.match(r"diff --git a/(.+?) b/", section)
            if m_path is None:
                continue
            path = m_path.group(1).strip()
        # /dev/null on the +++ side means deletion, not rewrite
        if path == "/dev/null":
            continue
        is_new_file = (
            "new file mode " in section
            or bool(re.search(r"^index 0{7,}\.\.", section, re.MULTILINE))
        )
        if not is_new_file:
            continue
        for mt in must_touch_files:
            if not mt:
                continue
            if mt == path or mt.endswith("/" + path) or path.endswith("/" + mt):
                rewritten.append(path)
                break
    if rewritten:
        return False, (
            "Diff rewrites existing must_touch file(s) as 'new file mode'. "
            "This drops the original file's behavior. Affected: "
            + ", ".join(sorted(set(rewritten)))
            + ". Use minimal --- a/path / +++ b/path hunks instead."
        )
    return True, ""


def self_validate(
    diff: str,
    source_path: Path,
    *,
    must_touch_files: list[str] | None = None,
) -> ValidationResult:
    """Run full self-validation: apply check + parse check.

    Returns ValidationResult. Conservative behavior:
    - When git or node missing -> validation skipped, valid=True
    - When source_path doesn't exist -> validation skipped, valid=True
    - When .kt files only -> validation skipped (no fast standalone
      Kotlin parser; gradle in compile_gate handles)
    """
    apply_ok, apply_err = validate_diff_applies(diff, source_path)
    if not apply_ok:
        if must_touch_files:
            no_rewrite_ok, no_rewrite_err = validate_no_rewrite_of_existing(
                diff, list(must_touch_files)
            )
            if not no_rewrite_ok:
                return ValidationResult(
                    valid=False,
                    reason="codegen rewrote an existing must_touch file as 'new file mode' (L5)",
                    error_detail=no_rewrite_err,
                    apply_check_passed=False,
                    parse_check_passed=False,
                )
        return ValidationResult(
            valid=False,
            reason="git apply --check failed (hunk drift / context mismatch)",
            error_detail=apply_err,
            apply_check_passed=False,
            parse_check_passed=False,
        )
    # L4a: import-preservation check before the slower parse step
    imports_ok, imports_err = validate_imports_preserved(diff)
    if not imports_ok:
        return ValidationResult(
            valid=False,
            reason="codegen dropped existing imports (likely DeepSeek failure mode)",
            error_detail=imports_err,
            apply_check_passed=True,
            parse_check_passed=False,
        )
    refs_ok, refs_err = validate_cross_file_refs(diff)
    if not refs_ok:
        return ValidationResult(
            valid=False,
            reason="cross-file rename oscillation (L4e — declared name removed in one file but still referenced in another)",
            error_detail=refs_err,
            apply_check_passed=True,
            parse_check_passed=False,
        )
    if must_touch_files:
        no_rewrite_ok, no_rewrite_err = validate_no_rewrite_of_existing(
            diff, list(must_touch_files)
        )
        if not no_rewrite_ok:
            return ValidationResult(
                valid=False,
                reason="codegen rewrote an existing must_touch file as 'new file mode' (L5)",
                error_detail=no_rewrite_err,
                apply_check_passed=True,
                parse_check_passed=False,
            )
    parse_ok, parse_err = validate_diff_parses(diff, source_path)
    if not parse_ok:
        return ValidationResult(
            valid=False,
            reason="post-apply parse failed",
            error_detail=parse_err,
            apply_check_passed=True,
            parse_check_passed=False,
        )
    return ValidationResult(
        valid=True,
        reason="ok",
        error_detail="",
        apply_check_passed=True,
        parse_check_passed=True,
    )
