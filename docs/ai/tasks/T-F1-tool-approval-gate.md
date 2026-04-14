# T-F1 — Tool-Execution Approval Gate

## Goal

When the gateway is asked to execute a tool tagged `APPROVAL_REQUIRED`, it must pause execution, create an `Approval` row, and raise a new `ToolApprovalRequired` exception. The orchestrator catches this, sets the task to `AWAITING_APPROVAL`, and records a lifecycle event. After approval is granted, the orchestrator resumes and the gateway executes the tool with the linked `approval_id`.

## Background

Phase F of the multi-agent MVP roadmap. The orchestrator already has a plan-level review→approval flow (via `ReviewerAgent`). This phase adds a **tool-level** gate so that individual high-risk tool invocations (e.g. `jira.transition_issue`, `sandbox.run_command`) cannot run without an explicit, recorded approval — even if the plan was pre-approved.

Existing infrastructure:
- `ToolPermissionCategory.APPROVAL_REQUIRED` enum exists.
- `Approval` model exists with `PENDING/GRANTED/REJECTED` statuses.
- `ApprovalService.grant()` already calls `orchestrator.resume_after_approval()`.
- The gateway's `execute()` already accepts an optional `approval_id` parameter.

What's missing: the gateway never checks `permission_category` before running. It always runs immediately.

## Design

### 1. New exception in `app/tools/gateway.py`

```python
class ToolApprovalRequired(Exception):
    """Raised when a tool requires approval before execution."""
    def __init__(self, tool_name: str, execution_id: str, approval_id: str):
        super().__init__(f"Tool '{tool_name}' requires approval (approval_id={approval_id})")
        self.tool_name = tool_name
        self.execution_id = execution_id
        self.approval_id = approval_id
```

### 2. Gate logic in `ToolGateway.execute()`

At the top of `execute()`, after creating the `ToolExecution` row but before the retry loop:

```python
if definition.permission_category == ToolPermissionCategory.APPROVAL_REQUIRED and approval_id is None:
    # Create approval, set execution to pending, raise
    approval = Approval(
        task_id=task_id,
        action_name=tool_name,
        status=ApprovalStatus.PENDING,
        requested_by_role=role.value if role else RoleName.ACTION.value,
        approver_role=ActorRole.TEAM_LEAD.value,
        requested_by_actor_name=str(actor_context.get("actor_name", "")),
        risk_level=RiskLevel.HIGH,
        risk_category=RiskCategory.CHANGE_MANAGEMENT,
        reason=f"Tool '{tool_name}' requires approval before execution.",
        request_payload_json=payload,
    )
    self.db.add(approval)
    self.db.flush()

    execution.status = ToolExecutionStatus.PENDING_APPROVAL  # new enum value
    execution.approval_id = approval.id
    self.db.flush()

    raise ToolApprovalRequired(
        tool_name=tool_name,
        execution_id=execution.id,
        approval_id=approval.id,
    )
```

When `approval_id` is provided (i.e. resuming after approval), the gate is skipped and execution proceeds normally.

### 3. New enum value

Add `PENDING_APPROVAL = "pending_approval"` to `ToolExecutionStatus` in `app/core/enums.py`.

### 4. Orchestrator handling

In `PrimaryOrchestrator._execute_plan()` (and `_execute_tool()` if it exists), catch `ToolApprovalRequired`:

```python
from app.tools.gateway import ToolApprovalRequired

try:
    result = self.tool_gateway.execute(...)
except ToolApprovalRequired as exc:
    task.pending_approval = True
    task.latest_result_json = {
        "status": TaskStatus.AWAITING_APPROVAL.value,
        "message": f"Tool '{exc.tool_name}' requires approval before execution.",
        "approval_id": exc.approval_id,
        "execution_id": exc.execution_id,
    }
    set_task_status(self.db, task=task,
        new_status=TaskStatus.AWAITING_APPROVAL,
        new_stage=WorkflowStage.ACTION,
        role=RoleName.ACTION,
        source=EventSource.ORCHESTRATOR,
        message=f"Task paused: tool '{exc.tool_name}' awaiting approval.")
    record_event(self.db, task_id=task.id,
        event_type=EventType.APPROVAL_REQUESTED,
        source=EventSource.TOOL_GATEWAY,
        stage=WorkflowStage.ACTION,
        role=RoleName.ACTION,
        tool_name=exc.tool_name,
        message=f"Approval requested for tool '{exc.tool_name}'.",
        payload={"approval_id": exc.approval_id, "execution_id": exc.execution_id})
    return  # task stays paused
```

