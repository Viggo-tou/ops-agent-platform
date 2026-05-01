# T-DOGFOOD-OBSERVABILITY-BUGS-2026-05-01 — Bugs surfaced by first UX dogfood session

<!-- Effort: small-medium per bug -->
<!-- Executor: codex (per-bug) -->

**Status:** todo (P1 — observability gaps that mask real failures)
**Priority:** P1 (UX dogfood blocked by these — every retry hits the same wall)
**Created:** 2026-05-01
**Linked:** SESSION_HANDOFF 2026-05-01-1705 (UX dogfood section)

## Background

First UX dogfood attempt of the platform (post Stage 20A V1 / V2). User submitted "完成Jira的P69-4" through the chat UI. The task was created at 10:59:05 UTC and **sat at `status=created / stage=intake` for 45+ minutes with no UI feedback** beyond "正在生成". When investigated:

- Pipeline `last_successful_task_at` was 10:14:47 UTC — i.e. NO task had completed in the 45 minutes BEFORE the user's submission either, including the test we ran during Phase 1 v2. The pipeline executor (ThreadPoolExecutor, `pipeline_max_workers=2`) was deadlocked on something earlier (probably an unbounded MiniMax or Jira API call from a previous run).
- Frontend showed "正在生成" indefinitely with no error, no timeout, no progress signal.
- Backend logs were not captured because `Start-Process -WindowStyle Hidden` discarded stdout. Required killing + restarting backend with explicit redirect to a file before any error was even visible.

After full backend restart, the orphan-sweep correctly marked the user's task `failed` ("Task orphaned by backend restart while in status=created, stage=intake. Pipeline executor thread no longer exists; marking as failed."). But by then the user had already lost 45 minutes to a silent failure mode.

## Three concrete bugs

### Bug 1: No timeout / progress signal in UI when task hangs at intake

**Symptoms**:
- User submits a task
- UI shows "正在生成" or equivalent
- Backend pipeline is hung on an external API call (MiniMax / Jira / etc) OR pipeline executor pool is jammed
- UI never updates, never errors, never offers a "this is taking too long" affordance
- User has no feedback signal except wall-clock time

**Repro**:
1. Trigger a state where `pipeline_max_workers=2` is fully occupied by hung tasks
2. Submit a new task from the UI
3. UI stays "正在生成" forever; backend status stays `created/intake`

**Fix scope**:
1. **Per-task soft timeout** (e.g. 600s for `process_question`, 1800s for `jira_issue_develop`). Frontend polls task status; when wall-time exceeds the soft timeout AND no stage advance, show "task slow / consider canceling" affordance.
2. **Per-task hard timeout** (configurable per scenario; default 1800s `process_question`, 3600s `jira_issue_develop`). Backend marks task `failed` with reason `pipeline_timeout` and frees the worker thread.
3. **Stage-level visibility in UI**: show user current stage (intake / planning / codegen / review / approval) with timestamps. They can see WHERE a task is hanging.

**Out of scope**: queue-position display ("you are #3 in line"). Useful but bigger work.

### Bug 2: No automatic recovery from worker-pool stall

**Symptoms**:
- Pipeline executor (ThreadPoolExecutor, max_workers=2) gets all worker threads stuck on hung external calls
- Subsequent task submissions queue up but never process
- No watchdog detects this; no alerts; no automatic worker reset

**Repro**:
- See Bug 1's repro path
- Wait 30+ minutes; observe pipeline_max_workers=2 worker threads are all hung on MiniMax/Jira/whatever
- New tasks pile up forever

**Fix scope**:
1. **Per-stage circuit breaker**: each external API call (MM, Jira, Anthropic, Codex, Claude Code CLI) wrapped in a timeout (e.g. 60s for MM, 30s for Jira API, 240s for synthesis). Timeout raises an exception, the pipeline catches it and marks the task `failed`. Worker thread freed.
2. **Pipeline-level watchdog**: every 5 min, scan tasks where `status in {created, running}` AND `last_event_age > 600s`. Either ping the task to confirm liveness, or mark `pipeline_stalled` with reason. Frees worker threads + alerts.
3. **Health endpoint should expose worker-pool status**: `pipeline_workers_active`, `pipeline_workers_total`, `pipeline_queue_depth`. Currently `/health` shows `last_successful_task_at` but not whether the executor pool is healthy.

