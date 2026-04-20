"""Spec-conformance checks between apply_patch and Jira transition.

General-purpose rules applied to *every* jira_issue_develop task:

1. ``shadow_implementation``
   Request contains a destructive/modifying verb (remove/rename/fix/...)
   but the patch only creates new files. Pattern observed on P69-10:
   model builds a parallel clean architecture instead of modifying the
   dirty existing code.

2. ``hit_delta``
   If the request mentions a quoted anchor (e.g. ``'Minij'``) that exists
   in the source tree before the patch, the post-patch occurrence count
   must strictly decrease. Anchor still present with same/higher count
   means the cleanup did not happen.

3. ``must_touch``
   If any quoted anchor exists in the source tree, at least one file
   that physically contained the anchor must appear in the diff
   (modified *or* overwritten). Patches that touch only unrelated files
   do not meet the spec.

These rules are deliberately free of any P69-10-specific pattern. The
verbs are a general English cleanup vocabulary, anchors come from the
request itself, and all counts are computed against the live source
tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DESTRUCTIVE_VERBS: frozenset[str] = frozenset(
    {
        "remove", "removed", "removing",
        "delete", "deleted", "deleting",
        "clean", "cleanup", "cleaned", "cleaning",
        "rename", "renamed", "renaming",
        "refactor", "refactored", "refactoring",
        "fix", "fixed", "fixing",
        "replace", "replaced", "replacing",
        "simplify", "simplified", "simplifying",
        "strip", "stripped", "stripping",
        "eliminate", "eliminated", "eliminating",
        "drop", "dropped", "dropping",
        "disable", "disabled", "disabling",
    }
)

_WORD_RE = re.compile(r"[A-Za-z]+")
_QUOTED_RE = re.compile(r"""['"`]([^'"`\n]{2,40})['"`]""")
# Target-direction prepositions: anchors following "to", "to only/just",
# "only", "just" are goal values (should be added/kept), not removal targets.
_TARGET_CONTEXT_RE = re.compile(
    r"""(?:(?:to\s+(?:(?:only|just)\s+)?)|(?:(?:only|just)\s+))['"`]([^'"`\n]{2,40})['"`]""",
    re.IGNORECASE,
)
# Unquoted identifier candidates: snake_case (has underscore) or camelCase/
# PascalCase with at least one internal uppercase transition. Excludes plain
# single-word capitalized English (e.g. "Admin", "Staff") which are too
# ambiguous — those must be quoted to count as anchors.
_IDENT_SNAKE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_IDENT_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_IDENT_PASCAL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_IDENT_ALPHANUM_RE = re.compile(r"\b[a-z]+[0-9]+\b|\b[a-z]+_[a-z0-9]+\b")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_NEW_FILE_RE = re.compile(r"^new file mode", re.MULTILINE)
_DELETED_FILE_RE = re.compile(r"^deleted file mode", re.MULTILINE)


@dataclass(frozen=True)
class ConformanceFinding:
    rule: str
    severity: str  # "block" | "warn"
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConformanceReport:
    verdict: str  # "pass" | "block"
    findings: tuple[ConformanceFinding, ...]

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    def block_messages(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "block"]

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [
                {
                    "rule": f.rule,
                    "severity": f.severity,
                    "message": f.message,
                    "evidence": f.evidence,
                }
                for f in self.findings
            ],
        }


