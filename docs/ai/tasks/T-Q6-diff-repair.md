# T-Q6 — Unified Diff Auto-Repair Before Apply

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add a `repair_diff()` function that fixes common structural errors in LLM-generated unified diffs before `git apply` runs. This sits between codegen output and `sandbox.apply_patch()`, fixing hunk headers, line counts, and other format issues that cause "malformed patch" or "corrupt patch at line N" errors.

## Background

MiniMax codegen now reliably produces diffs that look approximately correct (right files, right changes), but `git apply` and `patch -p1` both reject them due to hunk header errors — wrong line counts, missing blank separators between file diffs, etc. The actual code changes inside the hunks are correct; only the metadata is broken.

Common LLM diff errors observed:
1. Hunk line counts wrong: `@@ -10,3 +10,4 @@` but the hunk actually has 5 old lines and 6 new lines.
2. Missing newline between file sections (two `diff --git` lines with no blank line separator).
3. Trailing content after the last hunk (LLM adds explanation text).
4. Hunk starting line number off by a few lines.
5. Context lines that don't match the source file (LLM hallucinated context).

## Design

### 1. New module: `app/services/diff_repair.py`

```python
class DiffRepairResult:
    repaired_diff: str
    repairs_applied: list[str]  # descriptions of what was fixed
    file_count: int

def repair_diff(raw_diff: str, context_files: dict[str, str] | None = None) -> DiffRepairResult:
    """Parse and repair a unified diff.
    
    Args:
        raw_diff: The LLM-generated unified diff string.
        context_files: Optional dict of filepath -> file content. If provided,
            used to fix context line mismatches and recompute hunk start lines.
    """
```

### 2. Repair steps (in order)

**Step A — Split into per-file sections.**
Split at `diff --git` boundaries. Each section = one file's changes.

**Step B — Reparse and fix each file section.**
For each file section:
1. Extract `--- a/path` and `+++ b/path` lines.
2. Split into hunks at `@@ ... @@` lines.
3. For each hunk:
   - Count actual `-` lines (removals), `+` lines (additions), and ` ` lines (context).
   - Recompute the `@@ -old_start,old_count +new_start,new_count @@` header from actual line counts.
   - `old_count` = context_lines + removal_lines
   - `new_count` = context_lines + addition_lines
   - If `context_files` is provided and the file exists, validate that context lines match the source. If not, try to find the correct offset by searching for the context in the source file and fix `old_start`.

**Step C — Reassemble.**
Join all repaired file sections with proper newline separators. Ensure trailing newline.

**Step D — Strip post-diff text.**
If there's any text after the last hunk that isn't a diff line (`+`, `-`, ` `, `@`, `diff`, `---`, `+++`), remove it.

### 3. Integration in sandbox.py

In `apply_patch()`, call `repair_diff()` BEFORE the sanitize step:

```python
def apply_patch(self, diff, context_files=None, ...):
    from app.services.diff_repair import repair_diff
    repair_result = repair_diff(diff, context_files=context_files)
    sanitized = _sanitize_diff(repair_result.repaired_diff)
    # ... then try the 4 strategies as before
```

### 4. Integration in orchestrator

Pass `context_files` through to `apply_patch()` so the repair function can validate context lines against source files. In `_execute_develop_pipeline()`, the context_files dict is already available in `pipeline_state["context_file_paths"]` — but the actual content was passed to codegen. Store it in pipeline_state so apply_patch can use it.

Update the sandbox.apply_patch tool payload to accept an optional `context_files` dict, and pass it from the orchestrator.

## Files to create

1. `apps/backend/app/services/diff_repair.py`

## Files to edit

2. `apps/backend/app/services/sandbox.py` — call `repair_diff()` in `apply_patch()`, accept optional `context_files` param.
3. `apps/backend/app/orchestrator/service.py` — pass `context_files` to `sandbox.apply_patch` payload.
4. `apps/backend/app/tools/gateway.py` — pass `context_files` from payload to `sandbox.apply_patch()` if present.

## Tests

All in `apps/backend/tests/services/test_diff_repair.py`. Use `unittest.TestCase`.

1. **`test_repair_wrong_hunk_counts`** — Provide a diff with `@@ -1,2 +1,2 @@` but 3 context + 1 removal + 1 addition. Assert the repaired header is `@@ -1,4 +1,4 @@`.
2. **`test_repair_multiple_files`** — Provide a diff with two file sections. Assert both are repaired and properly separated.
3. **`test_repair_trailing_text_stripped`** — Append "Here is the explanation..." after the last hunk. Assert it's removed.
4. **`test_repair_missing_separator`** — Two `diff --git` lines with no blank line between them. Assert a blank line is added.
5. **`test_repair_with_context_files_fixes_offset`** — Provide a hunk where context lines match the source at a different offset. Assert `old_start` is corrected.
6. **`test_repair_clean_diff_unchanged`** — Provide a perfectly valid diff. Assert it comes back unchanged (no unnecessary mutations).

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 6 new tests pass.
- Full suite still green.
- A diff with wrong hunk counts gets auto-repaired to correct counts.
- Trailing LLM commentary after hunks is stripped.
- `repair_result.repairs_applied` lists what was fixed.
- Clean diffs pass through without modification.

## Workflow (for the executor)

<!-- Effort: medium — new service with diff parsing logic -->

1. Read `app/services/sandbox.py` (focus on `apply_patch`), `app/orchestrator/service.py` (focus on `_execute_develop_pipeline` sandbox.apply_patch call), `app/tools/gateway.py` (focus on sandbox executor).
2. Create `app/services/diff_repair.py`.
3. Edit `sandbox.py` to call repair before apply.
4. Edit orchestrator and gateway to pass context_files.
5. Create tests.
6. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q6-diff-repair.md
```
