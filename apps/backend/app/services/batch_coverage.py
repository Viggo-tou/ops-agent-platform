"""Must-touch / expected-new coverage gate over codegen batch outcomes.

v15 Ticket 2B (2026-05-11). v14 showed that the orchestrator was
willing to continue into compile / review even when 1 of 2 must_touch
batches silently no-changed itself — a "partial success" path that
let the model drop required scope without anyone noticing.

This module classifies each codegen batch into a structured outcome
and applies the user-confirmed Rule A–D over the plan's file-role
declarations:

- Rule A: every ``must_touch_file`` MUST end up patched.
- Rule B: ``must_touch + verified_no_change`` is a planner/codegen
  conflict → awaiting_approval, NOT failed.
- Rule C: ``must_touch + phantom_no_change`` is a model failure →
  failed (retries already consumed by codegen.py's inner loop).
- Rule D: ``expected_new_files`` MUST be created.
- ``likely_touch_files`` may verified-no-change; phantom still fails.
- ``must_inspect_files`` are read-only — they should never appear as
  codegen batch targets at all.

Pure logic: no DB, no LLM, no IO. The orchestrator wires events and
state transitions; this module just answers "given these batch results,
what verdict?".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


FileRole = Literal[
    "must_touch",
    "must_inspect",
    "likely_touch",
    "expected_new",
    "unknown",
]


BatchStatus = Literal[
    "patched",
    "phantom_no_change",
    "verified_no_change",
    "non_executable_patch",
    "no_output",
    "other_failure",
    "missing_batch",
]


@dataclass
class BatchOutcome:
    """Per-file batch result; everything the coverage gate + UI needs.

    ``batch_id`` is the index assigned by the orchestrator (e.g.
    "batch-1/2") — purely for human display.
    """

    file_path: str
    role: FileRole
    status: BatchStatus
    reason: str = ""
    attempts: int = 1
    provider: str = ""
    batch_id: str = ""
    terminal_kind: str = ""  # NO_CHANGE_NEEDED_VERIFIED / PHANTOM_NO_CHANGE / ...
    diff_stats: dict[str, int] = field(default_factory=lambda: {"added": 0, "removed": 0})
    verified_evidence_count: int = 0
    failed_quotes: list[dict[str, str]] = field(default_factory=list)
    changed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "role": self.role,
            "status": self.status,
            "reason": self.reason[:500],
            "attempts": self.attempts,
            "provider": self.provider,
            "batch_id": self.batch_id,
            "terminal_kind": self.terminal_kind,
            "diff_stats": dict(self.diff_stats),
            "verified_evidence_count": self.verified_evidence_count,
            "failed_quotes": list(self.failed_quotes),
            "changed": self.changed,
        }


_PHANTOM_RE = re.compile(r"PHANTOM_NO_CHANGE", re.IGNORECASE)
_VERIFIED_NO_CHANGE_RE = re.compile(r"NO_CHANGE_NEEDED_VERIFIED", re.IGNORECASE)
_EVIDENCE_GAP_RE = re.compile(r"EVIDENCE_GAP", re.IGNORECASE)


def _normalize_path(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    return text.strip("/")


def role_for_path(path: str, plan: Any) -> FileRole:
    """Look up a file's declared role in the plan.

    Suffix-tolerant: ``a/src/foo.kt`` matches ``src/foo.kt``. This is
    the same convention evidence_chain uses.
    """
    needle = _normalize_path(path)
    if not needle:
        return "unknown"

    def _match(haystack: list[str]) -> bool:
        for entry in haystack or []:
            if not isinstance(entry, str):
                continue
            candidate = _normalize_path(entry)
            if not candidate:
                continue
            if candidate == needle:
                return True
            if candidate.endswith("/" + needle) or needle.endswith("/" + candidate):
                return True
        return False

    if _match(list(getattr(plan, "must_touch_files", []) or [])):
        return "must_touch"
    if _match(list(getattr(plan, "expected_new_files", []) or [])):
        return "expected_new"
    if _match(list(getattr(plan, "likely_touch_files", []) or [])):
        return "likely_touch"
    if _match(list(getattr(plan, "must_inspect_files", []) or [])):
        return "must_inspect"
    return "unknown"


def classify_batch_outcome(
    *,
    file_path: str,
    plan: Any,
    batch_result: dict | None,
    error: Exception | None,
    batch_id: str = "",
) -> BatchOutcome:
    """Translate raw worker output into a structured outcome.

    Status priority (when error is set):
    - "PHANTOM_NO_CHANGE" in message  → phantom_no_change
    - "NO_CHANGE_NEEDED_VERIFIED" in message → verified_no_change
    - "EVIDENCE_GAP" in message → other_failure (caller can sub-classify)
    - anything else with a message → other_failure
    """
    role = role_for_path(file_path, plan)
    outcome = BatchOutcome(
        file_path=_normalize_path(file_path) or file_path,
        role=role,
        status="other_failure",
        batch_id=batch_id,
    )

    if error is not None:
        msg = str(error)[:1000]
        outcome.reason = msg
        if _PHANTOM_RE.search(msg):
            outcome.status = "phantom_no_change"
            outcome.terminal_kind = "PHANTOM_NO_CHANGE"
        elif _VERIFIED_NO_CHANGE_RE.search(msg):
            outcome.status = "verified_no_change"
            outcome.terminal_kind = "NO_CHANGE_NEEDED_VERIFIED"
        elif _EVIDENCE_GAP_RE.search(msg):
            outcome.status = "other_failure"
            outcome.terminal_kind = "EVIDENCE_GAP"
        return outcome

    if not isinstance(batch_result, dict):
        outcome.status = "no_output"
        outcome.reason = "batch returned no result"
        return outcome

    diff_text = str(batch_result.get("diff") or "").strip()
    files_changed = batch_result.get("files_changed") or []
    provider = str(batch_result.get("provider_name") or "")

    outcome.provider = provider
    attempt_history = batch_result.get("attempt_history")
    if isinstance(attempt_history, list):
        outcome.attempts = max(1, len(attempt_history))

    if not diff_text:
        outcome.status = "no_output"
        outcome.reason = "empty diff returned"
        return outcome

    # Check this file's slice of the diff for added/removed line counts.
    added, removed = _count_added_removed(diff_text, outcome.file_path)
    outcome.diff_stats = {"added": added, "removed": removed}
    file_targeted = any(
        _normalize_path(str(f)) == outcome.file_path
        or outcome.file_path.endswith("/" + _normalize_path(str(f)))
        or _normalize_path(str(f)).endswith("/" + outcome.file_path)
        for f in files_changed
    )

    if file_targeted and (added > 0 or removed > 0):
        if not _has_executable_change(diff_text, outcome.file_path):
            outcome.status = "non_executable_patch"
            outcome.changed = False
            outcome.reason = "patch only changed comments or whitespace"
            return outcome
        outcome.status = "patched"
        outcome.changed = True
        outcome.reason = f"+{added}/-{removed}"
        return outcome

    # Diff exists but doesn't touch this file (e.g. the batch produced
    # hunks only for a companion file). Treat as no_output for THIS
    # file — the coverage gate decides what to do with it.
    outcome.status = "no_output"
    outcome.reason = "diff did not modify the target file"
    return outcome


def _has_executable_change(diff_text: str, target_path: str) -> bool:
    """Return true when the target diff changes non-comment code.

    A feature/bugfix patch that only adds comments can satisfy line-based
    "touched file" accounting while leaving behavior unchanged. Treating
    such hunks as uncovered keeps batch coverage aligned with executable
    task progress.
    """
    if not diff_text or not target_path:
        return False
    target_norm = _normalize_path(target_path)
    sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    for section in sections:
        m = re.match(r"diff --git a/(.+?) b/", section)
        if not m:
            continue
        seg = _normalize_path(m.group(1))
        if not (
            seg == target_norm
            or seg.endswith("/" + target_norm)
            or target_norm.endswith("/" + seg)
        ):
            continue
        for raw_line in section.splitlines():
            if not (raw_line.startswith("+") or raw_line.startswith("-")):
                continue
            if raw_line.startswith("+++") or raw_line.startswith("---"):
                continue
            text = raw_line[1:].strip()
            if not text:
                continue
            if _is_comment_only_line(text):
                continue
            return True
    return False


def _is_comment_only_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    comment_prefixes = ("//", "#", "/*", "*", "*/", "<!--", "-->")
    if stripped.startswith(comment_prefixes):
        return True
    if stripped.endswith("*/") and stripped.startswith("*"):
        return True
    return False


def _count_added_removed(diff_text: str, target_path: str) -> tuple[int, int]:
    """Count +/− line counts within the diff section for ``target_path``.

    Permissive: when the diff is a single concatenated patch covering
    multiple files, this returns counts for the slice matching
    ``target_path`` only. When no section matches, returns (0, 0).
    """
    if not diff_text or not target_path:
        return 0, 0
    sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    target_norm = _normalize_path(target_path)
    for section in sections:
        m = re.match(r"diff --git a/(.+?) b/", section)
        if not m:
            continue
        seg = _normalize_path(m.group(1))
        if not (
            seg == target_norm
            or seg.endswith("/" + target_norm)
            or target_norm.endswith("/" + seg)
        ):
            continue
        added = 0
        removed = 0
        for line in section.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return added, removed
    return 0, 0


# ---------------------------------------------------------------------------
# Coverage verdict
# ---------------------------------------------------------------------------


CoverageKind = Literal[
    "ok",
    "plan_codegen_conflict",          # must_touch + verified_no_change → awaiting_approval
    "phantom_no_change_unrecoverable",  # must_touch + phantom (codegen retries exhausted)
    "missing_must_touch",             # must_touch file's batch failed / no output
    "missing_expected_new",           # expected_new_file was not created
]


@dataclass
class CoverageVerdict:
    """Per-task verdict from the gate."""

    kind: CoverageKind
    summary: str
    must_touch_outcomes: list[BatchOutcome]
    expected_new_outcomes: list[BatchOutcome]
    conflicts: list[BatchOutcome]
    phantoms: list[BatchOutcome]
    failures: list[BatchOutcome]
    diagnostic: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "must_touch_outcomes": [o.to_payload() for o in self.must_touch_outcomes],
            "expected_new_outcomes": [o.to_payload() for o in self.expected_new_outcomes],
            "conflicts": [o.to_payload() for o in self.conflicts],
            "phantoms": [o.to_payload() for o in self.phantoms],
            "failures": [o.to_payload() for o in self.failures],
            "diagnostic": dict(self.diagnostic),
        }


def check_coverage(
    outcomes: list[BatchOutcome],
    plan: Any,
) -> CoverageVerdict:
    """Apply Rule A–D to a list of batch outcomes.

    Precedence when multiple violations occur (most-blocking first):
    1. phantom_no_change_unrecoverable (Rule C) — model failure
    2. missing_must_touch (Rule A) — batch didn't produce anything usable
    3. missing_expected_new (Rule D) — file should have been created
    4. plan_codegen_conflict (Rule B) — verified no-change → human

    The precedence is intentional: phantoms and missing batches are
    things the system can identify as "broken", while a plan/codegen
    conflict requires human judgment and is the softest landing.
    """
    must_touch_paths = {
        _normalize_path(p)
        for p in (getattr(plan, "must_touch_files", []) or [])
        if isinstance(p, str)
    }
    expected_new_paths = {
        _normalize_path(p)
        for p in (getattr(plan, "expected_new_files", []) or [])
        if isinstance(p, str)
    }
    must_touch_paths.discard("")
    expected_new_paths.discard("")

    must_touch_outcomes = [o for o in outcomes if o.role == "must_touch"]
    expected_new_outcomes = [o for o in outcomes if o.role == "expected_new"]

    must_touch_seen = {o.file_path for o in must_touch_outcomes}
    expected_new_seen = {o.file_path for o in expected_new_outcomes}

    # Synthesise missing-batch outcomes so the verdict carries them
    # explicitly (the orchestrator can decide if 'batch never dispatched'
    # is its own failure mode separately).
    for path in must_touch_paths - must_touch_seen:
        must_touch_outcomes.append(
            BatchOutcome(
                file_path=path,
                role="must_touch",
                status="missing_batch",
                reason="no codegen batch was dispatched for this must_touch file",
            )
        )
    for path in expected_new_paths - expected_new_seen:
        expected_new_outcomes.append(
            BatchOutcome(
                file_path=path,
                role="expected_new",
                status="missing_batch",
                reason="no codegen batch was dispatched for this expected_new file",
            )
        )

    phantoms = [
        o for o in must_touch_outcomes if o.status == "phantom_no_change"
    ]
    conflicts = [
        o for o in must_touch_outcomes if o.status == "verified_no_change"
    ]
    missing_must = [
        o for o in must_touch_outcomes
        if o.status in {
            "no_output",
            "other_failure",
            "missing_batch",
            "non_executable_patch",
        }
    ]
    missing_new = [
        o for o in expected_new_outcomes
        if o.status in {
            "no_output",
            "other_failure",
            "missing_batch",
            "non_executable_patch",
        }
    ]

    diagnostic = {
        "must_touch_count": len(must_touch_outcomes),
        "must_touch_patched": sum(
            1 for o in must_touch_outcomes if o.status == "patched"
        ),
        "must_touch_phantom": len(phantoms),
        "must_touch_verified_no_change": len(conflicts),
        "must_touch_missing": len(missing_must),
        "expected_new_count": len(expected_new_outcomes),
        "expected_new_patched": sum(
            1 for o in expected_new_outcomes if o.status == "patched"
        ),
        "expected_new_missing": len(missing_new),
        "likely_touch_count": sum(
            1 for o in outcomes if o.role == "likely_touch"
        ),
        "likely_touch_phantom": sum(
            1 for o in outcomes
            if o.role == "likely_touch" and o.status == "phantom_no_change"
        ),
    }

    if phantoms:
        files = ", ".join(o.file_path for o in phantoms[:5])
        return CoverageVerdict(
            kind="phantom_no_change_unrecoverable",
            summary=(
                f"Codegen retries exhausted with PHANTOM_NO_CHANGE on "
                f"{len(phantoms)} must_touch file(s): {files}. Model "
                "claimed the feature already existed but the quoted "
                "evidence could not be verified against the file content."
            ),
            must_touch_outcomes=must_touch_outcomes,
            expected_new_outcomes=expected_new_outcomes,
            conflicts=conflicts,
            phantoms=phantoms,
            failures=missing_must + missing_new,
            diagnostic=diagnostic,
        )

    # likely_touch phantom also fails (model failure, not human concern).
    likely_phantoms = [
        o for o in outcomes
        if o.role == "likely_touch" and o.status == "phantom_no_change"
    ]
    if likely_phantoms:
        files = ", ".join(o.file_path for o in likely_phantoms[:5])
        return CoverageVerdict(
            kind="phantom_no_change_unrecoverable",
            summary=(
                f"Codegen retries exhausted with PHANTOM_NO_CHANGE on "
                f"{len(likely_phantoms)} likely_touch file(s): {files}."
            ),
            must_touch_outcomes=must_touch_outcomes,
            expected_new_outcomes=expected_new_outcomes,
            conflicts=conflicts,
            phantoms=likely_phantoms,
            failures=missing_must + missing_new,
            diagnostic=diagnostic,
        )

    if missing_must:
        files = ", ".join(o.file_path for o in missing_must[:5])
        return CoverageVerdict(
            kind="missing_must_touch",
            summary=(
                f"{len(missing_must)} must_touch file(s) were not "
                f"successfully patched: {files}. Partial completion "
                "is not permitted to continue into compile/review."
            ),
            must_touch_outcomes=must_touch_outcomes,
            expected_new_outcomes=expected_new_outcomes,
            conflicts=conflicts,
            phantoms=phantoms,
            failures=missing_must,
            diagnostic=diagnostic,
        )

    if missing_new:
        files = ", ".join(o.file_path for o in missing_new[:5])
        return CoverageVerdict(
            kind="missing_expected_new",
            summary=(
                f"{len(missing_new)} expected_new_file(s) were not "
                f"created: {files}."
            ),
            must_touch_outcomes=must_touch_outcomes,
            expected_new_outcomes=expected_new_outcomes,
            conflicts=conflicts,
            phantoms=phantoms,
            failures=missing_new,
            diagnostic=diagnostic,
        )

    if conflicts:
        files = ", ".join(o.file_path for o in conflicts[:5])
        return CoverageVerdict(
            kind="plan_codegen_conflict",
            summary=(
                "Planner marked "
                f"{len(conflicts)} file(s) as must_touch but codegen "
                f"returned verified NO_CHANGE_NEEDED for them: {files}. "
                "Human review required to decide whether the planner "
                "scope was too aggressive or the codegen interpretation "
                "is too shallow."
            ),
            must_touch_outcomes=must_touch_outcomes,
            expected_new_outcomes=expected_new_outcomes,
            conflicts=conflicts,
            phantoms=phantoms,
            failures=missing_must + missing_new,
            diagnostic=diagnostic,
        )

    return CoverageVerdict(
        kind="ok",
        summary=(
            f"Coverage gate passed: {diagnostic['must_touch_patched']}/"
            f"{diagnostic['must_touch_count']} must_touch patched, "
            f"{diagnostic['expected_new_patched']}/"
            f"{diagnostic['expected_new_count']} expected_new created."
        ),
        must_touch_outcomes=must_touch_outcomes,
        expected_new_outcomes=expected_new_outcomes,
        conflicts=conflicts,
        phantoms=phantoms,
        failures=[],
        diagnostic=diagnostic,
    )
