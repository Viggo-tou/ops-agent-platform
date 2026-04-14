# T-Q10 — Fix _sanitize_diff() Blank Context Line Corruption

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Fix the same blank-context-line bug in `_sanitize_diff()` that was just fixed in `repair_diff()` (T-Q9). The `_sanitize_diff()` function in `sandbox.py` also calls `line.rstrip()` which strips the leading space from blank context lines in unified diffs, corrupting the patch.

## Root cause

File: `apps/backend/app/services/sandbox.py`, function `_sanitize_diff()`, line 22:

```python
cleaned = [line.rstrip() for line in lines]
```

This strips the trailing space from blank context lines. In unified diff, a blank line in the source file is represented as a context line containing exactly one space `" "`. After `rstrip()`, it becomes `""`, which `git apply` treats as a malformed patch line.

## Fix

Change `rstrip()` to `rstrip("\r")` — only strip carriage returns (for CRLF normalization), not spaces:

```python
def _sanitize_diff(raw_diff: str) -> str:
    """Clean common LLM diff formatting issues before applying a patch."""
    lines = raw_diff.split("\n")
    cleaned = [line.rstrip("\r") for line in lines]
    result = "\n".join(cleaned)
    if not result.endswith("\n"):
        result += "\n"
    return result
```

## Files to edit

1. `apps/backend/app/services/sandbox.py` — change `line.rstrip()` to `line.rstrip("\r")` in `_sanitize_diff()`.

## Tests

Add to `apps/backend/tests/services/test_sandbox.py` (or create if needed):

1. **`test_sanitize_diff_preserves_blank_context_lines`** — Provide a diff string containing a blank context line (`" "` — single space). Assert `_sanitize_diff()` preserves the space and doesn't strip it.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New test passes.
- Full suite still green.
- `_sanitize_diff(" \n")` returns `" \n"` (space preserved, only CR stripped).

## Workflow (for the executor)

<!-- Effort: low — single line change -->

1. Read `app/services/sandbox.py` — focus on `_sanitize_diff()`.
2. Change `rstrip()` to `rstrip("\r")`.
3. Add test.
4. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q10-fix-sanitize-diff-rstrip.md
```
