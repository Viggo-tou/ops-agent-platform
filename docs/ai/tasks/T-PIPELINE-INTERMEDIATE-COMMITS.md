# T-PIPELINE-INTERMEDIATE-COMMITS — Intermediate DB commits at pipeline gates

## Problem

`apps/backend/app/services/tasks.py:34-81` (`run_pipeline_job`) opens one `SessionLocal()`, runs the entire `orchestrator.bootstrap_task()` chain (planner → evidence → codegen → sandbox → compile → test → review → approval gate, which for develop tasks can span 10-15 minutes), and calls `db.commit()` exactly once at the end.

All intermediate `set_task_status()` / `record_event()` writes go through `db.flush()` (`events.py:44`) but are **never committed** until the pipeline terminates.

## Observed impact

- UI stuck on "处理中…" for the entire pipeline duration. No feedback even though the orchestrator is actively progressing.
- If the pipeline crashes mid-flight, all intermediate progress is lost — the task appears to have never moved past `CREATED/INTAKE` in the DB.
- Task `b9e773e8` (2026-04-24): orchestrator logged 20+ lifecycle events (plan → review → codegen → sandbox → compile → 5× syntax repair → re-compile) over 15 minutes. DB stayed at `CREATED/INTAKE` with 2 events until pipeline terminated. User perceived this as "卡住了".
- Task `958d4e8a` (2026-04-24, successful approval flow): same architecture — fast path (~2 min) so less visible, but same root cause.

## Proposed fix

Commit (not just flush) at **stable gate boundaries** — points where the pipeline has completed a discrete phase and the DB state is a valid resumable checkpoint. Candidate boundaries:

1. After `set_task_status(PLANNING, ...)` — planner starts
2. After `set_task_status(REVIEW, ...)` — review phase starts
3. After `record_event(TOOL_SUCCEEDED, codegen.generate_patch)` — codegen done
4. After `record_event(TOOL_SUCCEEDED, sandbox.apply_patch)` — patch applied
5. After `record_event(TOOL_SUCCEEDED, compile_gate.check)` — compile pass
6. After `set_task_status(AWAITING_APPROVAL, REVIEW)` — approval gate reached
7. Before any long-running external call (`minimax.plan`, `codex exec`, `jira.transition`) — so crash during the call leaves a recoverable checkpoint

**Implementation sketch:**

- Add `commit_checkpoint(db)` helper next to `record_event` that calls `db.commit()` and handles rollback-on-failure.
- In `PrimaryOrchestrator`, insert `commit_checkpoint(self.db)` at each boundary above.
- In `services/tasks.py:run_pipeline_job`, the final `db.commit()` remains as safety net.

**Risk surface:**
- SQLite WAL already handles concurrent reads during writes — no blocking expected on the API reads that power the UI poll loop.
- Approval gate commits are already implicitly required (else the frontend can't see `pending_approval=True` to render the block) — in practice the commit currently happens because `_park_for_jira_approval` is followed by return from the pipeline closure, which reaches the outer `db.commit()`. For non-terminal checkpoints (codegen done, compile passed) there's no such implicit commit.
- No schema change required.

## Acceptance

- Submit a develop task via `/chat`. Within 30 seconds of submit, `GET /api/tasks/{id}/events` returns at least the `task_created` + `planner_started` events.
- At each of the 7 boundaries, a subsequent `curl` shows the new event persisted.
- Kill the backend mid-pipeline (e.g., during codegen). Restart. The task's DB state reflects its last committed checkpoint — not "CREATED/INTAKE with 2 events".
- No regressions in `pytest apps/backend/tests/`.

## Related

- Observability patch (this session, uncommitted in worktree `feat/provider-observability`): `CodegenResult.attempt_history` exposes codegen provider fallback chain. Complementary but independent from intermediate commits.
- Streaming SSE ticket (`docs/ai/tasks/T-STREAMING-SSE.md`, existing): a different approach — push events via SSE instead of polling committed DB state. Intermediate commits make polling work better; SSE would reduce polling frequency. The two are complementary, not alternatives.

## Priority

Medium. Not a blocker for any in-flight feature, but **every user running a multi-minute develop task perceives the system as hung** until it finishes. High UX impact for relatively small code change.
