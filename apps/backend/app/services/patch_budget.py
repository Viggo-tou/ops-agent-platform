"""Pre-apply structural budget gate for codegen patches.

Catches the runaway-patch class of failures (model rewrites a hundred
lines for a one-line bug, edits files outside scope, balloons imports)
*before* we hand the patch to the sandbox. Cheap — pure unified-diff
parsing, no SQL, no LLM.

Usage:

    budget = PatchBudget()  # or override via env / planner request
    report = evaluate_patch_budget(diff_text, budget)
    if not report.passed:
        # Surface report.violations in the codegen repair prompt or
        # fail the task. report.metrics has observed counts for the
        # event log.
        ...

Per-task overrides are supported by callers passing a custom
``PatchBudget`` instance — the planner can request relaxed limits in
its plan output (with rationale) and the orchestrator constructs the
budget accordingly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# Conservative defaults. The planner may request a relaxed budget for
# legitimate refactors (with rationale that the reviewer enforces).
@dataclass(frozen=True)
class PatchBudget:
    max_files_changed: int = 8
    max_added_lines: int = 300
    max_removed_lines: int = 200
    max_new_imports_per_file: int = 5
    max_new_files: int = 2
    max_function_signatures_changed: int = 3


@dataclass(frozen=True)
class PatchBudgetReport:
    passed: bool
    violations: list[str] = field(default_factory=list)
    # Per-rule observed counts for the event log. Always populated even
    # when the report passes, so dashboards can show the budget headroom.
    metrics: dict[str, object] = field(default_factory=dict)


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_NEW_FILE_RE = re.compile(r"^new file mode \d+$")

# Python `import x` / `import x.y as z` / `from a.b import c, d`
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+\S+\s+)?import\s+\S+")
# Java/Kotlin `import com.foo.Bar` (so we don't completely miss those, even
# though Python is our SWE-bench focus)
_JAVA_KT_IMPORT_RE = re.compile(r"^\s*import\s+[A-Za-z_][\w.]*(?:\.\*)?\s*;?\s*$")
# JS/TS ESM: `import x from 'y'` / `import { a, b } from 'y'` / `import 'y'`
_ESM_IMPORT_RE = re.compile(r"^\s*import(?:\s+(?:.+?)\s+from)?\s+['\"]")


def _is_import_line(content: str) -> bool:
    return (
        bool(_PY_IMPORT_RE.match(content))
        or bool(_JAVA_KT_IMPORT_RE.match(content))
        or bool(_ESM_IMPORT_RE.match(content))
    )


def evaluate_patch_budget(diff: str, budget: PatchBudget) -> PatchBudgetReport:
    """Walk the diff once, count, compare against budget."""
    files: dict[str, dict[str, int]] = {}
    new_files: set[str] = set()
    current_path: str | None = None
    current_is_new = False

    for raw_line in diff.splitlines():
        header = _DIFF_HEADER_RE.match(raw_line)
        if header is not None:
            current_path = header.group(2)
            current_is_new = False
            files.setdefault(current_path, {"added": 0, "removed": 0, "new_imports": 0})
            continue
        if _NEW_FILE_RE.match(raw_line):
            current_is_new = True
            if current_path is not None:
                new_files.add(current_path)
            continue
        if current_path is None:
            continue
        if raw_line.startswith("@@") or raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            content = raw_line[1:]
            files[current_path]["added"] += 1
            if _is_import_line(content):
                files[current_path]["new_imports"] += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            files[current_path]["removed"] += 1

    total_added = sum(f["added"] for f in files.values())
    total_removed = sum(f["removed"] for f in files.values())
    max_new_imports = max((f["new_imports"] for f in files.values()), default=0)
    files_changed = len(files)
    new_file_count = len(new_files)

    metrics = {
        "files_changed": files_changed,
        "added_lines": total_added,
        "removed_lines": total_removed,
        "new_files": new_file_count,
        "max_new_imports_per_file": max_new_imports,
        "per_file": {p: dict(stats) for p, stats in files.items()},
    }

    violations: list[str] = []
    if files_changed > budget.max_files_changed:
        violations.append(
            f"files_changed: {files_changed} > {budget.max_files_changed}"
        )
    if total_added > budget.max_added_lines:
        violations.append(f"added_lines: {total_added} > {budget.max_added_lines}")
    if total_removed > budget.max_removed_lines:
        violations.append(
            f"removed_lines: {total_removed} > {budget.max_removed_lines}"
        )
    if new_file_count > budget.max_new_files:
        violations.append(f"new_files: {new_file_count} > {budget.max_new_files}")
    if max_new_imports > budget.max_new_imports_per_file:
        violations.append(
            f"new_imports_per_file: {max_new_imports} > {budget.max_new_imports_per_file}"
        )

    return PatchBudgetReport(
        passed=not violations,
        violations=violations,
        metrics=metrics,
    )
