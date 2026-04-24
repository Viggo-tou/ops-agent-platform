# SPEC: Iterative refinement on an existing task (task iteration)

## Problem

Current pipeline: one user prompt → one Task → one develop pipeline run → one approval gate. If the user wants to tweak the generated patch ("make the rule stricter", "also add a firebase.json"), they must submit a new prompt, which creates a **fresh task** that re-runs planner + knowledge search + codegen from scratch. The prior diff is discarded; the new task has to re-derive everything.

## Goal

Let the user continue a conversation with a task that is still `AWAITING_APPROVAL`: type a follow-up, the orchestrator uses the current (un-approved) diff as the starting point, and codegen produces an amended patch. Re-run gates on the amended diff. Present the new version for approval. Original task's state is preserved as a revision history.

## Non-goals

- Iterating after Jira transition (once `jira.transition_issue` has run, the task is locked; iteration requires opening a new Jira ticket or a new task).
- Iterating across different scenarios (a develop task cannot be iterated into a plan task).
- Multi-user collaborative iteration on the same task.

## Key design decisions

### D1. Iteration triggers on an AWAITING_APPROVAL task only

Rationale: if we allow iteration at any stage, we have races between the in-flight pipeline and the user's follow-up. Iteration only starts from a terminal-before-approval state.

If task is `failed`, `completed` (Jira already transitioned), or `executing`, reject the iteration request with a clear error.

### D2. Iteration mode = "amend", not "regenerate"

Codegen receives the **current accepted diff** + the follow-up instruction, and produces a **single amended patch** that supersedes the prior diff (not a second patch applied on top).

Rationale:
- Supersede is simpler to audit: approval is on one coherent diff, not a stack.
- Avoids patch-apply-on-patch conflicts.
- Codegen already knows how to take a plan + context and produce a unified diff; we just add the "starting point" input.

Implementation: in the codegen worker prompt, add a section `PRIOR DIFF (baseline to amend)`:
```
... existing plan / context files ...

PRIOR DIFF (baseline to amend):
<current task.latest_result_json.diff>

FOLLOW-UP INSTRUCTION FROM USER:
<new prompt text>

Your output diff must produce the FULL amended result, not a diff-on-diff.
Start from the prior diff, apply the follow-up instruction, emit the combined
diff as a single unified patch.
```

### D3. Gate re-run is mandatory

Every iteration re-runs the full stage-3 gate battery (compile_gate, diff_reviewer, spec_conformance, runtime_validation, goal_decomposition, symbol_reference, artifact_existence) on the amended diff. Any gate failure returns the task to a failure mode.

### D4. Revision history persisted

Each iteration produces a `TaskRevision` row:
```
TaskRevision {
  id: UUID
  task_id: FK Task
  revision_number: int (1, 2, 3...)
  follow_up_prompt: str | null  (null for rev 1 = original prompt)
  diff: text
  reservations_json: dict        (from reservations reviewer — see SPEC item ①)
  created_at: datetime
  gate_summary_json: dict        (passed / warned / blocked per gate)
}
```

Task.latest_result_json always points at the HEAD revision. Approval button approves the HEAD.

Rollback: user can revert to a prior revision (rev 2) which makes it the new HEAD. Not in v1 but schema supports it.

### D5. Approval semantics

Approval always acts on the CURRENT HEAD revision. The `Approval` row is linked to the revision, not just the task:
```
Approval {
  ...existing fields...
  task_revision_id: FK TaskRevision (new)
}
```

If user iterates after an approval was requested but before approval was given, the pending Approval is CANCELLED automatically and a new Approval is created for the new HEAD. UI must make this obvious: "新的改动生成了,之前的审批请求已作废".

### D6. Iteration count limits

Cap at **5 iterations per task**. After 5, require a new task. Rationale: unbounded iteration = unbounded LLM spend + audit drift.

Configurable via `OPS_AGENT_MAX_TASK_ITERATIONS=5`.

### D7. Failure recovery

If an iteration's pipeline fails (gate block, codegen error), the task keeps the PRIOR HEAD revision as its effective state. The failed iteration is still persisted as a TaskRevision with status=failed for audit. User can try again.

## API surface

### New endpoint

```
POST /api/tasks/{task_id}/iterate
Body: { follow_up: str }
Returns: 202 Accepted with { task_id, revision_number }
```

