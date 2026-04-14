# T-Q4 — Resilient Patch Application with Fallback

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

Make `sandbox.apply_patch()` more resilient to imperfect diffs produced by LLMs. Currently it calls `git apply` strictly — any formatting issue (corrupt header, wrong line numbers, missing trailing newline) causes total failure. Add fallback strategies so slightly malformed diffs still get applied.

## Background

MiniMax (and other LLMs) generate unified diffs that sometimes have minor formatting issues:
- Missing trailing newline
- Off-by-one line numbers in hunk headers
- Extra whitespace in context lines
- Missing `\ No newline at end of file` marker

The result: `git apply` fails with "corrupt patch at line N" even though the actual code changes are correct. This blocks the entire pipeline.

## Design

### 1. Diff sanitization before apply

Add a `_sanitize_diff(raw_diff: str) -> str` function in `sandbox.py` that cleans common LLM diff artifacts:

```python
def _sanitize_diff(raw_diff: str) -> str:
    """Clean common LLM diff formatting issues."""
    lines = raw_diff.split("\n")
    cleaned = []
    for line in lines:
        # Strip trailing whitespace (common LLM artifact)
        line = line.rstrip()
        # Fix: some LLMs output "diff --git" without proper spacing
        # Fix: ensure diff ends with newline
        cleaned.append(line)
    # Ensure trailing newline
    result = "\n".join(cleaned)
    if not result.endswith("\n"):
        result += "\n"
    return result
```

### 2. Multi-strategy apply in `apply_patch()`

Change `apply_patch()` to try multiple strategies in order:

```python
def apply_patch(self, diff: str, ...) -> dict:
    sanitized = _sanitize_diff(diff)
    
    # Strategy 1: git apply (strict)
    result = self._try_git_apply(sanitized, extra_args=[])
    if result["success"]:
        return result
    
    # Strategy 2: git apply --3way (uses merge for conflicts)
    result = self._try_git_apply(sanitized, extra_args=["--3way"])
    if result["success"]:
        result["method"] = "git_apply_3way"
        return result
    
    # Strategy 3: git apply with relaxed whitespace
    result = self._try_git_apply(sanitized, extra_args=["--ignore-whitespace", "--whitespace=nowarn"])
    if result["success"]:
        result["method"] = "git_apply_relaxed"
        return result
    
    # Strategy 4: patch -p1 (more lenient than git apply)
    result = self._try_patch_command(sanitized)
    if result["success"]:
        result["method"] = "patch_p1"
        return result
    
    # All strategies failed
    raise SandboxError(f"All patch strategies failed. Last error: {result['error']}")
```

### 3. Helper methods

```python
def _try_git_apply(self, diff: str, extra_args: list[str]) -> dict:
    """Try git apply with given args. Return {"success": bool, "error": str}."""
    patch_file = self.work_dir / ".tmp_patch.diff"
    patch_file.write_text(diff, encoding="utf-8")
    try:
        cmd = f"git apply {' '.join(extra_args)} .tmp_patch.diff"
        result = self.run(cmd, timeout_seconds=30)
        if result["exit_code"] == 0:
            return {"success": True, "method": "git_apply", "error": ""}
        return {"success": False, "error": result.get("stderr", "")}
    finally:
        patch_file.unlink(missing_ok=True)

def _try_patch_command(self, diff: str) -> dict:
    """Try POSIX patch command as last resort."""
    patch_file = self.work_dir / ".tmp_patch.diff"
    patch_file.write_text(diff, encoding="utf-8")
    try:
        result = self.run("patch -p1 < .tmp_patch.diff", timeout_seconds=30)
        if result["exit_code"] == 0:
            return {"success": True, "method": "patch_p1", "error": ""}
        return {"success": False, "error": result.get("stderr", "")}
    finally:
        patch_file.unlink(missing_ok=True)
```

### 4. Logging

Each strategy attempt should log which method was tried and whether it succeeded, so we can see in events which fallback was used.

The returned result dict should include `"method"` field indicating which strategy worked.

## Files to edit

1. `apps/backend/app/services/sandbox.py` — add `_sanitize_diff()`, modify `apply_patch()` with multi-strategy fallback, add helper methods.

## Tests

Add to existing sandbox tests.

1. **`test_apply_patch_with_trailing_whitespace`** — Create a diff with trailing spaces on lines. Assert it gets applied successfully after sanitization.
2. **`test_apply_patch_missing_trailing_newline`** — Create a diff without final newline. Assert sanitizer adds it and apply succeeds.
3. **`test_apply_patch_fallback_to_relaxed`** — Mock strict `git apply` to fail but `--ignore-whitespace` to succeed. Assert the relaxed strategy is used and result includes `method: "git_apply_relaxed"`.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass.
- Full suite still green.
- `apply_patch()` tries up to 4 strategies before failing.
- Result dict includes `"method"` indicating which strategy worked.
- Trailing whitespace and missing newlines are cleaned before any attempt.

## Workflow (for the executor)

<!-- Effort: medium — modify existing sandbox method with fallback chain -->

1. Read `app/services/sandbox.py` — focus on `apply_patch()` and `run()` methods.
2. Add `_sanitize_diff()` function.
3. Refactor `apply_patch()` to use multi-strategy approach.
4. Add tests.
5. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q4-resilient-patch-apply.md
```
