# T-N1 — End-to-End Pipeline Orchestration (`jira_issue_develop`)

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: xhigh -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add a new scenario `jira_issue_develop` that runs the full automated pipeline: Jira fetch → plan → codegen → sandbox apply → test → diff review → approval → Jira writeback. Wire it into the orchestrator so users can say "把 OPS-123 做了" and the system executes end-to-end.

## Background

Phase N of the multi-agent MVP roadmap. All the individual pipeline stages exist:
- Jira read (Phase B): `jira.get_issue`
- Plan generation: `PrimaryAgentPlanner.generate_plan()`
- Code generation (Phase M): `CodeGenerator.generate_patch()`
- Sandbox (Phase C): `ExecutionSandbox.clone()`, `.apply_patch()`
- Test pipeline (Phase D): `TestPipeline.run()`
- Diff review (Phase E): `DiffReviewer.review()`
- Approval gate (Phase F): `ToolApprovalRequired`
- Jira writeback (Phase B): `jira.transition_issue`, `jira.add_comment`
- Rollback (Phase G): `RollbackExecutor`
- Cost tracking (Phase K): `CostTracker.record_usage()`

This phase **integrates** them into a single orchestrator flow. No new services — just orchestrator wiring.

## Design

### 1. New scenario classification

In `classify_request()` in `app/orchestrator/service.py`, add detection for `jira_issue_develop`:

```python
# Before jira_issue_plan detection:
if jira_reference and any(
    keyword in lowered
    for keyword in ("做了", "implement", "develop", "fix it", "修复", "开发", "build", "code")
):
    return "jira_issue_develop"
```

This takes priority over `jira_issue_plan` (which is just read + plan, no execution).

### 2. Pipeline executor method

New method in `PrimaryOrchestrator`:

```python
def _execute_develop_pipeline(
    self,
    *,
    task: Task,
    actor_name: str,
    plan: GeneratedPlan,
    approval_id: str | None = None,
) -> None:
    """Full pipeline: codegen → sandbox → test → review → approve → writeback."""
```

The method runs these steps in sequence:

**Step 1 — Gather context files.**
Read the affected files from the plan's `affected_code_locations`. For now, use a helper `_gather_codegen_context(plan, sandbox)` that:
- If a sandbox exists (cloned repo), reads files directly from the sandbox dir
- If no sandbox, uses the knowledge service to search for the files
- Returns `dict[str, str]` (filepath → content)

If no context files found, fail with a clear message.

**Step 2 — Generate code.**
Call `codegen.generate_patch` via the tool gateway:
```python
result = self.tool_gateway.execute(
    task_id=task.id,
    tool_name="codegen.generate_patch",
    payload={"plan_json": task.plan_json, "context_files": context_files, "task_description": task.request_text},
    ...
)
```
Record event: `"代码生成完成，修改了 N 个文件"`

**Step 3 — Setup sandbox & apply patch.**
If no sandbox exists yet, create one (clone the repo URL from plan context or a configured default).
Apply the generated diff:
```python
result = self.tool_gateway.execute(tool_name="sandbox.apply_patch", payload={"task_id": task.id, "patch": codegen_result["diff"]}, ...)
```

**Step 4 — Run tests.**
```python
result = self.tool_gateway.execute(tool_name="test_pipeline.run", payload={"task_id": task.id}, ...)
```
If tests fail → record event, set task failed, return.

**Step 5 — Diff review.**
```python
result = self.tool_gateway.execute(tool_name="diff_reviewer.review", payload={"diff": codegen_result["diff"], "test_result": test_result}, ...)
```
If reviewer blocks → record event, set task failed with reasons, return.

**Step 6 — Approval gate.**
The `codegen.generate_patch` and `sandbox.apply_patch` are `APPROVAL_REQUIRED`, so the approval gate (Phase F) will fire naturally during Step 2 or 3. The pipeline pauses and resumes via `resume_after_approval()`.

**Step 7 — Jira writeback (if applicable).**
If the task has a Jira issue key, transition status and add a comment summarizing changes:
```python
self.tool_gateway.execute(tool_name="jira.add_comment", payload={...})
self.tool_gateway.execute(tool_name="jira.transition_issue", payload={...})
```

