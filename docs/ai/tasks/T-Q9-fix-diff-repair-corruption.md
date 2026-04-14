# T-Q9 — Fix repair_diff() Blank Context Line Corruption

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

Fix a critical bug in `repair_diff()` where blank context lines in unified diffs cause the repair function to destroy valid hunks. The bug causes the entire MiniMax codegen pipeline to fail because valid diffs get corrupted before `git apply` runs.

## Background

In unified diff format, a blank line in the original source is represented as a context line containing exactly one space character: `" "`. The function `_split_file_sections()` in `diff_repair.py` calls `line.rstrip()` on every line, which strips this space character, turning `" "` into `""`. Then `_is_hunk_body_line("")` returns `False` because the empty string doesn't start with ` `, `+`, `-`, or `\`. The parser treats this as "trailing non-diff text" and deletes everything after the blank line — destroying the rest of the hunk and potentially entire subsequent hunks.

This was observed in the P69-10 pipeline: a valid 27-line diff from difflib was reduced to 14 lines by repair_diff(), causing "malformed patch at line 13: }" errors.

## Root cause

File: `apps/backend/app/services/diff_repair.py`

Line 67 and 70 in `_split_file_sections()`:
```python
current = [line.rstrip()]   # line 67 — strips trailing space from " "
current.append(line.rstrip())  # line 70 — same issue
```

This turns blank context lines `" "` into `""`, which then fails `_is_hunk_body_line()`.

## Fix

### 1. Change `rstrip()` to `rstrip('\r')` in `_split_file_sections()`

Only strip carriage returns (for CRLF normalization), NOT spaces:

```python
def _split_file_sections(raw_diff: str) -> list[_FileSection]:
    sections: list[_FileSection] = []
    current: list[str] | None = None

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                sections.append(_FileSection(lines=current, path=_extract_path(current)))
            current = [line.rstrip("\r")]  # Only strip CR, not spaces
            continue
        if current is not None:
            current.append(line.rstrip("\r"))  # Only strip CR, not spaces

    if current:
        sections.append(_FileSection(lines=current, path=_extract_path(current)))
    return sections
```

### 2. Also handle truly empty lines as context lines in `_is_hunk_body_line()`

As a defense-in-depth measure, treat empty strings as valid hunk body lines (they represent blank context lines where the space was already stripped):

```python
def _is_hunk_body_line(line: str) -> bool:
    return line == "" or line.startswith((" ", "+", "-", "\\"))
```

Both changes are needed: fix 1 prevents the data corruption, fix 2 prevents future regressions if blank lines enter through other paths.

## Files to edit

1. `apps/backend/app/services/diff_repair.py` — two changes as described above.

## Tests

Add to `apps/backend/tests/services/test_diff_repair.py`:

1. **`test_repair_preserves_blank_context_lines`** — Provide a valid diff with a blank context line (space-only line between hunks or within a hunk). Assert repair_diff returns the diff unchanged (no repairs applied, full hunk content preserved).

2. **`test_repair_multi_hunk_with_blank_lines`** — Provide a diff with two hunks where the second hunk contains a blank context line. Assert BOTH hunks are preserved in full. This is the exact scenario that was failing in production.

Example test diff for test 2:
```python
DIFF_WITH_BLANK_CONTEXT = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -7,7 +7,6 @@
 import os
 import sys
 import json
-import unused_module
 import re
 import logging
 import pathlib
@@ -20,6 +19,7 @@
 }
 
 def main():
+    setup()
     x = 1
     y = 2
     return x + y
"""
```
The blank line ` ` between `}` and `def main():` in the second hunk is the critical test case — after repair, it MUST still be present and the full second hunk must be preserved.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass.
- Full suite still green.
- `repair_diff()` on a valid diff with blank context lines returns the diff unchanged.
- `repair_diff()` on a valid multi-hunk diff preserves ALL hunks.
- The `repairs_applied` list is empty for a clean, valid diff with blank context lines.

## Workflow (for the executor)

<!-- Effort: low — two small fixes in existing file -->

1. Read `app/services/diff_repair.py` — focus on `_split_file_sections()` and `_is_hunk_body_line()`.
2. Change `rstrip()` to `rstrip("\r")` in `_split_file_sections()`.
3. Update `_is_hunk_body_line()` to accept empty strings.
4. Add tests.
5. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q9-fix-diff-repair-corruption.md
```
