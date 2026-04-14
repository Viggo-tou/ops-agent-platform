# T-G1 — Rollback Inverse Actions

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

Every mutating tool execution stores an `inverse_action` descriptor so rollback can replay inverses in reverse order. Wire `POST /tasks/{task_id}/rollback` to execute concrete inverse operations (git revert, Jira status restore, sandbox teardown) instead of just cancelling approvals.

## Background

Phase G of the multi-agent MVP roadmap. The rollback endpoint exists (`app/services/tasks.py:192`) but only cancels pending approvals and sets status to `ROLLED_BACK`. No actual side-effect reversal happens. This task adds:
1. An `inverse_action_json` column on `ToolExecution`.
2. Inverse descriptors recorded by the gateway after each mutating tool succeeds.
3. A `RollbackExecutor` that replays inverses in reverse chronological order.
4. Integration into the existing `rollback_task()` method.

## Design

### 1. New column on ToolExecution

Add to `app/models/tool_execution.py`:

```python
inverse_action_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

This stores a descriptor like:
```json
{
  "type": "git_revert",
  "sandbox_dir": "data/sandboxes/task-1",
  "before_sha": "abc1234"
}
```

Or for Jira:
```json
{
  "type": "jira_transition",
  "issue_key": "OPS-123",
  "from_status": "In Progress",
  "to_status": "To Do"
}
```

### 2. Gateway records inverse after success

In `ToolGateway.execute()`, after a successful execution (after `execution.status = SUCCEEDED`), call a new method `_build_inverse_action(tool_name, payload, result)` that returns the inverse descriptor based on tool name:

| Tool | Inverse type | Key fields |
|------|-------------|------------|
| `sandbox.apply_patch` | `git_revert` | `sandbox_dir`, `before_sha` from result |
| `sandbox.run_command` | `None` (read-only by nature, unless payload hints otherwise) |
| `jira.transition_issue` | `jira_transition` | `issue_key`, swap `from_status`/`to_status` from result |
| `jira.add_comment` | `jira_delete_comment` | `issue_key`, `comment_id` from result |
| `test_pipeline.run` | `None` (read-only) |
| `diff_reviewer.review` | `None` (read-only) |
| Everything else | `None` |

Store: `execution.inverse_action_json = inverse_descriptor`.

### 3. RollbackExecutor

New file: `app/services/rollback.py`

```python
@dataclass
class RollbackStepResult:
    execution_id: str
    tool_name: str
    inverse_type: str
    success: bool
    message: str

@dataclass
class RollbackResult:
    steps: list[RollbackStepResult]
    all_succeeded: bool
    total_steps: int
    succeeded_count: int
    failed_count: int
    skipped_count: int  # executions with no inverse

class RollbackExecutor:
    def __init__(self, db: Session):
        self.db = db

    def execute_rollback(self, task_id: str) -> RollbackResult:
        """Load all ToolExecution rows for the task, replay inverses in reverse order."""
        ...

    def _execute_inverse(self, inverse: dict) -> RollbackStepResult:
        """Dispatch to the appropriate inverse handler."""
        if inverse["type"] == "git_revert":
            return self._revert_git(inverse)
        elif inverse["type"] == "jira_transition":
            return self._revert_jira_transition(inverse)
        elif inverse["type"] == "jira_delete_comment":
            return self._revert_jira_comment(inverse)
        ...

    def _revert_git(self, inverse: dict) -> RollbackStepResult:
        """git reset --hard <before_sha> in the sandbox dir."""
        ...

    def _revert_jira_transition(self, inverse: dict) -> RollbackStepResult:
        """Transition Jira issue back to previous status (placeholder — logs intent, does not call Jira API)."""
        ...

    def _revert_jira_comment(self, inverse: dict) -> RollbackStepResult:
        """Delete Jira comment (placeholder — logs intent, does not call Jira API)."""
        ...
