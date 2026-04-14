# T-D1 — Reproducible Test Pipeline Runner

## Goal

Add a `TestPipeline` service and a `test_pipeline.run` tool that reads a `tests.yaml` from a sandbox and runs each test step via `sandbox.run_command`, aggregating results into a structured pass/fail verdict.

## Background

Phase D of the multi-agent MVP roadmap. Depends on T-C1 (`ExecutionSandbox` + `sandbox.run_command`).

The test pipeline reads a declarative `tests.yaml` from the sandboxed repo root and runs each step in order. If any required step fails, the pipeline stops and reports failure. Non-required steps can fail without blocking.

## Design

### `tests.yaml` schema

Located at the sandbox root. Example:

```yaml
steps:
  - name: lint
    command: "npm run lint"
    timeout_seconds: 60
    required: true
  - name: unit
    command: "npm test"
    timeout_seconds: 120
    required: true
  - name: integration
    command: "npm run test:integration"
    timeout_seconds: 180
    required: false
```

### TestPipeline service

New file: `apps/backend/app/services/test_pipeline.py`.

```python
@dataclass
class TestStepResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    required: bool
    passed: bool  # exit_code == 0 and not timed_out

@dataclass
class TestRunResult:
    steps: list[TestStepResult]
    overall_passed: bool  # all required steps passed
    total_steps: int
    passed_count: int
    failed_count: int
    skipped_count: int  # steps skipped because a prior required step failed
    duration_ms: int
```

The runner:
1. Reads `tests.yaml` from the sandbox root.
2. Parses and validates the step list.
3. Runs each step via `ExecutionSandbox.run()`.
4. If a required step fails, marks all subsequent steps as skipped.
5. Returns `TestRunResult`.

### Tool

`test_pipeline.run` — registered in the tool registry as `APPROVAL_REQUIRED`.

Gateway executor:
- Required payload: `task_id: str`.
- Optional: `config_path: str` (default `"tests.yaml"`).
- Creates sandbox, reads the config, runs the pipeline, returns structured result.

## Files to create

1. `apps/backend/app/services/test_pipeline.py`
2. `apps/backend/tests/services/test_pipeline.py` (unit tests)

## Files to edit

3. `apps/backend/app/tools/registry.py` — add `test_pipeline.run` tool definition.
4. `apps/backend/app/tools/gateway.py` — add dispatcher + executor method.
5. `apps/backend/app/services/governance.py` — add 2 policy rules.

## Tests

1. **`test_pipeline_all_pass`** — Create a sandbox with a `tests.yaml` containing 2 required steps that both succeed (use `echo ok` or `python -c "print('ok')"`). Assert `overall_passed=True`, `passed_count=2`.
2. **`test_pipeline_required_step_fails`** — One required step fails (`exit 1`). Assert `overall_passed=False`, subsequent steps are skipped.
3. **`test_pipeline_optional_step_fails`** — One non-required step fails. Assert `overall_passed=True` (required steps all pass).
4. **`test_pipeline_missing_config`** — No `tests.yaml` in sandbox. Assert error raised.
5. **`test_pipeline_empty_steps`** — `tests.yaml` with empty `steps` list. Assert `overall_passed=True`, `total_steps=0`.

Platform-aware commands (use `python -c` for cross-platform portability).

## Acceptance criteria

- `python -m compileall app` exits 0.
- `test_pipeline.run` in tool registry as `APPROVAL_REQUIRED`.
- All 5 tests pass.
- Save to `docs/ai/runs/T-D1.log`.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/services/sandbox.py` (T-C1 output), registry, gateway, governance.
2. Create `test_pipeline.py` service.
3. Wire tool in registry, gateway, governance.
4. Write tests.
5. Compile + run tests. Save to `docs/ai/runs/T-D1.log`.

Invocation:

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-D1-test-pipeline.md
```
