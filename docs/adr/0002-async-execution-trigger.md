# ADR 0002: Async Execution Trigger and Migration Path

- Status: Accepted
- Date: 2026-04-14
- Supersedes: n/a
- Related: T-031, CLAUDE.md "Development Constraints", `docs/phase-5-7-enterprise-roadmap.md`

## Context

Every request to `POST /api/tasks` today runs `PrimaryOrchestrator.bootstrap_task` inline inside the FastAPI handler (see `apps/backend/app/services/tasks.py:32` → `apps/backend/app/orchestrator/service.py:113`). Planning, codegen, Jira writeback, completeness check, and sandbox runs all execute on the request thread. A completed develop-scenario task currently takes ~10–60 s under MiniMax; pathological cases (large grounding files, slow Jira) push this to 120 s+. This is at the edge of what web clients and reverse proxies tolerate, but it has three upsides that have so far outweighed the latency cost:

1. **Audit coherence.** Every lifecycle event is written by the same DB session that serves the request, so governance rows (policy decisions, tool approvals, review verdicts) are observed by the caller in one consistent transaction boundary.
2. **Failure transparency.** Exceptions propagate straight to the client; we don't need separate failure surfaces for "queued but crashed later."
3. **Debuggability.** One process, one stack trace, no worker-side logs to correlate.

`CLAUDE.md` explicitly forbids splitting into async workers "before the roadmap calls for it." This ADR decides **when** that call arrives and **how** to migrate without losing the three upsides above.

## Decision

We stay on the single-runtime path **until all four trigger conditions below hold simultaneously**. Triggers are hard gates, not recommendations — any single missing condition means we do not migrate. When all four hold, we migrate along the "Option B" path in §Consequences.

### Trigger conditions (ALL must hold)

1. **Latency floor crossed.** p95 of `POST /api/tasks` → 201 response exceeds **60 s** over a 7-day window of real (non-test) traffic. Measure via existing OTel spans (`apps/backend/app/core/telemetry.py`).

2. **Governance stability.** Every write path exercised by an async worker has a backend-enforced policy check **and** an audit row (`lifecycle_events` + `tool_approvals`). No "the UI will confirm first" gaps remain. Today the status is mostly-there (T-034 landed the last jira_issue_develop gap); this trigger fires when the T-030 approval queue polish closes the remaining UX-only checks.

3. **Frontend async UX ready.** The chat surface can represent a task in `queued` / `running` / `needs_approval` / `completed` / `failed` states without degrading the current synchronous-answer feel. No half-finished "your task is processing…" placeholder that users see as a regression. Progress: T-Q1 streaming UX lands this capability on the client.

4. **Multi-tenant demand.** At least one deployment needs to run concurrent tasks from different tenants / roles such that a single in-process orchestrator becomes a scheduling bottleneck. Today there is exactly one concurrent-user deployment; this trigger is the furthest from firing.

Until all four hold, the default answer to "should we queue this?" is **no**.

### Non-triggers (explicitly insufficient on their own)

- "This task sometimes exceeds 30 s." — Fix the specific slow path (prompt size, timeout tuning, grounding selection). Queueing does not make the task faster; it only moves the wait elsewhere.
- "We want to be prepared for scale." — Premature. Every layer added before real load data is tech debt with interest.
- "Other agent frameworks are async." — Architectural mimicry. Our single-runtime choice is load-bearing for audit coherence.

## Options considered

### Option A — Stay single-runtime (current; recommended default)

**Keep** `PrimaryOrchestrator.bootstrap_task` synchronous in the request handler. Optimizations that do **not** violate this mode:

- Per-provider timeouts (already done: MiniMax/Anthropic request-level timeouts).
- Streaming provider responses back to the client over SSE — the request stays open but bytes flow.
- Pre-computed knowledge indices, cached grounding files.
- `BackgroundTasks` for strictly fire-and-forget side effects (metrics flush, non-critical log upload) where failure **never** affects audit correctness.

**Not allowed** under Option A: queueing a task whose outcome the user will poll for, moving planner/codegen off the request thread, or introducing any separate worker process.

