# T-042: General-Purpose Gate Repair Loop

## Objective

Add a **targeted repair cycle** to the develop pipeline so that when any
validation gate (runtime_validation, compile_gate, etc.) rejects a diff, the
orchestrator automatically calls codegen again with a **repair prompt** that
fixes ONLY the rejected issues — no full re-generation.

## Context

Current behavior: when `runtime_validation` fails with blocking findings the
pipeline immediately calls `_fail_develop_pipeline` and returns FAILED.  The
`build_repair_prompt()` function already exists in `runtime_validation.py` but
is never called.

The conformance gate already has a retry mechanism (`_reset_for_conformance_retry`)
but it does a **full re-generation** (clears all downstream state, deletes
sandbox, re-runs entire codegen).  That is wasteful for targeted fixes.

## Design

### 1. New method: `_run_targeted_repair`

Add to `PrimaryOrchestrator`:

```python
def _run_targeted_repair(
    self,
    *,
    task: Task,
    actor_name: str,
    plan: GeneratedPlan,
    pipeline_state: dict,
    repair_prompt: str,
    failing_files: list[str],
    approval_id: str | None,
    gate_name: str,
) -> str | None:
    """Run targeted repair codegen for specific files only.

    1. Extract ORIGINAL content for failing_files from context_files
    2. Call codegen.generate_patch with repair_prompt as task_description
       and only the failing files' context
    3. In the main diff, strip hunks for failing_files (reuse
       _strip_duplicate_diff_hunks)
    4. Concatenate remaining diff + repair diff
    5. Re-clone sandbox, re-apply the merged diff
    6. Return the updated merged diff, or None if repair failed.
    """
```

Key points:
- Codex only sees the files that need fixing (from `pipeline_state["context_files"]`)
- Codex gets the repair prompt describing what's wrong and how to fix
- The existing diff for non-failing files is preserved untouched
- The repaired hunks replace the old hunks for the failing files

### 2. Wire up runtime_validation

In the runtime_validation section (~line 2355-2427 of service.py):

```python
_rv_max_passes = 2  # was 1

for rv_pass in range(_rv_max_passes):
    # ... existing validation logic ...

    if rv_report.passed:
        break

    # --- Repair cycle (first failure only) ---
    if rv_pass == 0:
        from app.services.runtime_validation import build_repair_prompt
        repair_prompt = build_repair_prompt(rv_report.findings)
        if repair_prompt:
            failing_files = sorted({f.file for f in rv_report.findings
                                     if f.severity == "block"})
            record_event(...)  # Log repair attempt

            repaired_diff = self._run_targeted_repair(
                task=task, actor_name=actor_name, plan=plan,
                pipeline_state=pipeline_state,
                repair_prompt=repair_prompt,
                failing_files=failing_files,
                approval_id=approval_id,
                gate_name="runtime_validation",
            )
            if repaired_diff is not None:
                diff = repaired_diff
                pipeline_state["diff"] = diff
                # Reset validation-specific state for re-check
                pipeline_state.pop("runtime_validation", None)
                continue  # Re-validate with repaired diff

    # Final failure (second pass or no repair prompt)
    self._fail_develop_pipeline(...)
    return
```

### 3. `_run_targeted_repair` implementation details

```
Step 1: Extract original file content
    context = pipeline_state["context_files"]
    repair_context = {f: context[f] for f in failing_files if f in context}

Step 2: Build codegen payload
    task_description = repair_prompt  (from build_repair_prompt)
    Append: "EXISTING DIFF FOR CONTEXT:\n" + current diff
    Append: "Fix ONLY the files listed above. Output unified diff hunks."

Step 3: Call codegen
    repair_result = self._execute_develop_tool(
        tool_name="codegen.generate_patch",
        payload={
            "plan_json": pipeline_state.get("plan_json") or plan.model_dump(),
            "context_files": repair_context,
            "task_description": repair_prompt_with_context,
        },
        stage=WorkflowStage.REVIEW,
        role=RoleName.REVIEWER,
    )

Step 4: Merge diffs
    main_diff = pipeline_state["diff"]
    # Remove old hunks for failing files
    cleaned_diff = _strip_duplicate_diff_hunks(main_diff, set(failing_files))
    # Append repair hunks
    repair_diff = repair_result["diff"]
    merged_diff = cleaned_diff + "\n" + repair_diff

Step 5: Re-apply to sandbox
    - Delete sandbox
    - Re-clone
    - Apply merged diff

Step 6: Return merged_diff
```

### 4. Sandbox re-application

After merging the repair diff, the sandbox needs to be refreshed:
- Use the existing `sandbox.clone` + `sandbox.apply_patch` tools
- OR directly call the sandbox service to re-clone and re-apply

The simplest approach: reset sandbox-related pipeline state and let the
existing sandbox logic re-run. But since we're in the review stage (past
the sandbox stage), we need to handle this inside `_run_targeted_repair`:

```python
sandbox_dir = self._develop_sandbox_dir(task)
if sandbox_dir.exists():
    shutil.rmtree(sandbox_dir, ignore_errors=True)
# Re-clone + apply merged diff
sandbox_result = self._execute_develop_tool(
    tool_name="sandbox.clone", ...)
self._execute_develop_tool(
    tool_name="sandbox.apply_patch",
    payload={"diff": merged_diff, ...}, ...)
```

### 5. Config

Add to Settings:
```python
gate_repair_max_attempts: int = 1  # Max repair attempts per gate
gate_repair_timeout_seconds: float = 300.0  # Timeout for repair codegen
```

### 6. Generality

The `_run_targeted_repair` method is gate-agnostic. Any gate can use it by:
1. Having a `build_repair_prompt(findings) -> str` function in its service module
2. Calling `_run_targeted_repair` with the repair prompt and failing files

Future gates that want repair capability just need to add their own
`build_repair_prompt` and wire it into the orchestrator loop.

## Files to modify

1. **`apps/backend/app/orchestrator/service.py`**
   - Add `_run_targeted_repair` method
   - Modify runtime_validation section (lines 2355-2427): `_rv_max_passes = 2`,
     add repair cycle on first failure
   - Add `gate_repair_max_attempts` to class-level constants
   - Add config reads for repair settings

2. **`apps/backend/app/core/config.py`**
   - Add `gate_repair_max_attempts: int = 1`
   - Add `gate_repair_timeout_seconds: float = 300.0`

3. **`apps/backend/tests/orchestrator/test_gate_repair.py`** (NEW)
   - Test: runtime_validation failure triggers repair codegen
   - Test: repair codegen succeeds → re-validation passes → pipeline continues
   - Test: repair codegen fails → pipeline fails gracefully
   - Test: repair diff is correctly merged (old hunks replaced, other hunks preserved)
   - Test: max repair attempts respected (no infinite loop)
   - Test: events are recorded for repair attempt

## Constraints

- Repair is TARGETED: only re-generate diff for failing files, not all files
- Repair codegen sees only the original file content + repair instructions
- Max 1 repair attempt per gate per pipeline run (configurable)
- Existing diff for non-failing files is preserved untouched
- All repair attempts are logged as events for audit trail
- Do NOT change validation rules — they stay strict
