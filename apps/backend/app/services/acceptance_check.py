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
  - final_file_forbids_pattern_in_file: a touched final file must not
    contain the forbidden regex.

Each test carries a free-text rationale that the planner explains
*why* it asks for that check; the reviewer reads it for human-
readable feedback when a test fails.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable


_KNOWN_KINDS = frozenset(
    {
        "diff_contains_pattern",
        "diff_contains_pattern_in_file",
        "function_signature_unchanged",
        "function_signature_changed",
        "no_new_file_outside",
        "import_added",
        "final_file_forbids_pattern_in_file",
        # 2026-05-10 Class B counter-measure: planner can FORBID a
        # pattern (e.g. "no new module-level boolean settings flag" for
        # an ORM bug). Catches the "invent a SUBQUERY_GROUP_BY_PRESERVE
        # = True bypass" hallucination at the gate.
        "forbids_pattern_in_diff",
        # 2026-05-10 Class E counter-measure: when planner emits a
        # test scope hint, fail the diff if every newly-added test
        # only references symbols that did not exist in the codebase
        # before the patch. Catches "test asserts the new flag works"
        # self-justifying tests.
        "test_must_reference_existing_symbol",
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


@dataclass(frozen=True)
class _DiffHunk:
    file_path: str
    new_start: int
    added_lines: str


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


_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _iter_diff_hunks(diff: str):
    """Yield parsed hunks with file path, new-file start line, and added text."""
    current_path: str | None = None
    current_start = 1
    added: list[str] | None = None
    for raw in diff.splitlines():
        header = _DIFF_HEADER_RE.match(raw)
        if header is not None:
            if current_path is not None and added is not None:
                yield _DiffHunk(current_path, current_start, "\n".join(added))
            current_path = header.group(2)
            added = None
            continue
        if current_path is None:
            continue
        hunk = _HUNK_HEADER_RE.match(raw)
        if hunk is not None:
            if added is not None:
                yield _DiffHunk(current_path, current_start, "\n".join(added))
            current_start = int(hunk.group(3) or "1")
            added = []
            continue
        if added is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            added.append(raw[1:])
    if current_path is not None and added is not None:
        yield _DiffHunk(current_path, current_start, "\n".join(added))


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


def _pattern_matches(pattern: str, text: str) -> bool:
    """Treat acceptance patterns as literal substrings OR regexes.

    The planner often emits regex-shaped patterns (`org\\.osmdroid\\.`),
    while older tests and plans used plain substrings. Literal matching is
    attempted first to preserve the old behavior for simple strings.
    """
    if not pattern or not text:
        return False
    if pattern in text:
        return True
    try:
        if re.search(pattern, text):
            return True
    except re.error:
        pass
    if _io_dispatcher_equivalent(pattern, text):
        return True
    return False


def _io_dispatcher_equivalent(pattern: str, text: str) -> bool:
    """Accept common coroutine IO forms for planner's withContext-only pattern."""
    normalized = (pattern or "").replace("\\", "")
    if "Dispatchers.IO" not in normalized:
        return False
    if "withContext" not in normalized:
        return False
    return bool(
        re.search(r"\b(?:withContext|launch)\s*\(\s*Dispatchers\.IO\b", text or "")
    )


def _diff_touches_file(diff: str, file_path: str) -> bool:
    if not file_path:
        return False
    return any(hunk.file_path == file_path for hunk in _iter_diff_hunks(diff))


def _evaluate_existing_file_context_path_c(
    test: AcceptanceTest,
    diff: str,
    patched_files: dict[str, str] | None,
) -> AcceptanceResult | None:
    """Allow no-change structural evidence when a touched file already has it."""
    if not test.file or not patched_files:
        return None
    if not _diff_touches_file(diff, test.file):
        return None
    patched = patched_files.get(test.file)
    if not patched:
        return None
    if _pattern_matches(test.pattern, patched):
        return AcceptanceResult(
            test,
            True,
            f"found pattern in patched final file context ({test.file})",
        )
    return None


_FUN_DECL_RE = re.compile(
    r"^\s*(?:public|private|internal|protected|inline|suspend|operator|override|open|final|abstract|tailrec|infix|external)*"
    r"\s*fun\s+"
)


def _scope_around_hunk(patched_text: str, new_start: int) -> str:
    """Return same Kotlin function as the hunk, falling back to a window."""
    if not patched_text:
        return ""
    lines = patched_text.splitlines()
    idx = max(0, min(new_start - 1, len(lines) - 1))
    fun_start = -1
    for i in range(idx, -1, -1):
        if _FUN_DECL_RE.match(lines[i]):
            fun_start = i
            break
    if fun_start < 0:
        lo = max(0, idx - 80)
        hi = min(len(lines), idx + 81)
        return "\n".join(lines[lo:hi])
    depth = 0
    saw_open = False
    fun_end = len(lines) - 1
    for j in range(fun_start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                saw_open = True
            elif ch == "}":
                depth -= 1
                if saw_open and depth == 0:
                    fun_end = j
                    break
        if saw_open and depth == 0:
            break
    return "\n".join(lines[fun_start:fun_end + 1])


_PAYLOAD_FIELD_RE = re.compile(
    r'"(?:latitude|longitude|houseNumber|street|area|division|district|thana|city|country|postcode|postCode|notes?)"',
    re.IGNORECASE,
)


def _looks_like_existing_sink_pattern(pattern: str) -> bool:
    return bool(re.search(r"updateChildren|setValue|\.set\s*\(", pattern or ""))


def _evaluate_existing_sink_path_b(
    test: AcceptanceTest,
    diff: str,
    patched_files: dict[str, str] | None,
    *,
    file_filter: str | None = None,
) -> AcceptanceResult | None:
    """Allow payload-change -> unchanged sink evidence.

    Round 10 showed that selected lat/lng can be correctly wired by changing
    the map payload values while the existing `userRef.setValue(userData)`
    line remains a context line. That is still implementation evidence, not
    a failure to add a sink.
    """
    if not patched_files or not _looks_like_existing_sink_pattern(test.pattern):
        return None
    for hunk in _iter_diff_hunks(diff):
        if file_filter and hunk.file_path != file_filter:
            continue
        if not _PAYLOAD_FIELD_RE.search(hunk.added_lines):
            continue
        patched = patched_files.get(hunk.file_path)
        if not patched:
            continue
        scope_text = _scope_around_hunk(patched, hunk.new_start)
        if _pattern_matches(test.pattern, scope_text):
            return AcceptanceResult(
                test,
                True,
                f"found existing sink in patched file context after payload change ({hunk.file_path})",
            )
    return None


# --- per-kind evaluators ------------------------------------------------------


def _evaluate_diff_contains_pattern(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    pattern = test.pattern
    for _path, added in _iter_added_lines(diff):
        if _pattern_matches(pattern, added):
            return AcceptanceResult(test, True, f"found pattern in added lines")
    path_b = _evaluate_existing_sink_path_b(test, diff, patched_files)
    if path_b is not None:
        return path_b
    return AcceptanceResult(test, False, f"pattern {pattern!r} not in any added line")


def _evaluate_diff_contains_pattern_in_file(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    if not test.file:
        return AcceptanceResult(test, False, "no file specified for in-file pattern test")
    pattern = test.pattern
    for path, added in _iter_added_lines(diff):
        if path == test.file and _pattern_matches(pattern, added):
            return AcceptanceResult(test, True, f"found pattern in {test.file} added lines")
    path_b = _evaluate_existing_sink_path_b(
        test, diff, patched_files, file_filter=test.file
    )
    if path_b is not None:
        return path_b
    path_c = _evaluate_existing_file_context_path_c(test, diff, patched_files)
    if path_c is not None:
        return path_c
    return AcceptanceResult(
        test,
        False,
        f"pattern {pattern!r} not in added lines of {test.file}",
    )


def _evaluate_function_signature_unchanged(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
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
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
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


def _evaluate_no_new_file_outside(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
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


def _evaluate_import_added(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    pattern = test.pattern
    for _path, added in _iter_added_lines(diff):
        if _is_import_line(added) and _pattern_matches(pattern, added):
            return AcceptanceResult(test, True, "import added")
    return AcceptanceResult(
        test, False, f"import matching {pattern!r} not added in any file"
    )


def _evaluate_forbids_pattern_in_diff(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    """Reject the diff when the forbidden pattern appears in any added line.

    Counter-measure for Class B (hallucinated bypass). Planner emits this
    when the bug class makes a new top-level constant / settings flag a
    suspicious shape — for ORM/query bugs the fix lives inside an
    executable code path, not in `SOMETHING = True`.

    Pattern is treated as a regex (per ``re.search``) so the planner can
    write ``^[A-Z_]+ = (True|False)$`` to ban arbitrary new module-level
    booleans, or a more specific banned name. ``file`` (optional) scopes
    the check to a single file.
    """
    pattern = test.pattern
    if not pattern:
        return AcceptanceResult(test, False, "no forbidden pattern specified")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return AcceptanceResult(
            test, False, f"forbidden pattern {pattern!r} did not compile: {exc}"
        )
    offenders: list[str] = []
    for path, added in _iter_added_lines(diff):
        if test.file and path != test.file:
            continue
        if compiled.search(added):
            offenders.append(f"{path}: {added.strip()[:80]}")
            if len(offenders) >= 3:
                break
    if offenders:
        return AcceptanceResult(
            test, False,
            f"forbidden pattern {pattern!r} found in added lines: " + " | ".join(offenders),
        )
    return AcceptanceResult(
        test, True, f"forbidden pattern {pattern!r} not present in any added line"
    )


def _evaluate_final_file_forbids_pattern_in_file(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    """Reject when a forbidden pattern remains in a touched final file.

    This complements forbids_pattern_in_diff for deletion-style fixes. A
    patch can satisfy an issue by removing an unsafe call rather than adding a
    new one; checking only added lines cannot prove the unsafe final shape is
    gone.
    """
    if not test.file:
        return AcceptanceResult(
            test, False, "no file specified for final-file forbid test"
        )
    if not test.pattern:
        return AcceptanceResult(test, False, "no forbidden pattern specified")
    if not _diff_touches_file(diff, test.file):
        return AcceptanceResult(test, False, f"{test.file} was not touched")
    patched = (patched_files or {}).get(test.file)
    if patched is None:
        return AcceptanceResult(
            test, False, f"patched final file context missing for {test.file}"
        )
    try:
        compiled = re.compile(test.pattern)
    except re.error as exc:
        return AcceptanceResult(
            test, False, f"forbidden pattern {test.pattern!r} did not compile: {exc}"
        )
    if compiled.search(patched):
        return AcceptanceResult(
            test,
            False,
            f"forbidden pattern {test.pattern!r} remains in final {test.file}",
        )
    return AcceptanceResult(
        test,
        True,
        f"forbidden pattern {test.pattern!r} absent from final {test.file}",
    )


_TEST_SYMBOL_RE = re.compile(r"\b([A-Z_][A-Za-z0-9_]+)\b")


def _evaluate_test_must_reference_existing_symbol(
    test: AcceptanceTest, diff: str, patched_files: dict[str, str] | None = None
) -> AcceptanceResult:
    """Fail when a newly added test file references ONLY symbols that
    appear nowhere else in the patched (added or unchanged) code.

    Counter-measure for Class E ("test-shaped self-justification"):
    a model invents `SUBQUERY_GROUP_BY_PRESERVE = True` and writes a
    test that just asserts the new symbol exists. Such tests pass
    locally but don't reproduce the user's reported bug.

    ``scope`` (glob, optional) restricts which added test files are
    inspected; defaults to ``tests/**``. ``pattern`` (optional regex,
    Python identifier shape by default) controls what counts as a
    symbol reference inside the test file.
    """
    test_glob = (test.scope or "tests/").rstrip("/") + "/"
    # Collect new test files: any new-file-mode path that starts with
    # the test scope.
    new_test_files: set[str] = set()
    for path in _iter_new_files(diff):
        normalized = PurePosixPath(path).as_posix()
        if normalized.startswith(test_glob):
            new_test_files.add(normalized)
    if not new_test_files:
        return AcceptanceResult(
            test, True, "no new test files to check"
        )
    # Build the "symbol pool" from added lines outside the new test
    # files (i.e. the actual fix code) plus removed-line context.
    pool_text_parts: list[str] = []
    for path, added in _iter_added_lines(diff):
        if path in new_test_files:
            continue
        pool_text_parts.append(added)
    for _path, removed in _iter_removed_lines(diff):
        pool_text_parts.append(removed)
    pool_text = "\n".join(pool_text_parts)
    # For each new test file, check that at least one identifier inside
    # it appears in the pool. If the test file references only names
    # that don't exist anywhere in the touched non-test code, the test
    # is self-justifying and should fail the gate.
    offenders: list[str] = []
    for tf in sorted(new_test_files):
        tf_lines = [
            content for path, content in _iter_added_lines(diff) if path == tf
        ]
        identifiers = set()
        for line in tf_lines:
            for m in _TEST_SYMBOL_RE.finditer(line):
                identifiers.add(m.group(1))
        if not identifiers:
            continue  # empty test file edge case → not failing here
        if any(ident in pool_text for ident in identifiers):
            continue
        offenders.append(tf)
    if offenders:
        return AcceptanceResult(
            test, False,
            f"new test file(s) reference only symbols not in fix code: {offenders}",
        )
    return AcceptanceResult(
        test, True,
        f"all new test files reference at least one fix-code symbol",
    )


_EVALUATORS: dict[
    str, Callable[[AcceptanceTest, str, dict[str, str] | None], AcceptanceResult]
] = {
    "diff_contains_pattern": _evaluate_diff_contains_pattern,
    "diff_contains_pattern_in_file": _evaluate_diff_contains_pattern_in_file,
    "function_signature_unchanged": _evaluate_function_signature_unchanged,
    "function_signature_changed": _evaluate_function_signature_changed,
    "no_new_file_outside": _evaluate_no_new_file_outside,
    "import_added": _evaluate_import_added,
    "forbids_pattern_in_diff": _evaluate_forbids_pattern_in_diff,
    "final_file_forbids_pattern_in_file": _evaluate_final_file_forbids_pattern_in_file,
    "test_must_reference_existing_symbol": _evaluate_test_must_reference_existing_symbol,
}


def evaluate_acceptance(
    diff: str,
    tests: list[AcceptanceTest],
    patched_files: dict[str, str] | None = None,
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
        results.append(evaluator(test, diff, patched_files))
    passed = all(result.matched for result in results)
    return AcceptanceReport(passed=passed, results=results)