### Option B — In-process asyncio task pool

`asyncio.create_task(bootstrap_task(...))` inside the handler, returning 202 Accepted with the task id, and a new `GET /api/tasks/{id}/stream` SSE endpoint for lifecycle events. No new process, no queue broker.

Pros: single deploy unit, shared DB connection pool, single log stream. Governance path unchanged — events still written by the same process.

Cons: if the FastAPI process dies mid-task, the task is lost (no persisted queue). A restart-safe persisted queue (state machine on the `tasks` row + cron-resume) is needed before this is acceptable in production.

**This is the migration target when triggers fire.** It is the smallest architectural step that unlocks concurrency without taking on Celery/RQ operational surface.

### Option C — Celery / RQ / Arq worker process

Separate worker process, Redis or RabbitMQ broker, explicit queue names.

Pros: standard, horizontally scalable, mature operational tooling.

Cons:
- Doubles the deploy surface (web + worker + broker).
- Governance/audit rows now cross two processes; requires an idempotency key on every lifecycle write to avoid double-counting on retry.
- Debugging loses the single-stack-trace property — mandatory distributed tracing.
- Secrets (provider API keys, Jira tokens) must be replicated to the worker.

**Not considered until Option B hits its own ceiling.** Adding broker complexity while we still have one-process-is-enough capacity is premature.

### Option D — Serverless per-task invocation

Rejected for this codebase. Cold start variance + provider SDK warmup would dominate task latency, and the governance audit trail depends on a long-running process holding session state.

## Migration path (when triggers fire)

Three PRs, each independently mergeable, each behind a feature flag `ASYNC_EXECUTION_ENABLED=false` until the one before it is stable:

1. **PR-1: Persisted task state machine.** Add `tasks.execution_state` column with values `pending | running | needs_approval | completed | failed | abandoned`. Every orchestrator checkpoint writes this column. Tests cover crash-recovery: a process killed mid-`running` leaves a row the next process can resume from.

2. **PR-2: Async handler + SSE events endpoint.** Handler dispatches `asyncio.create_task`, returns 202. SSE endpoint streams `lifecycle_events` rows as they are written. Frontend gains `useTaskStream(taskId)` hook. Feature flag gates the 202 path; default behavior unchanged.

3. **PR-3: Restart-resume cron.** On app startup, scan for `tasks.execution_state == 'running'` older than 5 minutes and either resume or mark `abandoned` with an audit event. No broker; the DB is the queue.

Option C is a **future** migration if Option B still can't keep up, and each PR in that migration requires its own ADR.

## Consequences

**If we stay on Option A** (the current default): request timeouts will eventually become a user-visible issue as task complexity grows. Mitigation is targeted per-path — streaming responses, prompt budget, caching — not architectural.

**If we migrate to Option B** (when triggers fire): the three upsides from §Context partially degrade — failure surfaces now include "queued but the worker died." Mitigations (PR-1's state machine + PR-3's resume cron) preserve audit coherence at the cost of added schema and code surface. We accept this trade once and only once the trigger conditions justify it.

**Governance implications:** every async hop that crosses an "approval required" boundary MUST re-evaluate the policy at resume time, not at enqueue time. A task that was `needs_approval` when queued and auto-approved after 30 minutes must re-read the current policy row, because policy rules can be edited by admins while the task is paused. This is a hard requirement on PR-1's state machine design.

**Explicitly out of scope for this ADR:** multi-agent concurrency within a single task (parallel planner + codegen), worker autoscaling, priority queues, backpressure on provider rate limits. Each is its own decision after Option B is in production.

## References

- `CLAUDE.md` → "Development Constraints" (single-runtime mandate)
- `apps/backend/app/orchestrator/service.py:113` (`PrimaryOrchestrator`)
- `apps/backend/app/services/tasks.py:32` (synchronous bootstrap call site)
- `docs/phase-5-7-enterprise-roadmap.md`
- ADR 0001 (companion precedent for "binding-before-implementation" policy ADRs)
