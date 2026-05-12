"""Failing-test-first gate: behavior bug tasks must have test evidence.

T-041-06: When the task describes a behavior bug (stale display, wrong
permission, login issue, etc.), require that either:
  - A new test was added in the diff (test file created or modified)
  - The test pipeline ran and passed
  - The task is manually approved

If none of these conditions are met, the gate warns (escalatable to block).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TestGateFinding:
    rule: str
    severity: str
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TestGateReport:
    verdict: str  # "pass" | "warn" | "block"
    is_behavior_bug: bool
    findings: tuple[TestGateFinding, ...]

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "is_behavior_bug": self.is_behavior_bug,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "message": f.message, "evidence": f.evidence}
                for f in self.findings
            ],
        }


_BEHAVIOR_BUG_PATTERNS = [
    re.compile(r"\b(stale|wrong|incorrect|broken|not\s+(?:updating|displaying|showing|working))\b", re.I),
    re.compile(r"\b(login|session|auth|permission|role)\b.*\b(bug|fix|issue|error|fail)\b", re.I),
    re.compile(r"\b(bug|fix|issue|error|fail)\b.*\b(login|session|auth|permission|role)\b", re.I),
    re.compile(r"\b(display|render|show|page|screen|UI|button|form)\b.*\b(bug|fix|issue|error|fail|wrong|broken)\b", re.I),
    re.compile(r"\b(bug|fix|issue|error|fail|wrong|broken)\b.*\b(display|render|show|page|screen|UI|button|form)\b", re.I),
    re.compile(r"\b(console\s+error|network\s+error|500|404|403|401)\b", re.I),
    re.compile(r"\b(regression|flak[ey]|intermittent)\b", re.I),
]

_TEST_FILE_PATTERNS = [
    re.compile(r"test[_/]"),
    re.compile(r"_test\."),
    re.compile(r"\.test\."),
    re.compile(r"\.spec\."),
    re.compile(r"__tests__/"),
    re.compile(r"tests/"),
]


def check_failing_test_gate(
    *,
    request_text: str,
    file_shapes: dict[str, str],
    test_result: dict | None,
) -> TestGateReport:
    """Check if behavior-bug tasks have adequate test evidence."""
    is_behavior_bug = _is_behavior_bug(request_text)
    findings: list[TestGateFinding] = []

    if not is_behavior_bug:
        return TestGateReport(
            verdict="pass",
            is_behavior_bug=False,
            findings=(),
        )

    has_test_in_diff = any(
        _is_test_file(path) for path in file_shapes
    )

    test_passed = (
        isinstance(test_result, dict)
        and bool(test_result.get("overall_passed"))
    )

    test_skipped = (
        isinstance(test_result, dict)
        and str(test_result.get("status", "")).lower() == "skipped"
    )

    if has_test_in_diff and test_passed:
        return TestGateReport(
            verdict="pass",
            is_behavior_bug=True,
            findings=(),
        )

    if has_test_in_diff and not test_passed and not test_skipped:
        findings.append(TestGateFinding(
            rule="behavior_test_present_but_failing",
            severity="block",
            message="Behavior bug task has test file in diff but tests did not pass.",
            evidence={"test_result_status": test_result.get("status") if test_result else None},
        ))

    if not has_test_in_diff:
        findings.append(TestGateFinding(
            rule="behavior_no_test_evidence",
            severity="warn",
            message=(
                "Behavior bug task has no test file in the diff. "
                "Consider adding a regression test to verify the fix."
            ),
            evidence={
                "files_in_diff": sorted(file_shapes.keys())[:10],
                "test_pipeline_ran": test_result is not None,
                "test_pipeline_passed": test_passed,
            },
        ))

    verdict = "block" if any(f.severity == "block" for f in findings) else "warn"
    return TestGateReport(
        verdict=verdict,
        is_behavior_bug=True,
        findings=tuple(findings),
    )


def _is_behavior_bug(text: str) -> bool:
    return any(p.search(text) for p in _BEHAVIOR_BUG_PATTERNS)


def _is_test_file(path: str) -> bool:
    return any(p.search(path) for p in _TEST_FILE_PATTERNS)