def check_spec_conformance(
    *,
    request_text: str | None,
    normalized_request: str | None,
    diff: str,
    source_tree: Path | None,
    must_touch_files: Iterable[str] = (),
) -> ConformanceReport:
    """Run all conformance rules and return a combined report.

    ``must_touch_files`` comes from ``GeneratedPlan.must_touch_files`` and
    represents files the planner committed to modifying. Adds a fourth
    rule (``planner_must_touch``) on top of the three anchor-based ones.
    """
    findings: list[ConformanceFinding] = []
    combined_request = " ".join(
        part.strip() for part in (request_text, normalized_request) if part
    )

    file_shapes = _classify_files_in_diff(diff)
    modified = {p for p, s in file_shapes.items() if s == "modify"}
    created = {p for p, s in file_shapes.items() if s == "create"}
    deleted = {p for p, s in file_shapes.items() if s == "delete"}
    touched = modified | created | deleted

    if _has_destructive_verb(combined_request):
        if created and not modified and not deleted:
            findings.append(
                ConformanceFinding(
                    rule="shadow_implementation",
                    severity="block",
                    message=(
                        "Request asks to modify or remove existing behavior, "
                        "but patch only creates new files. Existing files that "
                        "hold the target behavior must be modified."
                    ),
                    evidence={
                        "created_files": sorted(created),
                        "modified_files": [],
                    },
                )
            )

    anchors = _extract_quoted_anchors(combined_request)
    target_set = _identify_target_anchors(combined_request)
    # Only apply hit_delta / must_touch to removal anchors — target
    # anchors are values the code should change *to*, not remove.
    removal_anchors = [a for a in anchors if a.lower() not in target_set]
    if removal_anchors and source_tree is not None and source_tree.exists():
        anchor_report = _anchor_delta_report(
            source_tree=source_tree,
            diff=diff,
            anchors=removal_anchors,
        )
        findings.extend(anchor_report)

        # T-040 "no-found > fabrication": when the request references
        # specific identifiers but NONE of them exist in the configured
        # knowledge source, the task is almost certainly targeting the
        # wrong repository. Block so the LLM's fabricated patch cannot
        # pass review. Only triggers when a destructive verb is also
        # present so plain read/explain queries don't trip it.
        if _has_destructive_verb(combined_request):
            anchors_with_hits = [
                a for a in removal_anchors if _find_files_containing_anchor(source_tree, a)
            ]
            if removal_anchors and not anchors_with_hits:
                findings.append(
                    ConformanceFinding(
                        rule="anchors_missing_from_tree",
                        severity="block",
                        message=(
                            "Request references specific identifiers ("
                            + ", ".join(sorted(removal_anchors))
                            + ") but NONE of them appear in the "
                            "configured knowledge source. This likely "
                            "means the task is targeting a different "
                            "repository. Verify the knowledge source "
                            "configuration or re-check the request."
                        ),
                        evidence={
                            "anchors": sorted(removal_anchors),
                            "source_tree": str(source_tree),
                        },
                    )
                )

    must_touch_clean = [
        str(path).strip().replace("\\", "/")
        for path in must_touch_files
        if isinstance(path, str) and path.strip()
    ]
    if must_touch_clean:
        missing = [p for p in must_touch_clean if p not in touched]
        # require *modification* of must-touch files when they exist on disk;
        # creating a new file with the same path counts only when the file
        # did not exist pre-patch (we treat "create" as acceptable only when
        # source_tree does not contain it).
        if missing and source_tree is not None and source_tree.exists():
            unsatisfied: list[str] = []
            for path in missing:
                expected = source_tree / path
                if expected.exists():
                    # Only require touching files that contain at least one
                    # removal anchor — the planner sometimes over-commits
                    # files that have no relevant content.
                    if not removal_anchors:
                        # No quoted anchors in request — the planner's
                        # must-touch list was generated without concrete
                        # identifiers.  Don't enforce file-level checks
                        # when the commitment is speculative.
                        continue
                    try:
                        content = expected.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        content = ""
                    has_anchor = any(
                        a.lower() in content.lower()
                        for a in removal_anchors
                    )
                    if not has_anchor:
                        continue
                    unsatisfied.append(path)
                # if file does not exist on disk, the planner committed to a
                # new file — that's an acceptable shape; do not flag.
            if not unsatisfied:
                missing = []
            else:
                missing = unsatisfied
        if missing:
            findings.append(
                ConformanceFinding(
                    rule="planner_must_touch",
                    severity="block",
                    message=(
                        "Planner committed to modifying these existing files "
                        "but the patch did not touch them: "
                        + ", ".join(sorted(missing))
                    ),
                    evidence={
                        "must_touch_files": sorted(must_touch_clean),
                        "missing_from_diff": sorted(missing),
                        "actually_touched": sorted(touched),
                    },
                )
            )

    verdict = "block" if any(f.severity == "block" for f in findings) else "pass"
    return ConformanceReport(verdict=verdict, findings=tuple(findings))


