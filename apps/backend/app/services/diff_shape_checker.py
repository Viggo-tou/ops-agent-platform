"""Diff shape checker: intent-vs-diff validation + existing-file-first policy.

T-041-02: Validates that the diff shape matches the task intent.
T-041-03: Enforces that destructive/fix tasks prefer modifying existing files.

Runs after codegen, before review. Catches overreach: a small bug fix
producing 12 files changed, or a "delete X" task that creates 5 new files
while barely touching the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShapeFinding:
    rule: str
    severity: str  # "block" | "warn"
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ShapeReport:
    verdict: str  # "pass" | "block"
    findings: tuple[ShapeFinding, ...]

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "message": f.message, "evidence": f.evidence}
                for f in self.findings
            ],
        }


_TASK_TYPE_PATTERNS = {
    "delete": re.compile(r"\b(delete|remove|clean|strip|eliminate|drop)\b", re.I),
    "fix": re.compile(r"\b(fix|bugfix|hotfix|patch|repair)\b", re.I),
    "rename": re.compile(r"\b(rename|move|refactor)\b", re.I),
    "small_change": re.compile(r"\b(tweak|adjust|update|change|modify)\b", re.I),
}

# Thresholds per task type
_SHAPE_LIMITS: dict[str, dict[str, int | float]] = {
    "delete": {"max_files": 10, "max_new_ratio": 0.3, "max_diff_lines": 500},
    "fix": {"max_files": 8, "max_new_ratio": 0.25, "max_diff_lines": 400},
    "rename": {"max_files": 20, "max_new_ratio": 0.5, "max_diff_lines": 2000},
    "small_change": {"max_files": 5, "max_new_ratio": 0.2, "max_diff_lines": 200},
    "default": {"max_files": 30, "max_new_ratio": 0.6, "max_diff_lines": 5000},
}


def check_diff_shape(
    *,
    request_text: str,
    diff: str,
    file_shapes: dict[str, str],
) -> ShapeReport:
    """Validate diff shape against task intent.

    ``file_shapes`` is {filepath: "create"|"modify"|"delete"} from
    ``_classify_files_in_diff``.
    """
    findings: list[ShapeFinding] = []

    task_type = _classify_task_type(request_text)
    limits = _SHAPE_LIMITS.get(task_type, _SHAPE_LIMITS["default"])

    modified = {p for p, s in file_shapes.items() if s == "modify"}
    created = {p for p, s in file_shapes.items() if s == "create"}
    deleted = {p for p, s in file_shapes.items() if s == "delete"}
    total_files = len(file_shapes)
    diff_lines = len(diff.splitlines())

    # Rule 1: File count exceeds task-type limit
    max_files = int(limits["max_files"])
    if total_files > max_files:
        findings.append(ShapeFinding(
            rule="shape_file_count",
            severity="block",
            message=(
                f"Task type '{task_type}' expects at most {max_files} files "
                f"but diff touches {total_files}."
            ),
            evidence={"task_type": task_type, "max_files": max_files, "actual_files": total_files},
        ))

    # Rule 2: Diff line count exceeds task-type limit
    max_lines = int(limits["max_diff_lines"])
    if diff_lines > max_lines:
        findings.append(ShapeFinding(
            rule="shape_diff_size",
            severity="warn",
            message=(
                f"Task type '{task_type}' expects at most {max_lines} diff lines "
                f"but diff has {diff_lines}."
            ),
            evidence={"task_type": task_type, "max_lines": max_lines, "actual_lines": diff_lines},
        ))

    # Rule 3: New file ratio (existing-file-first policy, T-041-03)
    max_new_ratio = float(limits["max_new_ratio"])
    if total_files > 0:
        new_ratio = len(created) / total_files
        if new_ratio > max_new_ratio and len(created) > 0:
            severity = "warn"
            findings.append(ShapeFinding(
                rule="existing_file_first",
                severity=severity,
                message=(
                    f"{len(created)}/{total_files} files in diff are NEW "
                    f"(ratio {new_ratio:.0%} exceeds {max_new_ratio:.0%} limit for "
                    f"'{task_type}' tasks). Prefer modifying existing files."
                ),
                evidence={
                    "task_type": task_type,
                    "created_files": sorted(created)[:10],
                    "modified_files": sorted(modified)[:10],
                    "new_ratio": round(new_ratio, 2),
                    "max_new_ratio": max_new_ratio,
                },
            ))

    # Rule 4: Delete task but no files actually deleted or reduced
    # Severity is warn (not block) because spec_conformance.shadow_implementation
    # is the authoritative gate for this pattern and handles retry logic.
    if task_type == "delete" and not deleted and not modified:
        findings.append(ShapeFinding(
            rule="shape_intent_mismatch",
            severity="warn",
            message="Delete task but diff neither deletes nor modifies any file.",
            evidence={"task_type": task_type, "file_shapes": dict(file_shapes)},
        ))

    # Rule 5: Fix/small_change but mostly creates
    if task_type in ("fix", "small_change") and len(created) > len(modified) and len(created) > 1:
        findings.append(ShapeFinding(
            rule="shape_overreach",
            severity="warn",
            message=(
                f"'{task_type}' task creates more files ({len(created)}) than "
                f"it modifies ({len(modified)}). This suggests overreach."
            ),
            evidence={
                "created": sorted(created)[:10],
                "modified": sorted(modified)[:10],
            },
        ))

    verdict = "block" if any(f.severity == "block" for f in findings) else "pass"
    return ShapeReport(verdict=verdict, findings=tuple(findings))


def _classify_task_type(request_text: str) -> str:
    for ttype, pattern in _TASK_TYPE_PATTERNS.items():
        if pattern.search(request_text):
            return ttype
    return "default"