### 5. Resume path

`resume_after_approval()` already exists and calls `_execute_plan()`. It passes `approval_id`, which flows through to `tool_gateway.execute(approval_id=...)`. The gate sees `approval_id is not None` and skips the check — execution proceeds.

The existing `ToolExecution` row (created during the first attempt) should be updated: set `approval_id`, reset `status` from `PENDING_APPROVAL` to `RUNNING`, then the retry loop runs normally. Add a helper `_resume_pending_execution()` in the gateway that finds the existing execution by approval_id and reuses it instead of creating a duplicate.

## Files to edit

1. `apps/backend/app/core/enums.py` — add `PENDING_APPROVAL` to `ToolExecutionStatus`.
2. `apps/backend/app/tools/gateway.py` — add `ToolApprovalRequired` exception, gate logic, `_resume_pending_execution()`.
3. `apps/backend/app/orchestrator/service.py` — catch `ToolApprovalRequired` in `_execute_plan()` / tool execution paths.

## Files to create

4. `apps/backend/tests/tools/test_tool_approval_gate.py`

## Tests

Use `unittest.TestCase`. Mock `self.db` with a minimal stub that supports `add()`, `flush()`, `scalars()`. Tests should not need a real database.

1. **`test_read_only_tool_executes_without_approval`** — Create a gateway with a `READ_ONLY` tool. Call `execute()` without `approval_id`. Assert it runs immediately (no `ToolApprovalRequired`).
2. **`test_approval_required_tool_raises_without_approval_id`** — Create a gateway with an `APPROVAL_REQUIRED` tool. Call `execute()` without `approval_id`. Assert `ToolApprovalRequired` is raised with correct `tool_name` and `approval_id`.
3. **`test_approval_required_tool_executes_with_approval_id`** — Same tool, but pass `approval_id="test-approval"`. Assert the tool runs successfully, no exception.
4. **`test_write_tool_executes_without_approval`** — A `WRITE` tool executes immediately without `approval_id` (only `APPROVAL_REQUIRED` triggers the gate).
5. **`test_pending_approval_status_set`** — After `ToolApprovalRequired` is raised, verify the `ToolExecution` row has `status == PENDING_APPROVAL`.
6. **`test_approval_row_created`** — After raise, verify an `Approval` row was added to the session with correct fields.

## Acceptance criteria

- `python -m compileall app` exits 0 from `apps/backend/`.
- All 6 new tests pass.
- Full suite still green: `python -m unittest discover -s tests -v`.
- A `READ_ONLY` or `WRITE` tool can still execute immediately.
- An `APPROVAL_REQUIRED` tool without `approval_id` raises `ToolApprovalRequired`.
- An `APPROVAL_REQUIRED` tool with `approval_id` executes normally.

## Workflow (for the executor, i.e. Codex)

1. Read `app/core/enums.py`, `app/tools/gateway.py`, `app/orchestrator/service.py`, `app/services/approvals.py`, `app/models/approval.py`.
2. Add `PENDING_APPROVAL` to `ToolExecutionStatus` in enums.
3. Add `ToolApprovalRequired` + gate logic + `_resume_pending_execution()` to gateway.
4. Add `ToolApprovalRequired` catch in orchestrator `_execute_plan()`.
5. Create tests.
6. Run `python -m compileall app && python -m unittest tests.tools.test_tool_approval_gate -v && python -m unittest discover -s tests -v`.

Invocation:

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-F1-tool-approval-gate.md
```