def build_goal_attestation(
    *,
    request_text: str | None,
    normalized_request: str | None,
    diff: str,
    source_tree: Path | None,
) -> dict:
    """Produce per-anchor evidence that each destructive sub-goal was met.

    Runs only the evidence computation (no verdict). Intended to run AFTER
    ``check_spec_conformance`` returns verdict=pass so we have a positive
    attestation to surface in events and in the task result.

    Anchors are classified into two directions:

    * **removal** (default): the anchor should be reduced. Success means
      ``post < baseline``.
    * **target**: the anchor follows a "to" / "only" preposition — it is a
      goal *value* the code should change to.  Success means
      ``plus_in_diff > 0`` (the value appears in added lines).
    """
    combined_request = " ".join(
        part.strip() for part in (request_text, normalized_request) if part
    )
    destructive_verbs = sorted(
        {tok for tok in _WORD_RE.findall(combined_request.lower()) if tok in DESTRUCTIVE_VERBS}
    )
    anchors = _extract_quoted_anchors(combined_request)
    target_set = _identify_target_anchors(combined_request)

    per_anchor: list[dict] = []
    all_met = True
    if source_tree is not None and source_tree.exists() and anchors:
        minus_counts, plus_counts = _count_anchor_occurrences_in_diff(
            diff=diff, anchors=anchors
        )
        touched_files = _collect_files_touched_by_diff(diff)
        for anchor in anchors:
            is_target = anchor.lower() in target_set
            hit_files = _find_files_containing_anchor(source_tree, anchor)
            if not hit_files:
                # Anchor not in tree. For targets, check if it was added.
                if is_target:
                    plus = plus_counts.get(anchor, 0)
                    achieved = plus > 0
                    if not achieved:
                        all_met = False
                    per_anchor.append({
                        "anchor": anchor,
                        "direction": "target",
                        "status": "achieved" if achieved else "not_achieved",
                        "count_before": 0,
                        "count_after": plus,
                        "plus_in_diff": plus,
                        "files_before": [],
                        "files_modified": [],
                    })
                else:
                    per_anchor.append({
                        "anchor": anchor,
                        "direction": "removal",
                        "status": "not_in_tree",
                        "count_before": 0,
                        "count_after": 0,
                        "files_before": [],
                        "files_modified": [],
                    })
                continue
            baseline = sum(hit_files.values())
            minus = minus_counts.get(anchor, 0)
            plus = plus_counts.get(anchor, 0)
            post = max(0, baseline - minus + plus)
            hit_paths = set(hit_files.keys())
            modified_containing = sorted(hit_paths & touched_files)
            if is_target:
                # Target anchor: success when it appears in added lines
                achieved = plus > 0
            else:
                # Removal anchor: success when count decreases
                achieved = post < baseline
            if not achieved:
                all_met = False
            per_anchor.append({
                "anchor": anchor,
                "direction": "target" if is_target else "removal",
                "status": "achieved" if achieved else "not_achieved",
                "count_before": baseline,
                "count_after": post,
                "minus_in_diff": minus,
                "plus_in_diff": plus,
                "files_before": sorted(hit_paths)[:20],
                "files_modified": modified_containing[:20],
            })

    return {
        "destructive_verbs_detected": destructive_verbs,
        "anchors": per_anchor,
        "all_goals_met": all_met if per_anchor else (not destructive_verbs),
    }


def _has_destructive_verb(text: str) -> bool:
    tokens = _WORD_RE.findall(text.lower())
    return any(tok in DESTRUCTIVE_VERBS for tok in tokens)


