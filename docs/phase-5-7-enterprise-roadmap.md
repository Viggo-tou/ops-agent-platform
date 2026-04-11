# Phase 5-7 Enterprise Roadmap

## Current Baseline

The platform now has a workable end-to-end loop:

- User request intake
- Task creation and durable event logging
- Semantic translation
- Planner output with natural-language change guidance
- Reviewer validation
- Tool execution with logs
- Dashboard visibility for task, review, evidence, and tool activity

The most recent optimization was intentional:

- MiniMax now powers semantic translation and planner generation
- Planner output now explains what should change in natural language
- Planner output includes likely source-code locations from knowledge retrieval
- Planner requests are grounded with Jira context and compact repository evidence

### Why these changes were worth doing first

Before governance and approval work, the system needed to produce plans that a human can actually inspect.

That matters because:

- approvals are meaningless if the plan is unreadable
- reviewer checks are weak if the plan has no grounded code context
- enterprise demos are weak if the agent cannot explain what it intends to change
- future guardrails need structured intermediate artifacts, not only free-form text

In short: the recent planner work upgraded the system from "model produced something" to "model produced something reviewable."

## Guiding Principles

The next phases should preserve these constraints:

- keep one orchestrator runtime until governance flows are stable
- keep all decisions auditable through events and snapshots
- prefer explicit policy evaluation over hidden prompt behavior
- require human approval for high-risk actions before scale-out work
- only split into async workers after approval, rollback, and audit paths are reliable

## Phase 5: Approval + RBAC + Risk Guardrails

### Goal

Make the system behave like an enterprise tool instead of a smart demo by adding access control, policy evaluation, approval routing, and high-risk blocking.

### What to build

#### 1. RBAC matrix

Introduce explicit role definitions and action scope.

Suggested first roles:

- `employee`
- `team_lead`
- `manager`
- `admin`
- `system`

Suggested first resource groups:

- `knowledge`
- `slack`
- `jira`
- `notion`
- `internal_api`
- `internal_db`
- `admin_settings`

Suggested permission levels:

- `allow`
- `approval_required`
- `deny`

Suggested first-pass matrix:

| Resource / Action | employee | team_lead | manager | admin |
| --- | --- | --- | --- | --- |
| knowledge.search | allow | allow | allow | allow |
| slack.post_message.private_or_team | allow | allow | allow | allow |
| slack.post_message.public_broadcast | approval_required | approval_required | allow | allow |
| jira.get_issue | allow | allow | allow | allow |
| jira.create_issue | allow | allow | allow | allow |
| notion.update_published_doc | deny | approval_required | approval_required | allow |
| internal_api.read | allow | allow | allow | allow |
| internal_api.write | deny | approval_required | approval_required | allow |
| internal_db.read | deny | approval_required | approval_required | allow |
| internal_db.write | deny | deny | approval_required | allow |
| prod_config.change | deny | deny | approval_required | allow |
| admin_settings.manage | deny | deny | deny | allow |

#### 2. Action policy engine

Add a deterministic policy engine that evaluates each planned action before execution.

Policy inputs:

- actor role
- scenario
- tool name
- target scope
- environment
- risk signals
- task metadata
- planner output

Policy outputs:

- `allow`
- `require_approval`
- `deny`
- `allowed_with_constraints`

Recommended response shape:

```json
{
  "decision": "require_approval",
  "policy_rule_id": "slack.public_broadcast.employee.v1",
  "reason": "Public broadcast requires lead approval",
  "risk_level": "medium",
  "required_approver_role": "team_lead",
  "constraints": ["channel_scope=public"]
}
```

#### 3. Approval workflow

Approval should become a first-class workflow instead of a side branch.

Required states:

- `pending`
- `granted`
- `rejected`
- `expired`
- `cancelled`

Recommended task flow:

- `created`
- `planning`
- `reviewing`
- `awaiting_approval`
- `executing`
- `completed`
- `failed`
- `rolled_back`

Required approval data:

- requester
- approver role
- approver identity
- requested action
- risk summary
- plan snapshot
- evidence snapshot
- decision note
- expiry timestamp

#### 4. High-risk guardrails

Add explicit risk categories rather than only generic `low / medium / high`.

Suggested first categories:

- `external_broadcast`
- `knowledge_exfiltration`
- `production_write`
- `configuration_change`
- `cross_team_notification`
- `privileged_data_access`

Suggested default rules:

- knowledge lookup: allow
- Jira creation: allow
- Slack public broadcast: lead approval
- Notion published-doc update: approval required
- internal DB write: deny unless admin
- production config change: deny unless admin

#### 5. Auditability additions

Every policy and approval step should emit durable events.

New event types:

- `policy_evaluation_started`
- `policy_evaluation_completed`
- `policy_denied`
- `approval_assigned`
- `approval_expired`
- `approval_cancelled`
- `guardrail_triggered`

### Backend deliverables

- `rbac_role`, `policy_rule`, and policy-config storage
- policy evaluation service
- approval routing service
- guardrail evaluator
- task pre-execution authorization gate
- approval-aware reviewer output

