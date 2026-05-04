"""Static shape check for unified diffs (Stage X.1 dogfood-trigger fix).

Codex consult verdict on today's P69-19 dogfood: review gates passed a
patch that deleted 11 lines from each of two must_touch files and added
zero lines, claiming "all goals met". A trivial static check on the diff
counts catches this.

Rule v1:
  - For each file in the diff, count + (added_lines) and - (removed_lines).
  - If TOTAL added_lines == 0 and removed_lines > 0 -> destructive, reject.
  - If a must_touch file has added_lines == 0 -> destructive for that file
    (planner asked to MODIFY it; pure deletion means work not done).

Future work (deferred):
  - Per-file positive-evidence check: must_touch file should contain
    tokens from the requested-constructs list (e.g. "AddressPicker",
    "rememberMapState") to count as actual implementation.
  - Allowlist for explicit deletion-only/refactor tasks.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShapeCheckResult:
    destructive: bool
    reason: str
    per_file: dict[str, dict[str, int]]
    totals: dict[str, int]

    def to_payload(self) -> dict[str, object]:
        return {
            "destructive": self.destructive,
            "reason": self.reason,
            "per_file": dict(self.per_file),
            "totals": dict(self.totals),
        }


def analyze_diff(diff: str) -> dict[str, dict[str, int]]:
    """Parse unified-diff text into per-file {added, removed} counts.

    Files are keyed by the b-side path from `diff --git a/FOO b/BAR`.
    Lines starting with `+` (added) or `-` (removed) but NOT the file
    header `+++ ` / `--- ` are counted.
    """
    per_file: dict[str, dict[str, int]] = {}
    current_path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            # form: diff --git a/<path-a> b/<path-b>
            parts = raw.split(" b/", 1)
            if len(parts) == 2:
                current_path = parts[1].strip()
                per_file.setdefault(current_path, {"added": 0, "removed": 0})
            continue
        if current_path is None:
            continue
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue  # file header
        if raw.startswith("@@ "):
            continue  # hunk header
        if raw.startswith("+"):
            per_file[current_path]["added"] += 1
        elif raw.startswith("-"):
            per_file[current_path]["removed"] += 1
    return per_file


_DELETION_INTENT_TOKENS = (
    "remove", "delete", "strip", "drop", "uninstall", "deprecate",
    "clean up", "obsolete", "purge",
)


def _looks_like_deletion_intent(text: str) -> bool:
    """True if the task description signals deletion-only / refactor intent."""
    lower = (text or "").lower()
    return any(token in lower for token in _DELETION_INTENT_TOKENS)


def evaluate_patch_shape(
    diff: str,
    must_touch_files: list[str] | None = None,
    task_intent: str = "",
) -> ShapeCheckResult:
    """Static shape evaluation. See module docstring for rules."""
    per_file = analyze_diff(diff or "")
    totals = {
        "added": sum(stats["added"] for stats in per_file.values()),
        "removed": sum(stats["removed"] for stats in per_file.values()),
    }

    if not per_file:
        return ShapeCheckResult(
            destructive=False,
            reason="empty diff (no file headers)",
            per_file=per_file,
            totals=totals,
        )

    # Totals-only rule (added=0 && removed>0 -> reject) was too aggressive
    # and falsely flagged legitimate deletion tasks like 'Remove X from
    # foo.py'. Codex consult verdict: 'unless explicitly classified as
    # deletion-only/refactor'. v1 keeps only must_touch-specific rule.
    # Pure-deletion of a must_touch file is still rejected below.

    deletion_intent = _looks_like_deletion_intent(task_intent)
    must_touch = {p.strip() for p in (must_touch_files or []) if isinstance(p, str) and p.strip()}
    if must_touch and not deletion_intent:
        for path, stats in per_file.items():
            for required in must_touch:
                # Match either exact or with source-name prefix tolerance
                # (handyman-admin-dashboard/src/foo == src/foo)
                if path == required or path.endswith("/" + required) or required.endswith("/" + path):
                    if stats["added"] == 0 and stats["removed"] > 0:
                        return ShapeCheckResult(
                            destructive=True,
                            reason=(
                                f"must_touch file {path!r} is pure-deletion "
                                f"(added=0 removed={stats['removed']}). Planner asked "
                                f"to MODIFY this file; pure deletion means work not done."
                            ),
                            per_file=per_file,
                            totals=totals,
                        )
    if must_touch and deletion_intent:
        return ShapeCheckResult(
            destructive=False,
            reason=(
                f"must_touch deletion check skipped: task intent looks like "
                f"explicit deletion/refactor ({task_intent[:80]!r})"
            ),
            per_file=per_file,
            totals=totals,
        )

    return ShapeCheckResult(
        destructive=False,
        reason="patch shape ok",
        per_file=per_file,
        totals=totals,
    )
