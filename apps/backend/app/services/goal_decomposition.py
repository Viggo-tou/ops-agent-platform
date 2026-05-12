"""Goal-by-goal conformance and per-file justification.

T-041-08: Instead of a single pass/fail on the whole task, decompose the
request into sub-goals and verify each one independently. Also require that
every file in the diff has a justification (connection to a sub-goal).

This builds on top of `build_goal_attestation` by adding:
1. Sub-goal decomposition from the request text
2. Per-file relevance scoring against sub-goals
3. Unjustified file detection
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SubGoal:
    text: str
    anchors: list[str]
    status: str  # "achieved" | "not_achieved" | "no_anchor"
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FileJustification:
    file_path: str
    shape: str
    related_goals: list[str]
    justified: bool


@dataclass(frozen=True)
class GoalReport:
    sub_goals: tuple[SubGoal, ...]
    file_justifications: tuple[FileJustification, ...]
    all_goals_met: bool
    unjustified_files: list[str]
    verdict: str  # "pass" | "warn" | "block"

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "all_goals_met": self.all_goals_met,
            "sub_goals": [
                {"text": g.text, "anchors": g.anchors, "status": g.status, "evidence": g.evidence}
                for g in self.sub_goals
            ],
            "file_justifications": [
                {"file": f.file_path, "shape": f.shape, "related_goals": f.related_goals, "justified": f.justified}
                for f in self.file_justifications
            ],
            "unjustified_files": self.unjustified_files,
        }


_GOAL_SPLIT_RE = re.compile(
    r"(?:,\s*(?:and\s+)?|;\s*|\band\b\s+(?=\w+\s+(?:the|a|in|from|to)\b))"
    r"|(?:\d+\.\s+)"
    r"|(?:\n\s*[-*]\s+)",
    re.I,
)

_ANCHOR_RE = re.compile(r"""['"`]([^'"`\n]{2,40})['"`]""")
_IDENT_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b|\b[a-z]+[0-9]+\b")


def decompose_and_verify(
    *,
    request_text: str,
    diff: str,
    file_shapes: dict[str, str],
    source_tree: Path | None,
    attestation: dict | None = None,
) -> GoalReport:
    """Decompose request into sub-goals and verify each, plus justify each file."""
    sub_goals = _decompose_goals(request_text)

    verified_goals: list[SubGoal] = []
    all_met = True

    anchor_status = {}
    if isinstance(attestation, dict):
        for a in attestation.get("anchors", []):
            anchor_status[a.get("anchor", "")] = a.get("status", "")

    for goal in sub_goals:
        anchors = _extract_anchors(goal)
        if not anchors:
            verified_goals.append(SubGoal(
                text=goal, anchors=[], status="no_anchor",
                evidence={"note": "No specific anchor to verify"},
            ))
            continue

        goal_achieved = True
        goal_evidence: dict = {}
        for anchor in anchors:
            status = anchor_status.get(anchor, "unknown")
            goal_evidence[anchor] = status
            if status == "not_achieved":
                goal_achieved = False
                all_met = False

        verified_goals.append(SubGoal(
            text=goal,
            anchors=anchors,
            status="achieved" if goal_achieved else "not_achieved",
            evidence=goal_evidence,
        ))

    file_justifications: list[FileJustification] = []
    unjustified: list[str] = []

    for filepath, shape in file_shapes.items():
        related = _find_related_goals(filepath, sub_goals, request_text)
        justified = len(related) > 0
        if not justified:
            if _is_support_file(filepath):
                justified = True
                related = ["support/config"]
        file_justifications.append(FileJustification(
            file_path=filepath,
            shape=shape,
            related_goals=related,
            justified=justified,
        ))
        if not justified:
            unjustified.append(filepath)

    verdict = "pass"
    if not all_met:
        verdict = "warn"
    if unjustified:
        verdict = "warn"

    return GoalReport(
        sub_goals=tuple(verified_goals),
        file_justifications=tuple(file_justifications),
        all_goals_met=all_met,
        unjustified_files=unjustified,
        verdict=verdict,
    )


def _decompose_goals(request_text: str) -> list[str]:
    """Split request into sub-goals."""
    parts = _GOAL_SPLIT_RE.split(request_text)
    goals = [p.strip() for p in parts if p and len(p.strip()) > 10]
    if not goals:
        goals = [request_text.strip()]
    return goals[:10]


def _extract_anchors(text: str) -> list[str]:
    anchors: list[str] = []
    seen: set[str] = set()
    for m in _ANCHOR_RE.finditer(text):
        raw = m.group(1).strip()
        if raw.lower() not in seen and len(raw) >= 3:
            seen.add(raw.lower())
            anchors.append(raw)
    for m in _IDENT_RE.finditer(text):
        raw = m.group(0)
        if raw.lower() not in seen and len(raw) >= 4:
            seen.add(raw.lower())
            anchors.append(raw)
    return anchors


def _find_related_goals(filepath: str, goals: list[str], request_text: str) -> list[str]:
    """Find which goals mention this file or its basename."""
    related: list[str] = []
    basename = filepath.rsplit("/", 1)[-1] if "/" in filepath else filepath
    name_no_ext = basename.rsplit(".", 1)[0] if "." in basename else basename

    for goal in goals:
        goal_lower = goal.lower()
        if basename.lower() in goal_lower or name_no_ext.lower() in goal_lower:
            related.append(goal[:80])
        elif filepath.lower() in goal_lower:
            related.append(goal[:80])

    if not related and (basename.lower() in request_text.lower() or filepath.lower() in request_text.lower()):
        related.append("mentioned in request")

    return related


def _is_support_file(filepath: str) -> bool:
    support_patterns = [
        r"package\.json$", r"tsconfig", r"\.config\.", r"\.lock$",
        r"test[_/]", r"_test\.", r"\.test\.", r"\.spec\.",
    ]
    return any(re.search(p, filepath) for p in support_patterns)