### Frontend deliverables

- approval queue page
- task detail approval history panel
- policy decision badges
- denied-task explanation panel
- approver action UI with reason capture

### Acceptance criteria

- every tool call is policy-checked before execution
- approval-required actions cannot bypass approval
- denied actions are blocked before external side effects
- approval history is visible in the dashboard
- audit trail explains who requested, who approved, and why

### Recommended implementation order

1. RBAC data model
2. policy engine
3. approval queue and APIs
4. task-execution gate
5. UI visibility and audit surfaces

## Phase 6: Enterprise Demo Dashboard

### Goal

Turn the current UI from functional MVP screens into a cohesive enterprise console that is strong in demos and strong in operator comprehension.

### Required modules

#### 1. Request Console

Show:

- prompt input
- actor identity and role
- tool readiness snapshot
- recent requests
- scenario hints

#### 2. Task List

Show:

- task status
- scenario
- planner provider
- reviewer verdict
- approval state
- last updated time
- filters for role, status, provider, risk, session

#### 3. Task Detail

Show:

- original user request
- session metadata
- status history
- current stage
- final response

#### 4. Plan and Review Panel

Show:

- planner summary
- change explanation
- likely code locations
- assumptions
- missing information
- reviewer verdict
- reviewer findings
- policy results

#### 5. Tool Execution Logs

Show:

- execution timeline
- tool requests and results
- retries
- timeouts
- failures
- permission category
- execution duration

#### 6. Approval Queue

Show:

- pending approvals
- requester
- requested action
- risk level
- due time
- approve/reject actions

#### 7. Admin Settings (optional for first cut)

Show:

- role policy matrix
- tool registry status
- environment readiness
- allowed scopes
- approval rules

### UI design goals

- dashboard should explain system state without opening raw JSON
- every high-risk operation should have a visible explanation trail
- important statuses must be scannable within 5 seconds
- task detail should read like an enterprise case file

### Task Detail target layout

Each task detail page should surface:

- original request
- semantic translation
- planner output
- knowledge evidence
- reviewer verdict
- tool calls timeline
- final response
- approval history

### Recommended implementation order

1. task detail information architecture cleanup
2. approval queue
3. tool execution timeline polish
4. admin/settings visibility
5. cross-page filter consistency

## Phase 7: Async Execution and Real Multi-Agent Scale-Out

### Goal

Only after governance and visibility are stable, introduce scale and runtime decomposition.

### Why this phase comes later

Without Phase 5 and Phase 6 in place:

- async failures are harder to investigate
- multi-agent behavior becomes less auditable
- approval gates become easier to bypass accidentally
- long-running workflows become difficult to explain in demos

### What to build

#### 1. Queue and worker model

- async task queue
- long-running workflow persistence
- resumable execution steps
- retry orchestration outside the request thread

#### 2. Tool runners

- dedicated tool workers
- timeout and retry policies by tool class
- cancellation hooks
- idempotency keys for write actions

#### 3. Real multi-agent decomposition

Possible split after stabilization:

- `primary runtime`
- `planner worker`
- `knowledge worker`
- `action worker`
- `reviewer worker`

This should only happen after each role already has a stable contract.

#### 4. Streaming and live updates

- server-sent events or websocket stream
- live status updates
- incremental tool-log updates
- long-running task progress

#### 5. Sandboxed code execution

- isolated repo workspaces
- per-task scratch environments
- reproducible test runs
- controlled rollback and cleanup

### Acceptance criteria

- task execution survives process restarts
- long-running jobs can resume safely
- write actions are idempotent or explicitly guarded
- UI receives live updates without polling-heavy hacks
- role separation does not reduce audit visibility

## Recommended Program Order

### Wave 1

Phase 5 foundation:

- RBAC model
- policy engine
- approval workflow
- guardrail events

### Wave 2

Phase 6 enterprise UI:

- approval queue
- polished task detail
- policy and audit visibility
- operator-friendly logs

### Wave 3

Phase 5 hardening:

- admin settings
- richer policy scopes
- denial reasons
- expiry and escalation paths

### Wave 4

Phase 7 scale-out:

- queue
- async runners
- streaming updates
- optional service decomposition

## Suggested Next Task Cards

Recommended immediate implementation sequence:

1. `T-024` Phase 5 data model and enums - done
2. `T-025` minimal AI workbench frontend refactor - done
3. `T-027` resumable development state files - done
4. `T-028` fix chat knowledge answer chain - done
5. `T-029` strict reference UI pass - done
6. `T-032` same-conversation follow-up turns - done
7. `T-033` environment handoff documentation - done
8. `T-026` workbench backend persistence and governance integration - next
9. `T-030` approval APIs, approval queue UI, and policy decision surfacing
10. `T-031` async execution design spike

## Summary

The platform is now at the point where governance is the highest-leverage next step.

The right next move is not more model cleverness. It is:

- approval
- access control
- risk gating
- audit visibility

Once that is in place, the dashboard becomes much stronger, and only then does deeper async or multi-agent splitting become worth the complexity.
