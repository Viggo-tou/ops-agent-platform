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


def self_validate(
    diff: str,
    source_path: Path,
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
