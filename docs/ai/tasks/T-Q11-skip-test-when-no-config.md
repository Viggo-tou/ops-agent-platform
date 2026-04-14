# T-Q11 — Skip Test Pipeline When No Config Exists

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

When the test pipeline config file (e.g., `tests.yaml`) does not exist in the sandbox, the develop pipeline should skip the test step gracefully instead of failing the entire task. Not all projects have a `tests.yaml` — the pipeline should still succeed.

## Background

After T-Q9/T-Q10 fixed the diff repair corruption, the P69-10 pipeline now successfully:
1. Translates the request (MiniMax)
2. Plans the work (mock fallback)
3. Generates code (MiniMax JSON mode + difflib)
4. Applies the patch (git apply with relaxed whitespace)

But then fails at step 5 (test_pipeline.run) because the HandymanApp Android project has no `tests.yaml` config file. The error "Test pipeline config not found: tests.yaml" causes the entire task to fail even though the patch was applied correctly.

## Fix

In the orchestrator's `_execute_develop_pipeline()`, wrap the test_pipeline.run call so that a missing config is NOT a fatal error:

In `app/orchestrator/service.py`, around line 1278-1291, change the test pipeline call:

```python
# Current code:
try:
    self._execute_tool_step(
        task=task,
        actor_name=actor_name,
        tool_name="test_pipeline.run",
        ...
    )
except Exception as exc:
    self._fail_develop_pipeline(task=task, message=f"测试未通过：{exc}", ...)

# New code:
try:
    self._execute_tool_step(
        task=task,
        actor_name=actor_name,
        tool_name="test_pipeline.run",
        ...
    )
except Exception as exc:
    error_msg = str(exc)
    if "config not found" in error_msg.lower() or "not found" in error_msg.lower() and "config" in error_msg.lower():
        # No test config in this project — skip tests, don't fail
        self._emit_event(
            task=task,
            event_type="TOOL_SKIPPED",
            message=f"Test pipeline skipped: {error_msg}",
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
        )
    else:
        self._fail_develop_pipeline(task=task, message=f"测试未通过：{exc}", ...)
```

The key change: if the test pipeline fails because the config file is not found, emit a TOOL_SKIPPED event and continue the pipeline instead of failing.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — modify the test_pipeline.run exception handler in `_execute_develop_pipeline()` to skip on missing config.

## Tests

Add to existing orchestrator tests:

1. **`test_develop_pipeline_skips_test_when_no_config`** — Mock the test_pipeline tool to raise with "config not found" message. Assert the pipeline does NOT fail, emits a TOOL_SKIPPED event, and continues to completion.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New test passes.
- Full suite still green.
- Missing `tests.yaml` causes a TOOL_SKIPPED event, not pipeline failure.
- Actual test failures (non-config errors) still fail the pipeline.

## Workflow (for the executor)

<!-- Effort: low — modify exception handler in orchestrator -->

1. Read `app/orchestrator/service.py` — focus on `_execute_develop_pipeline()` around the `test_pipeline.run` call (search for "test_pipeline.run" to find the exact location).
2. Modify the exception handler to skip on "config not found" errors.
3. Add test.
4. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q11-skip-test-when-no-config.md
```
