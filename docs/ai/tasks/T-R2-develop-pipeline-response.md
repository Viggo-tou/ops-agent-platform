# T-R2 — Meaningful Develop Pipeline Response (Replace Template Text)

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Frontend root: `apps/web/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

When the `jira_issue_develop` pipeline completes, the chat response shown to the user should be a clear, readable summary of what was done — NOT the generic template "Answer the question with grounded evidence from the repository."

Currently the user sees a confusing template message that looks like internal debug output. They have no idea what the pipeline did, what files were changed, or what happened with Jira.

## Background

The current final response for a develop pipeline task comes from `latest_result_json` which contains the mock planner's generic text. For `jira_issue_develop` tasks, the orchestrator should construct a human-readable summary from the pipeline results.

## Design

### 1. Build a develop pipeline summary in the orchestrator

In `app/orchestrator/service.py`, when the develop pipeline completes successfully, construct a structured response instead of using the generic plan text.

After all pipeline steps complete (codegen, apply_patch, test, review, jira), build the final result like:

```python
develop_result = {
    "status": "completed",
    "message": self._build_develop_summary(pipeline_state),
    "result": {
        "scenario": "jira_issue_develop",
        "issue_key": issue_key,
        "summary": plan.change_summary,
        "files_changed": codegen_result.get("files_changed", []),
        "diff": codegen_result.get("diff", ""),
        "patch_method": apply_result.get("method", ""),
        "test_skipped": pipeline_state.get("test_skipped", False),
        "review_verdict": review_result.get("verdict", ""),
        "jira_transitioned": True,
    }
}
```

### 2. Human-readable summary builder

```python
def _build_develop_summary(self, pipeline_state: dict) -> str:
    """Build a human-readable summary of the develop pipeline execution."""
    parts = []
    
    issue_key = pipeline_state.get("issue_key", "unknown")
    parts.append(f"## {issue_key} Development Complete\n")
    
    # What changed
    files = pipeline_state.get("files_changed", [])
    if files:
        parts.append(f"**Modified {len(files)} file(s):**")
        for f in files[:10]:
            parts.append(f"- `{f}`")
        parts.append("")
    
    # The diff
    diff = pipeline_state.get("diff", "")
    if diff:
        parts.append("**Changes:**")
        parts.append(f"```diff\n{diff}\n```\n")
    
    # Pipeline steps
    parts.append("**Pipeline:**")
    parts.append(f"- Code generation: {pipeline_state.get('codegen_provider', 'unknown')}")
    method = pipeline_state.get("patch_method", "")
    if method:
        parts.append(f"- Patch applied via: {method}")
    if pipeline_state.get("test_skipped"):
        parts.append("- Tests: skipped (no test config)")
    parts.append(f"- Review: {pipeline_state.get('review_verdict', 'N/A')}")
    parts.append(f"- Jira: commented and transitioned")
    
    return "\n".join(parts)
```

### 3. Store pipeline state incrementally

The orchestrator's `_execute_develop_pipeline()` should store intermediate results in `pipeline_state` as each step completes:

After codegen:
```python
pipeline_state["diff"] = codegen_result.diff
pipeline_state["files_changed"] = codegen_result.files_changed
pipeline_state["codegen_provider"] = codegen_result.provider_name
```

After apply_patch:
```python
pipeline_state["patch_method"] = apply_result.get("method", "git_apply")
```

After test skip:
```python
pipeline_state["test_skipped"] = True
```

After review:
```python
pipeline_state["review_verdict"] = review_result.get("verdict", "")
```

### 4. Use this summary as the task's final result

Replace the current generic `latest_result_json` with `develop_result` when setting the task to completed.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — add `_build_develop_summary()`, store pipeline state incrementally, use summary as final result for develop pipeline.

## Tests

1. **`test_develop_summary_includes_diff`** — Mock a successful develop pipeline. Assert the final result message contains the diff in a code fence, file names, and pipeline steps.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New test passes.
- Full suite still green.
- After a successful develop pipeline, the chat shows: issue key, files changed, diff in code fence, pipeline steps summary, Jira status.
- The generic "Answer the question with grounded evidence" text is NOT shown for develop tasks.

## Workflow (for the executor)

1. Read `app/orchestrator/service.py` — focus on `_execute_develop_pipeline()` and how `latest_result_json` is set on task completion. Search for where the task status is set to completed at the end of the develop pipeline.
2. Add `_build_develop_summary()` method.
3. Store intermediate results in pipeline_state during execution.
4. Use the summary when completing the task.
5. Add test.
6. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-R2-develop-pipeline-response.md
```