def _extract_quoted_anchors(text: str) -> list[str]:
    """Extract anchors from the request.

    Priority 1: explicitly quoted tokens (`'Minij'`, `"master admin"`).
    Priority 2: unquoted identifier-shaped tokens — snake_case,
    camelCase, PascalCase with internal uppercase transitions. These are
    specific enough to grep against a real codebase without producing
    obvious false positives from ordinary English prose.

    Single-word capitalized tokens like "Admin" or proper nouns without
    structural cues are intentionally NOT extracted here — too ambiguous.
    Translation/planner output is the right place to surface those.
    """
    seen: set[str] = set()
    anchors: list[str] = []

    for match in _QUOTED_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        low = raw.lower()
        if low in seen:
            continue
        if len(raw.split()) > 3:
            continue
        seen.add(low)
        anchors.append(raw)

    for pattern in (_IDENT_SNAKE_RE, _IDENT_CAMEL_RE, _IDENT_PASCAL_RE, _IDENT_ALPHANUM_RE):
        for match in pattern.finditer(text):
            raw = match.group(0)
            low = raw.lower()
            if low in seen:
                continue
            if len(raw) < 4:
                continue
            seen.add(low)
            anchors.append(raw)

    return anchors


def _identify_target_anchors(text: str) -> set[str]:
    """Return the subset of quoted anchors that are *goal values* — values
    the code should change *to*, not values that should be removed.

    Detected by preceding prepositions like "to 'X'", "to only 'X'",
    "only 'X'".  Conjunctions ("and 'Y'", "or 'Y'") following a target
    match are also treated as targets.  For example, in
    "consolidate roles to only 'Admin' and 'Staff'", both 'Admin' and
    'Staff' are targets.
    """
    targets: set[str] = set()
    for match in _TARGET_CONTEXT_RE.finditer(text):
        raw = match.group(1).strip()
        if raw:
            targets.add(raw.lower())
        # Scan forward for conjunctions: "and 'Y'" / "or 'Y'"
        rest = text[match.end():]
        for conj_match in re.finditer(
            r"""^\s*(?:,\s*)?(?:and|or)\s+['"`]([^'"`\n]{2,40})['"`]""",
            rest,
            re.IGNORECASE,
        ):
            conj_raw = conj_match.group(1).strip()
            if conj_raw:
                targets.add(conj_raw.lower())
    return targets


_UNIFIED_HEADER_RE = re.compile(
    r"^---\s+(?:a/)?(.+?)(?:\t.*)?$\n^\+\+\+\s+(?:b/)?(.+?)(?:\t.*)?$",
    re.MULTILINE,
)


def _classify_files_in_diff(diff: str) -> dict[str, str]:
    shapes: dict[str, str] = {}
    if not diff:
        return shapes

    # Strategy 1: git-style diff headers
    chunks = re.split(r"(?m)^(?=diff --git )", diff)
    for chunk in chunks:
        header = _DIFF_HEADER_RE.search(chunk)
        if not header:
            continue
        path = header.group(2).strip()
        if _NEW_FILE_RE.search(chunk):
            shapes[path] = "create"
        elif _DELETED_FILE_RE.search(chunk):
            shapes[path] = "delete"
        else:
            shapes[path] = "modify"

    # Strategy 2: standard unified diff (--- a/... / +++ b/...) without git headers
    if not shapes:
        for m in _UNIFIED_HEADER_RE.finditer(diff):
            old_path = m.group(1).strip()
            new_path = m.group(2).strip()
            if old_path == "/dev/null":
                shapes[new_path] = "create"
            elif new_path == "/dev/null":
                shapes[old_path] = "delete"
            else:
                shapes[new_path] = "modify"

    return shapes


