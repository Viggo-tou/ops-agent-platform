"""Acceptance test evaluation gate.

The planner emits a list of acceptance_tests as part of its plan output.
After codegen produces a diff and before approval, this module checks
the diff against each test and produces a report. It's a structural
gate — no LLM, no file system reads of the patched repo, just diff
parsing.

Why this is stronger than feature_presence_check:

  - feature_presence is token-level: "does the file contain the word
    'mask' anywhere after applying?". A diff that adds a code comment
    with the word satisfies that gate.
  - acceptance_check is shape-level: "did the diff add a real `if
    mask is None:` branch?". Code comments don't satisfy that.

Test kinds (this is the V1 set; we add more as failure modes surface):

  - diff_contains_pattern: substring or regex must appear in any
    +-prefixed line.
  - diff_contains_pattern_in_file: same, scoped to a specific file.
  - function_signature_unchanged: named function's `def NAME(...)`
    line must not appear with a `-` prefix in the diff.
  - function_signature_changed: same line MUST appear with `-` and
    `+` (it was modified).
  - no_new_file_outside: no diff for a path created (`new file mode`)
    outside the named directory scope.
  - import_added: pattern must appear on an import-shaped added line.

Each test carries a free-text rationale that the planner explains
*why* it asks for that check; the reviewer reads it for human-
readable feedback when a test fails.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


_KNOWN_KINDS = frozenset(
    {
        "diff_contains_pattern",
        "diff_contains_pattern_in_file",
        "function_signature_unchanged",
        "function_signature_changed",
        "no_new_file_outside",
        "import_added",
    }
)


@dataclass(frozen=True)
class AcceptanceTest:
    kind: str
    pattern: str = ""
    file: str | None = None
    function: str | None = None
    scope: str | None = None
    rationale: str = ""


@dataclass(frozen=True)
class AcceptanceResult:
    test: AcceptanceTest
    matched: bool
    reason: str


@dataclass(frozen=True)
class AcceptanceReport:
    passed: bool
    results: list[AcceptanceResult] = field(default_factory=list)


# --- diff walking helpers ----------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_NEW_FILE_RE = re.compile(r"^new file mode \d+$")


def _iter_added_lines(diff: str):
    """Yield (file_path, content) for every +-prefixed line in the diff."""
    current_path: str | None = None
    for raw in diff.splitlines():
        header = _DIFF_HEADER_RE.match(raw)
        if header is not None:
            current_path = header.group(2)
            continue
        if current_path is None:
            continue
        if raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            yield current_path, raw[1:]


def _iter_removed_lines(diff: str):
    current_path: str | None = None
    for raw in diff.splitlines():
        header = _DIFF_HEADER_RE.match(raw)
        if header is not None:
            current_path = header.group(2)
            continue
        if current_path is None:
            continue
        if raw.startswith("---"):
            continue
        if raw.startswith("-"):
            yield current_path, raw[1:]


def _iter_new_files(diff: str):
    """Yield path for each `new file mode` block in the diff."""
    current_path: str | None = None
    for raw in diff.splitlines():
        header = _DIFF_HEADER_RE.match(raw)
        if header is not None:
            current_path = header.group(2)
            continue
        if current_path is not None and _NEW_FILE_RE.match(raw):
            yield current_path


def _is_python_def_line(content: str, function: str) -> bool:
    return bool(re.match(rf"^\s*def\s+{re.escape(function)}\s*\(", content))


def _is_import_line(content: str) -> bool:
    return (
        bool(re.match(r"^\s*(?:from\s+\S+\s+)?import\s+\S+", content))
        or bool(re.match(r"^\s*import\s+[A-Za-z_][\w.]*(?:\.\*)?\s*;?\s*$", content))
        or bool(re.match(r"^\s*import(?:\s+(?:.+?)\s+from)?\s+['\"]", content))
    )


# --- per-kind evaluators ------------------------------------------------------


def _evaluate_diff_contains_pattern(test: AcceptanceTest, diff: str) -> AcceptanceResult:
    pattern = test.pattern
    for _path, added in _iter_added_lines(diff):
        if pattern in added:
            return AcceptanceResult(test, True, f"found pattern in added lines")
    return AcceptanceResult(test, False, f"pattern {pattern!r} not in any added line")


def _evaluate_diff_contains_pattern_in_file(test: AcceptanceTest, diff: str) -> AcceptanceResult:
    if not test.file:
        return AcceptanceResult(test, False, "no file specified for in-file pattern test")
    pattern = test.pattern
    for path, added in _iter_added_lines(diff):
        if path == test.file and pattern in added:
            return AcceptanceResult(test, True, f"found pattern in {test.file} added lines")
    return AcceptanceResult(
        test,
        False,
        f"pattern {pattern!r} not in added lines of {test.file}",
    )


def _evaluate_function_signature_unchanged(
    test: AcceptanceTest, diff: str
) -> AcceptanceResult:
    if not test.function:
        return AcceptanceResult(test, False, "no function specified")
    for _path, removed in _iter_removed_lines(diff):
        if _is_python_def_line(removed, test.function):
            return AcceptanceResult(
                test,
                False,
                f"def {test.function}(...) removed (signature changed)",
            )
    return AcceptanceResult(
        test, True, f"def {test.function}(...) not removed in diff"
    )


def _evaluate_function_signature_changed(
    test: AcceptanceTest, diff: str
) -> AcceptanceResult:
    if not test.function:
        return AcceptanceResult(test, False, "no function specified")
    has_remove = any(
        _is_python_def_line(content, test.function)
        for _path, content in _iter_removed_lines(diff)
    )
    has_add = any(
        _is_python_def_line(content, test.function)
        for _path, content in _iter_added_lines(diff)
    )
    if has_remove and has_add:
        return AcceptanceResult(
            test, True, f"def {test.function}(...) replaced (old removed + new added)"
        )
    return AcceptanceResult(
        test,
        False,
        f"signature change not observed for {test.function}",
    )


def _evaluate_no_new_file_outside(test: AcceptanceTest, diff: str) -> AcceptanceResult:
    if not test.scope:
        return AcceptanceResult(test, False, "no scope specified")
    scope = test.scope.rstrip("/") + "/"
    offenders: list[str] = []
    for path in _iter_new_files(diff):
        normalized = PurePosixPath(path).as_posix()
        if not normalized.startswith(scope):
            offenders.append(normalized)
    if offenders:
        return AcceptanceResult(
            test, False, f"new file(s) outside {scope}: {offenders}"
        )
    return AcceptanceResult(test, True, f"no new files outside {scope}")


def _evaluate_import_added(test: AcceptanceTest, diff: str) -> AcceptanceResult:
    pattern = test.pattern
    for _path, added in _iter_added_lines(diff):
        if _is_import_line(added) and pattern in added:
            return AcceptanceResult(test, True, "import added")
    return AcceptanceResult(
        test, False, f"import matching {pattern!r} not added in any file"
    )


_EVALUATORS = {
    "diff_contains_pattern": _evaluate_diff_contains_pattern,
    "diff_contains_pattern_in_file": _evaluate_diff_contains_pattern_in_file,
    "function_signature_unchanged": _evaluate_function_signature_unchanged,
    "function_signature_changed": _evaluate_function_signature_changed,
    "no_new_file_outside": _evaluate_no_new_file_outside,
    "import_added": _evaluate_import_added,
}


def evaluate_acceptance(
    diff: str, tests: list[AcceptanceTest]
) -> AcceptanceReport:
    results: list[AcceptanceResult] = []
    for test in tests:
        evaluator = _EVALUATORS.get(test.kind)
        if evaluator is None:
            results.append(
                AcceptanceResult(
                    test, False, f"unknown acceptance test kind: {test.kind!r}"
                )
            )
            continue
        results.append(evaluator(test, diff))
    passed = all(result.matched for result in results)
    return AcceptanceReport(passed=passed, results=results)