```

For Phase G, the git_revert handler actually runs `git reset --hard` via subprocess in the sandbox dir. The Jira handlers are **placeholder** — they log the intended rollback but don't call the Jira API (that requires network + auth, deferred to later). This is explicitly noted in the `RollbackStepResult.message`.

### 4. Integration into rollback_task()

In `app/services/tasks.py`, `rollback_task()` — after cancelling pending approvals and before setting the final status:

```python
from app.services.rollback import RollbackExecutor

executor = RollbackExecutor(self.db)
rollback_result = executor.execute_rollback(task_id=task.id)

task.latest_result_json = {
    "status": TaskStatus.ROLLED_BACK.value,
    "message": f"Rollback completed: {rollback_result.succeeded_count}/{rollback_result.total_steps} inverses executed.",
    "rollback": {
        "total_steps": rollback_result.total_steps,
        "succeeded": rollback_result.succeeded_count,
        "failed": rollback_result.failed_count,
        "skipped": rollback_result.skipped_count,
        "steps": [{"execution_id": s.execution_id, "tool_name": s.tool_name,
                    "inverse_type": s.inverse_type, "success": s.success,
                    "message": s.message} for s in rollback_result.steps],
    },
    "reason": payload.reason,
}
```

## Files to create

1. `apps/backend/app/services/rollback.py`
2. `apps/backend/tests/services/test_rollback.py`

## Files to edit

3. `apps/backend/app/models/tool_execution.py` — add `inverse_action_json` column.
4. `apps/backend/app/tools/gateway.py` — add `_build_inverse_action()`, store after success.
5. `apps/backend/app/services/tasks.py` — integrate `RollbackExecutor` into `rollback_task()`.

## Tests

All in `apps/backend/tests/services/test_rollback.py`. Use `unittest.TestCase`.

1. **`test_rollback_git_revert`** — Create a temp dir with a git repo, make a commit, record a `ToolExecution` with `inverse_action_json={"type": "git_revert", "sandbox_dir": ..., "before_sha": <initial_sha>}`. Run rollback. Assert HEAD is back to `before_sha`.
2. **`test_rollback_jira_transition_placeholder`** — `ToolExecution` with `inverse_action_json={"type": "jira_transition", ...}`. Run rollback. Assert `success=True`, message contains "placeholder".
3. **`test_rollback_jira_comment_placeholder`** — Same pattern for `jira_delete_comment`. Assert placeholder behavior.
4. **`test_rollback_no_inverse_skipped`** — `ToolExecution` with `inverse_action_json=None`. Assert `skipped_count=1`.
5. **`test_rollback_multiple_in_reverse_order`** — Two executions (A created first, B created second). Rollback runs B's inverse before A's. Assert order via a side-effect list.
6. **`test_rollback_empty_task`** — Task with no tool executions. Assert `all_succeeded=True`, `total_steps=0`.
7. **`test_build_inverse_action_sandbox_apply_patch`** — Call `_build_inverse_action("sandbox.apply_patch", ..., result_with_before_sha)`. Assert correct `git_revert` descriptor.
8. **`test_build_inverse_action_read_only_returns_none`** — Call for `diff_reviewer.review`. Assert `None`.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 8 new tests pass.
- Full suite still green.
- `ToolExecution` model has `inverse_action_json` column.
- Gateway populates `inverse_action_json` for `sandbox.apply_patch` and Jira writeback tools.
- `rollback_task()` calls `RollbackExecutor` and includes rollback details in `latest_result_json`.

## Workflow (for the executor)

<!-- Effort: xhigh -->

1. Read `app/models/tool_execution.py`, `app/tools/gateway.py`, `app/services/tasks.py`, `app/services/sandbox.py`.
2. Add `inverse_action_json` column to ToolExecution model.
3. Add `_build_inverse_action()` to gateway, call after successful execution.
4. Create `app/services/rollback.py` with `RollbackExecutor`.
5. Integrate into `rollback_task()`.
6. Create tests.
7. Run `python -m compileall app && python -m unittest tests.services.test_rollback -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-G1-rollback-inverses.md
```