def _anchor_delta_report(
    *,
    source_tree: Path,
    diff: str,
    anchors: Iterable[str],
) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    diff_minus_counts, diff_plus_counts = _count_anchor_occurrences_in_diff(
        diff=diff, anchors=anchors
    )
    touched_files = _collect_files_touched_by_diff(diff)

    any_anchor_decreased = False
    delta_details: list[ConformanceFinding] = []
    touch_details: list[ConformanceFinding] = []

    for anchor in anchors:
        hit_files = _find_files_containing_anchor(source_tree, anchor)
        if not hit_files:
            continue  # anchor not present in the tree — nothing to assert

        # hit_delta
        baseline = sum(hit_files.values())
        minus = diff_minus_counts.get(anchor, 0)
        plus = diff_plus_counts.get(anchor, 0)
        post = max(0, baseline - minus + plus)
        if post < baseline:
            any_anchor_decreased = True
        elif minus > 0 and plus > 0:
            # Diff both removes and re-adds the anchor — normalization
            # pattern (e.g. mapping "master admin" → "Admin"). Treat as
            # a decrease so this finding won't be promoted to "block".
            any_anchor_decreased = True
        elif post >= baseline and baseline > 0:
            delta_details.append(
                ConformanceFinding(
                    rule="hit_delta",
                    severity="warn",
                    message=(
                        f"Request references {anchor!r} but post-patch "
                        f"occurrence count did not decrease "
                        f"(before={baseline}, after={post})."
                    ),
                    evidence={
                        "anchor": anchor,
                        "count_before": baseline,
                        "count_after": post,
                        "minus_in_diff": minus,
                        "plus_in_diff": plus,
                    },
                )
            )

        # must_touch
        hit_paths = set(hit_files.keys())
        if hit_paths.isdisjoint(touched_files):
            touch_details.append(
                ConformanceFinding(
                    rule="must_touch",
                    severity="block",
                    message=(
                        f"Anchor {anchor!r} exists in the source tree but "
                        "the patch does not touch any file containing it."
                    ),
                    evidence={
                        "anchor": anchor,
                        "hit_files": sorted(hit_paths)[:20],
                        "touched_files": sorted(touched_files)[:20],
                    },
                )
            )

    # hit_delta: only promote to block when NO anchor decreased at all
    if not any_anchor_decreased and delta_details:
        delta_details = [
            ConformanceFinding(rule=f.rule, severity="block", message=f.message, evidence=f.evidence)
            for f in delta_details
        ]
    findings.extend(delta_details)
    findings.extend(touch_details)
    return findings


def _count_anchor_occurrences_in_diff(
    *, diff: str, anchors: Iterable[str]
) -> tuple[dict[str, int], dict[str, int]]:
    minus: dict[str, int] = {}
    plus: dict[str, int] = {}
    anchor_list = list(anchors)
    for line in diff.splitlines():
        if not line or line.startswith("---") or line.startswith("+++"):
            continue
        if line[0] == "-":
            for a in anchor_list:
                if a in line:
                    minus[a] = minus.get(a, 0) + line.count(a)
        elif line[0] == "+":
            for a in anchor_list:
                if a in line:
                    plus[a] = plus.get(a, 0) + line.count(a)
    return minus, plus


def _collect_files_touched_by_diff(diff: str) -> set[str]:
    shapes = _classify_files_in_diff(diff)
    return {p for p in shapes.keys()}


_SKIP_DIRS = frozenset({".git", "node_modules", "build", "dist", ".gradle", ".idea", "__pycache__"})
_MAX_SCAN_BYTES = 2 * 1024 * 1024  # 2 MiB per file


def _find_files_containing_anchor(
    source_tree: Path, anchor: str
) -> dict[str, int]:
    """Return {relative_path: occurrence_count} for files containing anchor.

    Plain-Python scan; avoids a hard dependency on ripgrep for portability.
    """
    counts: dict[str, int] = {}
    root = source_tree.resolve()
    for path in _iter_text_files(root):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            data = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = data.count(anchor)
        if hits:
            counts[str(rel).replace("\\", "/")] = hits
    return counts


def _iter_text_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in _SKIP_DIRS or name.startswith("."):
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                yield entry
