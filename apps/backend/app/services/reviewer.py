from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Protocol


DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "**/migrations/**",
    "**/.env*",
    "**/secrets/**",
    "**/*.pem",
    "**/*.key",
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Only match hardcoded string values (quoted), not variable assignments
    re.compile(r"""password\s*=\s*["'][^"']{4,}["']""", re.IGNORECASE),
    re.compile(r"""api_key\s*=\s*["'][^"']{4,}["']""", re.IGNORECASE),
    re.compile(r"""secret\s*=\s*["'][^"']{4,}["']""", re.IGNORECASE),
    re.compile(r"""token\s*=\s*["'][^"']{4,}["']""", re.IGNORECASE),
    re.compile(r"-----BEGIN .* PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


@dataclass
class ReviewContext:
    diff: str
    test_result: dict | None = None
    task_description: str = ""
    changed_files: list[str] = field(default_factory=list)


@dataclass
class ReviewViolation:
    rule_name: str
    severity: str
    message: str


@dataclass
class ReviewResult:
    verdict: str
    violations: list[ReviewViolation]
    rules_checked: int
    duration_ms: int


class ReviewRule(Protocol):
    def __call__(self, context: ReviewContext) -> ReviewViolation | None:
        ...


class DiffReviewer:
    def __init__(
        self,
        *,
        protected_paths: list[str] | None = None,
        max_diff_size: int = 50_000,
    ):
        self.protected_paths = tuple(protected_paths) if protected_paths is not None else DEFAULT_PROTECTED_PATHS
        self.max_diff_size = max_diff_size
        self.rules: dict[str, ReviewRule] = {
            "tests-must-pass": self._tests_must_pass,
            "no-secrets": self._no_secrets,
            "protected-paths": self._protected_paths,
            "max-diff-size": self._max_diff_size,
        }

    def review(self, context: ReviewContext) -> ReviewResult:
        """Run all rules, return verdict."""
        started = time.monotonic()
        if not context.changed_files:
            context.changed_files = self.parse_changed_files(context.diff)

        violations: list[ReviewViolation] = []
        rules_checked = 0
        for rule_name, rule in self.rules.items():
            if rule_name == "tests-must-pass" and context.test_result is None:
                continue

            rules_checked += 1
            violation = rule(context)
            if violation is not None:
                violations.append(violation)

        verdict = "block" if any(violation.severity == "block" for violation in violations) else "pass"
        duration_ms = int((time.monotonic() - started) * 1000)
        return ReviewResult(
            verdict=verdict,
            violations=violations,
            rules_checked=rules_checked,
            duration_ms=duration_ms,
        )

    @staticmethod
    def parse_changed_files(diff: str) -> list[str]:
        """Extract file paths from unified diff headers (--- a/... / +++ b/...)."""
        changed_files: list[str] = []
        seen: set[str] = set()

        for line in diff.splitlines():
            if not (line.startswith("--- ") or line.startswith("+++ ")):
                continue

            path = _clean_diff_path(line[4:])
            if path is None or path in seen:
                continue

            seen.add(path)
            changed_files.append(path)

        return changed_files

    def _tests_must_pass(self, context: ReviewContext) -> ReviewViolation | None:
        if context.test_result is None or context.test_result.get("overall_passed") is True:
            return None

        return ReviewViolation(
            rule_name="tests-must-pass",
            severity="block",
            message="Test pipeline result is present but overall_passed is not true.",
        )

    def _no_secrets(self, context: ReviewContext) -> ReviewViolation | None:
        for line in context.diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue

            added_text = line[1:]
            if any(pattern.search(added_text) for pattern in SECRET_PATTERNS):
                return ReviewViolation(
                    rule_name="no-secrets",
                    severity="block",
                    message="Diff adds a line that matches a common secret pattern.",
                )

        return None

    def _protected_paths(self, context: ReviewContext) -> ReviewViolation | None:
        for file_path in context.changed_files:
            if self._matches_protected_path(file_path):
                return ReviewViolation(
                    rule_name="protected-paths",
                    severity="block",
                    message=f"Diff touches protected path: {file_path}",
                )

        return None

    def _max_diff_size(self, context: ReviewContext) -> ReviewViolation | None:
        if len(context.diff) <= self.max_diff_size:
            return None

        return ReviewViolation(
            rule_name="max-diff-size",
            severity="block",
            message=f"Diff is {len(context.diff)} characters, above the limit of {self.max_diff_size}.",
        )

    def _matches_protected_path(self, file_path: str) -> bool:
        normalized_path = _normalize_path(file_path)
        for pattern in self.protected_paths:
            for candidate in _glob_variants(pattern):
                if fnmatch.fnmatchcase(normalized_path, candidate):
                    return True
        return False


def _clean_diff_path(raw_path: str) -> str | None:
    path = raw_path.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0].strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]

    path = _normalize_path(path)
    return path or None


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _glob_variants(pattern: str) -> tuple[str, ...]:
    normalized = _normalize_path(pattern)
    if normalized.startswith("**/"):
        return (normalized, normalized[3:])
    return (normalized,)
