# Phase 2 Planner + Reviewer

## Goal
Add two internal roles on top of the current single-runtime orchestrator:

1. `planner agent`: turn a user request into a structured execution plan
2. `reviewer agent`: validate the plan and execution output for completeness, policy, and risk

This is the first phase that behaves like multi-agent orchestration, but it still stays inside one runtime and one orchestrator process.

## Phase 2 Flow
1. user submits request
2. `primary` normalizes the request and creates the task
3. `planner` emits a structured plan
4. `reviewer` checks the plan
5. if the review passes, the task moves into execution
6. if approval is required, the task pauses for approval before execution
7. after execution, the `reviewer` can validate the final output before completion

## Role Responsibilities

### Primary
- Accept the user request
- Normalize task input and runtime context
- Start planner and reviewer stages
- Return high-level task state to the UI

### Planner
- Convert the request into a structured plan
- Break the work into ordered steps
- Declare tools, risks, dependencies, and approval requirements
- Identify missing information before execution starts

### Reviewer
- Check that the plan is complete and internally consistent
- Check whether requested actions exceed current permissions or scope
- Check whether the output is safe, complete, and ready to execute
- Decide whether execution can proceed, needs approval, or should fail

## Task Status Transitions

Phase 2 task status should move to this set:

- `created`
- `planning`
- `reviewing`
- `awaiting_approval`
- `executing`
- `completed`
- `failed`

Recommended transitions:

1. `created -> planning`
   Trigger: task is accepted by `primary` and handed to `planner`

2. `planning -> reviewing`
   Trigger: `planner` emits a valid structured plan

3. `planning -> failed`
   Trigger: planner cannot produce a valid plan after retries or the request is not actionable

4. `reviewing -> executing`
   Trigger: `reviewer` verdict is `approved` and no approval gate is needed

5. `reviewing -> awaiting_approval`
   Trigger: `reviewer` finds that one or more planned actions require human approval

6. `reviewing -> failed`
   Trigger: `reviewer` finds missing critical information, policy violations, or unrecoverable scope issues

7. `awaiting_approval -> executing`
   Trigger: approval is granted

8. `awaiting_approval -> failed`
   Trigger: approval is rejected or expires

9. `executing -> completed`
   Trigger: planned steps finish successfully and the final output passes review

10. `executing -> failed`
    Trigger: tool execution fails, output review fails, or the runtime cannot recover safely

## Plan Schema

The planner should output a single structured document. Store it in `task.plan_json`.

```json
{
  "schema_version": "phase2.plan.v1",
  "plan_id": "plan_123",
  "task_id": "task_123",
  "objective": "Create a Jira ticket draft for the requested dashboard bug",
  "request_summary": "User wants a Jira draft for a dashboard filter issue",
  "scenario": "jira_ticket_draft",
  "assumptions": [
    "The requester has permission to create a draft ticket"
  ],
  "missing_information": [],
  "risk_level": "medium",
  "requires_approval": false,
  "approval_reasons": [],
  "tools": [
    {
      "tool_name": "jira.create_ticket_draft",
      "permission_category": "write",
      "purpose": "Create a draft ticket"
    }
  ],
  "steps": [
    {
      "step_id": "step_1",
      "title": "Extract ticket details from the request",
      "kind": "analysis",
      "owner_role": "planner",
      "depends_on": [],
      "tool_name": null,
      "expected_output": "Structured ticket fields",
      "success_criteria": "Title, summary, and context are clear"
    },
    {
      "step_id": "step_2",
      "title": "Create Jira draft ticket",
      "kind": "action",
      "owner_role": "action",
      "depends_on": ["step_1"],
      "tool_name": "jira.create_ticket_draft",
      "expected_output": "Draft ticket key and summary",
      "success_criteria": "Draft ticket is created successfully"
    }
  ],
  "final_output_contract": {
    "type": "jira_ticket_draft",
    "required_fields": ["ticket_key", "status", "summary"]
  },
  "provider": {
    "name": "mock",
    "mode": "deterministic_planner",
    "model": null
  }
}
```

### Required plan fields
- `schema_version`: schema version for compatibility
- `plan_id`: unique identifier for the plan instance
- `task_id`: owning task id
- `objective`: plain-language goal
- `request_summary`: normalized user intent
- `scenario`: scenario classification
- `assumptions`: assumptions the planner made
- `missing_information`: unresolved inputs needed for safe execution
- `risk_level`: `low | medium | high`
- `requires_approval`: boolean gate
- `approval_reasons`: why approval is required
- `tools`: planned tool usage summary
- `steps`: ordered execution steps
- `final_output_contract`: what the executor must produce
- `provider`: planner provider metadata

