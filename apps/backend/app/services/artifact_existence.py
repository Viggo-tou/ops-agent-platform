"""Artifact existence gate.

Verifies that the files the planner committed to touching/creating actually
exist (and in the case of new-file creation, have non-empty content) in the
sandbox after the patch is applied. Closes the gap where a pipeline can
"pass" all style/lint gates but silently drop the core deliverable file
(e.g. Jira task says to create database.rules.json, and claude_code's diff
was filtered out by scope-lock — the file never lands in the sandbox but
every other gate passes).

Severity:
- Missing file listed in must_touch_files OR expected_new_files -> block
- Empty file where content was expected -> block
- File present but not touched by diff (and listed as must_touch) -> warn
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ArtifactFinding:
    file: str
    severity: str  # "block" | "warn"
    rule: str
    message: str


@dataclass(frozen=True)
class ArtifactReport:
    findings: list[ArtifactFinding]
    checked_must_touch: list[str]
    checked_expected_new: list[str]

    @property
    def blocking_findings(self) -> list[ArtifactFinding]:
        return [f for f in self.findings if f.severity == "block"]

    def to_payload(self) -> dict:
        return {
            "findings_blocking": len(self.blocking_findings),
            "findings_total": len(self.findings),
            "checked_must_touch": self.checked_must_touch,
            "checked_expected_new": self.checked_expected_new,
            "findings": [
                {
                    "file": f.file,
                    "severity": f.severity,
                    "rule": f.rule,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


def check_artifact_existence(
    *,
    sandbox_dir: str | Path,
    must_touch_files: list[str] | None,
    expected_new_files: list[str] | None,
    diff_touched_paths: set[str] | None = None,
) -> ArtifactReport:
    """Verify expected artifacts exist in sandbox_dir post-patch.

    sandbox_dir:           root of the sandboxed working tree (where the patch
                           has been applied)
    must_touch_files:      files the planner committed to modifying
    expected_new_files:    files the planner committed to creating (may be
                           same as or subset of must_touch)
    diff_touched_paths:    absolute set of file paths touched by the applied
                           diff; used to verify must_touch files were actually
                           touched (not just present)
    """
    must_touch = list(must_touch_files or [])
    expected_new = list(expected_new_files or [])
    touched = {p.strip() for p in (diff_touched_paths or set()) if p and p.strip()}

    sandbox = Path(str(sandbox_dir))
    findings: list[ArtifactFinding] = []

    # If the sandbox directory doesn't exist on disk, we can't meaningfully
    # check artifacts — this typically means we're in a test/mocked environment
    # or the pipeline short-circuited before sandbox.clone. Return empty report
    # instead of flagging every planner-declared file as missing.
    if not sandbox.exists() or not sandbox.is_dir():
        return ArtifactReport(
            findings=[],
            checked_must_touch=must_touch,
            checked_expected_new=expected_new,
        )

    # The sandbox usually has the project folder at top-level (e.g.
    # ``<sandbox>/handyman-admin-dashboard/...``). Planner paths are usually
    # relative to that project (e.g. ``src/firebase.js``). Try both direct
    # match and a one-level project root.
    def _resolve(rel_path: str) -> Path | None:
        candidates = [sandbox / rel_path]
        # Add each immediate subdir of sandbox as a project root candidate.
        if sandbox.is_dir():
            for child in sandbox.iterdir():
                if child.is_dir():
                    candidates.append(child / rel_path)
        for c in candidates:
            if c.exists():
                return c
        return None

    def _is_touched_by_diff(rel_path: str) -> bool:
        if not touched:
            return True  # unknown — skip the "not-touched" warn check
        if rel_path in touched:
            return True
        # Allow partial-path match (diff may have project prefix).
        for t in touched:
            if t == rel_path or t.endswith("/" + rel_path) or rel_path.endswith("/" + t):
                return True
        return False

    # Check expected_new_files first (stricter: must exist AND have content).
    for path in expected_new:
        resolved = _resolve(path)
        if resolved is None:
            findings.append(
                ArtifactFinding(
                    file=path,
                    severity="block",
                    rule="missing_expected_new_file",
                    message=(
                        f"Planner committed to creating '{path}' but no file "
                        f"with that path exists in the sandbox. The task is "
                        f"not complete; codegen/scope-lock likely stripped it."
                    ),
                )
            )
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            findings.append(
                ArtifactFinding(
                    file=path,
                    severity="block",
                    rule="empty_expected_new_file",
                    message=(
                        f"Expected new file '{path}' exists but is empty. The "
                        f"task's core deliverable content is missing."
                    ),
                )
            )

    # Check must_touch_files: exist + touched by diff (warn if not touched).
    for path in must_touch:
        if path in expected_new:
            continue  # already covered above
        resolved = _resolve(path)
        if resolved is None:
            findings.append(
                ArtifactFinding(
                    file=path,
                    severity="block",
                    rule="missing_must_touch_file",
                    message=(
                        f"Planner committed to modifying '{path}' but no file "
                        f"with that path exists in the sandbox after patch."
                    ),
                )
            )
            continue
        if not _is_touched_by_diff(path):
            findings.append(
                ArtifactFinding(
                    file=path,
                    severity="warn",
                    rule="must_touch_file_not_modified",
                    message=(
                        f"File '{path}' is listed in must_touch_files but the "
                        f"diff did not modify it. Either the plan was wrong "
                        f"or the change is incomplete."
                    ),
                )
            )

    return ArtifactReport(
        findings=findings,
        checked_must_touch=must_touch,
        checked_expected_new=expected_new,
    )