Preconditions:
- Task exists
- Task.status == AWAITING_APPROVAL
- Task.revision_count < MAX_TASK_ITERATIONS
- No active (non-cancelled) Approval blocks exist for this task on a revision newer than the one in HEAD

Side effects:
- Cancel pending Approval (status=CANCELLED, reason="superseded by task iteration")
- Transition task back to PLANNING/REVIEWING
- Submit pipeline job (`run_iteration_pipeline_job`)
- Pipeline does codegen-amend → gate battery → reservations → park-for-approval

### Modified endpoint

```
GET /api/tasks/{task_id}/revisions
Returns: [TaskRevision...] sorted by revision_number desc
```

## Orchestrator flow

New method on `PrimaryOrchestrator`: `iterate_task(task_id, follow_up, actor_name)`.

```
1. Load task, validate preconditions
2. Create TaskRevision (rev N+1) with status=pending
3. Cancel any active Approval for the task (cascade delete or mark CANCELLED)
4. Read HEAD revision's diff and plan context
5. Build codegen payload:
     context_files: same as original task (files referenced in plan)
     task_description: "FOLLOW-UP: " + follow_up (with PRIOR DIFF block)
     plan_json: extend original plan with follow_up note
6. Run codegen (reuses per-file parallel path)
7. Apply amended diff to a fresh sandbox (new sandbox dir per revision)
8. Run stage-3 gates
9. If all pass: run reservations reviewer (item ①)
10. Park for approval (new Approval linked to new revision)
11. Update Task.latest_result_json + Task.revision_count += 1
```

## Frontend changes

- Chat page detects task-bound mode: if URL is `/chat/{task_id}` and task is AWAITING_APPROVAL, the input box adds a "继续改动" submit button (separate from normal send)
- Clicking "继续改动" sends `POST /api/tasks/{id}/iterate` instead of `POST /api/tasks`
- Chat timeline renders TaskRevision dividers between iterations: `----- 改动 v2 -----`
- If an Approval was cancelled due to iteration, surface a system message: "之前的审批请求已作废,等待新版本审批"

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Codegen misinterprets "amend" and produces diff-on-diff | Explicit prompt wording + post-process check: if `--- a/filename` line has a modified baseline that doesn't match source file content, reject + retry |
| User iterates forever, racking up cost | `OPS_AGENT_MAX_TASK_ITERATIONS=5` cap |
| Audit trail confusion ("which version shipped to Jira?") | TaskRevision.id recorded in final Jira transition event payload |
| Cancelled-approval race (user clicks Approve just as iteration starts) | DB-level: cancel Approval with SELECT ... FOR UPDATE then create new revision. If race is lost, return 409 Conflict to the later request |
| Sandbox cleanup (N revisions = N sandboxes) | Delete old sandbox when new revision is created (keep current only), OR keep last 3 |

## Acceptance

- User can type follow-up in chat on AWAITING_APPROVAL task, click "继续改动", new revision appears
- Failed iteration keeps prior revision as HEAD
- 6th iteration rejected with clear message
- Once Jira transition happens, iteration API returns 409

## Estimated scope

- Backend:
  - DB schema: `task_revision` table + migration (~30 lines + migration)
  - Approval table: `task_revision_id` nullable FK (~5 lines + migration)
  - `iterate_task` method in orchestrator (~80 lines)
  - New API endpoint + pipeline job wrapper (~40 lines)
  - Prompt amendments for amend-mode codegen (~20 lines)
- Frontend:
  - Chat page iteration mode + submit path (~40 lines)
  - TaskRevision timeline rendering (~30 lines)
- Tests:
  - Happy path iteration (~40 lines)
  - Max-iterations cap (~20 lines)
  - Race: iterate during pending approval (~30 lines)

**Total**: ~350 lines + 2 migrations

## Decision points for implementation

Before coding starts, these need explicit answers from the product owner:

1. Does "approval cancelled by iteration" notify the original approver? (Who was the approver?)
2. After Jira transition, can a new iteration create a new Jira comment referencing the update? Or is it strictly locked?
3. Sandbox retention: delete-previous or keep-last-3?
4. Iteration count shown in chat UI as "v2", "v3", or hidden under a "history" disclosure?

## Dependencies

- Item ① (reservations reviewer) should land first so iteration inherits it for each revision
- Item ③a (artifact existence gate) should land first so iteration amended diffs are also verified
- Item ③b (planner declares new files) should land first so iteration plan can carry over the expected_new_files list