### Bug 3: Backend stdout/stderr not captured by start-backend.ps1

**Symptoms**:
- Standard `start-backend.ps1` invocation via `Start-Process -WindowStyle Hidden` discards uvicorn stdout AND application logs
- When pipeline hangs / errors, there is NO record visible without killing backend and restarting with manual redirect (`> backend.log 2>&1`)
- Debugging requires reproducing the hang AFTER backend restart, which often resolves the hang and erases the evidence

**Repro**:
- `powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1` from a hidden / detached process
- Trigger any error
- Observe: no log file, no captured output, log only visible in the (now-gone) terminal

**Fix scope**:
1. `start-backend.ps1` should accept a `-LogFile` parameter (default `D:/项目/Ops_agent_platform/backend.log`) and redirect uvicorn stdout/stderr to it, with rotation (overwrite or append).
2. The redirect should happen INSIDE the script, not in the calling process — that way `Start-Process -WindowStyle Hidden` works correctly.
3. The current backend already uses structured JSON logging (we saw `"event": "lifecycle_event"` and `"component": "http"` in the log capture I did manually). The fix is just routing those to disk reliably.
4. Bonus: `start-backend.ps1 -StreamLogs` flag that tails the log to current console for interactive debugging.

## Acceptance (per bug)

### Bug 1 acceptance

- New task with simulated 600s+ no-progress shows "task slow" affordance in UI within 60s of timeout breach
- Task with hard timeout reaches `status=failed reason=pipeline_timeout` automatically
- Per-stage indicator visible in UI for any in-flight task

### Bug 2 acceptance

- External API timeout (60s MM, 30s Jira) test triggers task `failed` with `external_api_timeout` reason
- Watchdog test: simulated stalled task gets caught within 5 min
- `/health` endpoint surfaces `pipeline_workers_active` and `pipeline_queue_depth` numeric fields

### Bug 3 acceptance

- `powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1` produces a populated log file even when launched via `Start-Process -WindowStyle Hidden`
- Log rotates (or overwrites with timestamp) on each restart
- `-StreamLogs` flag works for interactive debugging

## Out of scope

- Queue-position display in UI (informational, bigger work)
- Multi-tenant log routing (per-actor logs) — current single-user is fine
- Rate-limit-aware retry logic on external APIs — that's a separate quality ticket
- Frontend redesign / confidence labels — separate UX work

## Why P1

These three bugs make every UX dogfood session unrecoverable from the user's perspective:
- Bug 1: user can't tell if system is broken vs slow
- Bug 2: once the pool jams, every subsequent task is silently lost
- Bug 3: when devs try to investigate, the evidence has already been discarded

Without fixing these, dogfood produces "the agent doesn't work" verdicts that mask real underlying causes (slow MM call, Jira auth issue, etc). Quality + UX work both depend on visibility into what the pipeline is actually doing.

## Workflow

**Per-bug dispatch** (each is a separate codex round, ~1-2 days):

```bash
# Bug 1: UI timeout + stage visibility
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/dogfood-bug-1-timeouts" \
  -c model_reasoning_effort=xhigh \
  - < <task-1-spec.md>

# Bug 2: circuit breaker + watchdog + health
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/dogfood-bug-2-recovery" \
  -c model_reasoning_effort=xhigh \
  - < <task-2-spec.md>

# Bug 3: log capture (smallest, least risky — could be direct-applied)
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/dogfood-bug-3-logs" \
  -c model_reasoning_effort=medium \
  - < <task-3-spec.md>
```

Recommend Bug 3 first (smallest, unblocks future debugging), then Bug 2 (the cause of Bug 1's symptoms), then Bug 1 (UI work that benefits from Bug 2's instrumentation).