### Step schema
- `step_id`: stable step identifier
- `title`: short description of the step
- `kind`: `analysis | knowledge | action | review`
- `owner_role`: role expected to own the step
- `depends_on`: upstream step ids
- `tool_name`: tool to call, if any
- `expected_output`: what the step should produce
- `success_criteria`: how the orchestrator/reviewer knows the step is done

## Review Schema

The reviewer should output a structured review document and store it in `task.latest_result_json` during review and final validation phases, or in a dedicated `review_json` field in a later schema upgrade.

```json
{
  "schema_version": "phase2.review.v1",
  "review_id": "review_123",
  "task_id": "task_123",
  "plan_id": "plan_123",
  "review_stage": "pre_execution",
  "verdict": "approved",
  "ready_for_execution": true,
  "summary": "Plan is complete and within scope for execution",
  "findings": [],
  "missing_information": [],
  "policy_checks": [
    {
      "name": "tool_scope",
      "status": "passed",
      "detail": "Only approved mock Jira write scope is requested"
    },
    {
      "name": "approval_gate",
      "status": "passed",
      "detail": "No approval-required tools were found"
    }
  ],
  "approval_requirements": [],
  "recommended_status": "executing",
  "provider": {
    "name": "mock",
    "mode": "deterministic_reviewer",
    "model": null
  }
}
```

### Required review fields
- `schema_version`: schema version
- `review_id`: unique review identifier
- `task_id`: owning task id
- `plan_id`: reviewed plan id
- `review_stage`: `pre_execution | post_execution`
- `verdict`: `approved | requires_approval | needs_info | rejected`
- `ready_for_execution`: execution gate flag
- `summary`: human-readable review conclusion
- `findings`: issues or warnings found by the reviewer
- `missing_information`: unresolved blockers
- `policy_checks`: explicit permission and scope checks
- `approval_requirements`: approvals needed before execution
- `recommended_status`: target status to move the task into
- `provider`: reviewer provider metadata

### Review finding schema
- `code`: machine-readable issue code
- `severity`: `info | warning | error`
- `message`: short finding description
- `step_id`: optional related plan step
- `field`: optional related plan field

### Policy check schema
- `name`: policy name
- `status`: `passed | warning | failed`
- `detail`: short explanation

## Recommended Reviewer Decisions

Map reviewer verdicts to statuses like this:

- `approved` -> `executing`
- `requires_approval` -> `awaiting_approval`
- `needs_info` -> `failed`
- `rejected` -> `failed`

For Phase 2, map `needs_info` to `failed` instead of inventing a separate status. A later phase can introduce `waiting_for_input`.

## Orchestrator Contract

Phase 2 still uses one orchestrator process, but the internal sequence becomes:

1. `primary.intake(task)`
2. `planner.generate_plan(task) -> plan_json`
3. `reviewer.review_plan(task, plan_json) -> review_json`
4. if approved: `executor.run(task, plan_json)`
5. `reviewer.review_output(task, result_json)` when needed
6. finalize task status

Suggested internal interfaces:

```text
PrimaryOrchestrator.run(task_id)
PlannerRole.generate_plan(task_state) -> PlanDocument
ReviewerRole.review_plan(task_state, plan) -> ReviewDocument
ReviewerRole.review_output(task_state, plan, result) -> ReviewDocument
```

## Event Additions

Phase 2 should add explicit events for the new roles:

- `planning_started`
- `plan_generated`
- `review_started`
- `review_passed`
- `review_failed`
- `execution_started`
- `execution_completed`
- `execution_failed`

These remain append-only records in the existing `event` table.

## Implementation Notes

- Keep one runtime and one orchestrator process
- Treat `planner` and `reviewer` as internal role executors, not separate services
- Reuse the existing `task`, `event`, and `approval` tables unless a dedicated `review` table is later justified
- Keep mock tools in place for Phase 2
- Prefer adding `review_json` to `task` only if `latest_result_json` becomes too overloaded

## Phase 2 Exit Criteria

Phase 2 is complete when:

- a task moves through `created -> planning -> reviewing -> executing -> completed`
- a task that requires approval moves through `created -> planning -> reviewing -> awaiting_approval`
- the planner always emits a structured plan schema
- the reviewer always emits a structured review schema
- dashboard users can see the current status and the latest plan/review outcome