**Step 8 — Complete.**
Set task status to COMPLETED, record final event.

### 3. Route in _execute_plan_impl

Add a branch at the top of `_execute_plan_impl()`:

```python
if task.scenario == "jira_issue_develop":
    return self._execute_develop_pipeline(
        task=task, actor_name=actor_name, plan=plan, approval_id=approval_id,
    )
```

### 4. Context gathering helper

```python
def _gather_codegen_context(self, *, task: Task, plan: GeneratedPlan) -> dict[str, str]:
    """Read affected files from sandbox or knowledge index."""
    context_files: dict[str, str] = {}
    for location in plan.affected_code_locations:
        path = location.relative_path
        # Try sandbox first
        sandbox_dir = Path(f"data/sandboxes/{task.id}")
        full_path = sandbox_dir / path
        if full_path.exists() and full_path.is_file():
            context_files[path] = full_path.read_text(encoding="utf-8", errors="replace")
            continue
        # Fallback: search knowledge
        try:
            results = self.knowledge_service.search(query=path, top_k=1)
            if results:
                context_files[path] = results[0].get("content", "")
        except Exception:
            pass
    return context_files
```

### 5. Error handling

Each step checks for failure and records appropriate events:
- CodegenError → `EXECUTION_FAILED`, message: "代码生成失败：{reason}"
- Test failure → `EXECUTION_FAILED`, message: "测试未通过：{failed_count} 个失败"
- Reviewer blocks → `REVIEW_FAILED`, message: "代码审查未通过：{violations}"
- ToolApprovalRequired → pause (existing mechanism)
- Any other exception → `EXECUTION_FAILED` with error detail

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — add `classify_request` branch, `_execute_develop_pipeline`, `_gather_codegen_context`, route in `_execute_plan_impl`.

## Files to create

2. `apps/backend/tests/orchestrator/test_develop_pipeline.py`

## Tests

All in `apps/backend/tests/orchestrator/test_develop_pipeline.py`. Use `unittest.TestCase`. Mock the tool gateway, knowledge service, and sandbox.

1. **`test_classify_request_develop`** — "把 OPS-123 做了" → `jira_issue_develop`. Also test "implement OPS-123", "fix OPS-123".
2. **`test_classify_request_plan_not_develop`** — "plan OPS-123" → still `jira_issue_plan` (not develop).
3. **`test_gather_codegen_context_from_sandbox`** — Create a temp sandbox dir with a file. Assert `_gather_codegen_context` reads it.
4. **`test_gather_codegen_context_empty`** — No sandbox, no knowledge results. Assert returns empty dict.
5. **`test_develop_pipeline_codegen_failure_sets_failed`** — Mock codegen tool to raise error. Assert task status set to FAILED with appropriate message.
6. **`test_develop_pipeline_test_failure_sets_failed`** — Mock codegen success, test pipeline returns `overall_passed=False`. Assert task FAILED before review.
7. **`test_develop_pipeline_reviewer_blocks`** — Mock codegen + tests pass, reviewer returns `verdict=block`. Assert task FAILED with reviewer reasons.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 7 new tests pass.
- Full suite still green.
- `classify_request("把 OPS-123 做了")` returns `"jira_issue_develop"`.
- The orchestrator routes `jira_issue_develop` through the full pipeline.
- Each pipeline step records lifecycle events visible in the chat timeline.

## Workflow (for the executor)

<!-- Effort: xhigh — complex integration across 8 existing services -->

1. Read `app/orchestrator/service.py` (full file — focus on `classify_request`, `bootstrap_task`, `_execute_plan_impl`, `_execute_writeback_plan`), `app/services/codegen.py`, `app/services/sandbox.py`, `app/services/test_pipeline.py`, `app/services/reviewer.py`.
2. Add `jira_issue_develop` to `classify_request()`.
3. Add `_execute_develop_pipeline()` and `_gather_codegen_context()`.
4. Route in `_execute_plan_impl()`.
5. Create tests.
6. Run `python -m compileall app && python -m unittest tests.orchestrator.test_develop_pipeline -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-N1-e2e-pipeline.md
```