## Bug 4 (added later in same session): Pipeline hangs deterministically on jira_issue_* scenarios + POST /api/tasks blocks

**Discovered**: After fixing the original P69-4 hang by full backend restart, ran a controlled experiment via Playwright:

1. Restart backend cleanly with stdout captured.
2. Submit a fresh `process_question` task (English, ASCII): completes in ~5 min ✓.
3. Submit P69-4 via Playwright UI ("完成Jira的P69-4" → `jira_issue_develop`): hangs at `created/intake` for 200+ seconds, no `semantic_translation_started` event fires. Pipeline worker never picks it up.
4. Submit `jira_issue_plan` ("Plan Jira P69-4 for implementation"): pipeline DOES fire `semantic_translation_started`, but stops there. No further events.
5. Submit a SECOND task immediately after: **POST /api/tasks hangs** (curl returns empty body, even though /health shows backend alive).

The third symptom is the most concerning. The CREATE endpoint blocks. Backend serves GET requests fine (/health, /api/tasks list, /api/tasks/{id}, /api/tasks/{id}/events all return 200 quickly). Only POST /api/tasks blocks.

Likely root causes (need investigation):

- **DB lock contention**: `TaskService.create_task` acquires a write lock when committing the new task; if a pipeline worker is also inside `db.commit()` for the same SQLite connection, SQLite's per-database lock blocks the API request.
- **Pipeline worker deadlock**: ThreadPoolExecutor (`pipeline_max_workers=2`) workers may be stuck inside `bootstrap_task` on a synchronization primitive, an unbounded subprocess, or a network call without timeout. The exact step is unknown because no error log surfaces — the workers just don't emit further events.
- **Orchestrator reentry pattern**: `bootstrap_task` for jira_issue_* scenarios calls `_prefetch_jira_issue_context` (Jira API, verified working in 0.81s) and `_translate_request` (MM call, untimed). The MM call has no timeout and may block on rate-limit or network. With `max_workers=2`, 2 such hangs jam the pool.

**Repro recipe** (deterministic):

```
1. Fresh backend restart
2. Submit process_question — completes ✓
3. Submit jira_issue_develop — hangs forever
4. Submit any 4th task — POST hangs
5. /health still says "healthy"
6. Backend log shows no errors, just HTTP request log spam
```

**Severity**: critical for UX dogfood. Every Jira-related dogfood attempt blocks indefinitely with no error to the user. The `last_successful_task_at` field in /health misleads the user into thinking the system is operating normally.

**Fix scope (preliminary, needs investigation)**:

1. **Wrap every external API call in a timeout** in `bootstrap_task` and downstream methods. Currently `_prefetch_jira_issue_context` uses httpx default timeout (might be unbounded), MM calls are unbounded. Hard 30-60s per call.
2. **Audit `bootstrap_task` for any unbounded synchronization** (Lock acquisitions without timeout, subprocess.run without timeout, etc.). Add timeouts everywhere.
3. **Investigate POST /api/tasks blocking**: likely DB lock contention with worker. SQLite isolation level / connection pooling needs review. Possibly switch SQLite to WAL mode or use separate readonly connection for API reads.
4. **Add per-stage event emission**: orchestrator should emit lifecycle events at every API call boundary (`jira_fetch_started`, `jira_fetch_succeeded`, `mm_synthesis_started`, etc.) — currently we have `semantic_translation_started` but nothing between that and the next high-level milestone. When a worker hangs, we have no idea what step it's hung on.
5. **`/health` must include true pipeline-pool status**: not just `last_successful_task_at` (a DB read) but actual `pipeline_workers_active`, `pipeline_workers_idle`, `pipeline_recent_event_within_60s` boolean. A jammed pool should make `/health` return `degraded`, not `healthy`.

This expands the original Bug 2 scope significantly. Bug 4 makes Bug 2 P0-urgent rather than P1.

**Verification needed**: backend pipeline reliability is currently unsuitable for any UX dogfood. Until Bug 4 is fixed, additional dogfood is not worth doing — every session will hit this same wall.
