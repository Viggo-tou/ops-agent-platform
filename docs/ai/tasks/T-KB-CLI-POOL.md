# T-KB-CLI-POOL — Pre-spawned warm `claude` CLI process pool

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium-high -->
<!-- Executor: codex -->

**Status:** todo (P2)
**Priority:** P2 (real runtime improvement; lower-priority because current 71min baseline is acceptable for now and CC mode already works)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.

## Goal

Replace per-tool-call cold-start of `npx @anthropic-ai/claude-code` (currently ~2-5s overhead per CC tool call) with a small pool of warm, reusable `claude` CLI processes. Each `cc_glob` / `cc_grep` / `cc_read` reuses an existing process instead of spawning fresh. Target: cut CC agent total time from ~30s (8 tool calls × 3s cold start) down to ~10-12s actual claude work.

## Background

QA baseline 2026-04-28: per-question wall-clock avg 111.6s. Breakdown:
- CC agent: ~30s (8 tool calls × ~3.5s each)
- Synthesis: ~60s
- Other overhead: ~20s

Of CC agent's 30s, **~16s** is just process startup (npx loading + claude-code package init + auth probe). Actual `Glob` / `Grep` / `Read` work is fast once the process is up.

If we keep `claude` CLI processes warm (pre-spawned at backend startup, reused per request), CC agent time drops to ~10-12s. Wall-clock 111s → ~85s. With max_rounds=3 budget, more questions complete confidently.

This is the largest "easy win" runtime optimization remaining after Phase AF.

### Risks compared to alternatives

- T-KB-EVIDENCE-TIER-CAP (sibling P1 ticket): high ROI, low risk, ~20 lines. Do that first.
- T-KB-HYBRID-RAG-FAST-PATH (sibling ticket): bigger architectural pivot, also reduces runtime but changes correctness boundary
- THIS ticket: pure infrastructure, no semantic change, but **medium-high engineering** (pool + lifecycle + health checks + concurrency). Easy to over-engineer.

## Design

### A. Investigate first: does `claude` CLI support reuse?

This card depends on a key assumption that needs to be verified BEFORE committing to the design:

> Does `npx @anthropic-ai/claude-code` support running multiple Glob/Grep/Read calls within a single process invocation, or does each Bash invocation expect cold start?

Codex first task: **5-minute investigation**. Try running `claude` once, send multiple commands via stdin or interactive mode, see if it works. If yes, design as below. If no, this ticket downgrades to "P3 / not viable" and we instead work on T-KB-HYBRID-RAG-FAST-PATH.

### B. (If feasible) Pool design

```python
# apps/backend/app/services/cc_cli_pool.py

@dataclass
class CCWorker:
    process: subprocess.Popen
    pid: int
    started_at: float
    last_used_at: float
    requests_handled: int
    healthy: bool

class CCCliPool:
    def __init__(self, *, size: int = 4, max_age_s: float = 300.0, max_requests: int = 50):
        self._pool: queue.Queue[CCWorker] = queue.Queue(maxsize=size)
        self._size = size
        self._max_age_s = max_age_s
        self._max_requests = max_requests
        self._lock = threading.Lock()

    def acquire(self, timeout_s: float = 10.0) -> CCWorker: ...
    def release(self, worker: CCWorker, *, healthy: bool = True) -> None: ...
    def execute(self, command: str, *, timeout_s: float, cwd: Path) -> CCToolResult:
        worker = self.acquire()
        try:
            return self._send_command(worker, command, timeout_s=timeout_s, cwd=cwd)
        finally:
            self.release(worker, healthy=...)
    def shutdown(self) -> None: ...
```

### C. Worker lifecycle

- **Startup**: backend `lifespan` hook spawns N (= `cc_cli_pool_size`, default 4) workers
- **Health check**: send a no-op (e.g. `Read .claude/CLAUDE.md`) at startup; reject worker if errors
- **Recycling**: after `max_requests` (default 50) or `max_age_s` (default 5 min), kill + replace
- **On shutdown**: backend `lifespan` exit hook gracefully terminates all workers
- **Concurrency**: each worker handles one request at a time. Pool size = max parallel CC tool calls

### D. Wire into existing `cc_agent.py`

Replace:
```python
result = subprocess.run(["npx", ...], cwd=cwd, timeout=timeout_s, ...)
```
With:
```python
result = self.pool.execute(command_args, cwd=cwd, timeout_s=timeout_s)
```

If pool not initialized (e.g. tests, isolated runs) → fall back to current per-call subprocess.run.

### E. Configuration

```python
cc_cli_pool_enabled: bool = True
cc_cli_pool_size: int = 4
cc_cli_pool_max_age_seconds: float = 300.0
cc_cli_pool_max_requests_per_worker: int = 50
cc_cli_pool_acquire_timeout_seconds: float = 10.0
```

## Files to create

1. `apps/backend/app/services/cc_cli_pool.py` — Pool + Worker
2. `apps/backend/tests/services/test_cc_cli_pool.py` — pool lifecycle tests (mocked subprocess)

## Files to edit

1. `apps/backend/app/services/cc_agent.py` — use pool when available
2. `apps/backend/app/main.py` (or wherever `lifespan` lives) — pool startup/shutdown hooks
3. `apps/backend/app/core/config.py` — 5 new settings

## Tests

### Investigation gate

0. (Manual / first thing in spec workflow) Verify `claude` CLI supports multi-request reuse. **If not**, abort this ticket.

### Unit tests (test_cc_cli_pool.py)

1. `test_pool_starts_and_acquires_worker` — pool of size 2; acquire returns valid worker
2. `test_pool_releases_worker_back_to_queue` — acquire + release; subsequent acquire returns same worker
3. `test_pool_blocks_when_all_workers_in_use` — pool size 1; acquire twice blocks the second
4. `test_pool_acquire_timeout_raises_when_starved` — pool size 1; acquire while held; second acquire times out
5. `test_worker_recycled_after_max_requests` — worker.requests_handled exceeds threshold; pool replaces
6. `test_worker_recycled_after_max_age` — worker.started_at older than threshold; pool replaces
7. `test_unhealthy_worker_rejected` — release with healthy=False; next acquire returns a different (or new) worker
8. `test_pool_shutdown_terminates_all_workers` — shutdown kills all subprocess
9. `test_pool_disabled_fallback_to_subprocess_run` — `cc_cli_pool_enabled=False`; `cc_agent.cc_glob` falls through to old code path
10. `test_first_warm_request_is_faster_than_cold` — sanity benchmark: time first request (cold) vs second request (warm); warm should be at least 2x faster (mocked subprocess startup latency)

### Integration

11. `test_cc_agent_glob_uses_pool_when_enabled` — pool initialized; cc_glob() routes through pool.execute()

## Acceptance criteria

- 11 new tests pass + investigation gate cleared
- Re-run benchmark with pool enabled:
  - Wall-clock ≤ 50 min (vs 71 min baseline)
  - Mean ≥ 49.65 (no regression)
  - Per-Q avg ≤ 90s (vs 111.6 baseline)
- Backend startup logs `cc_cli_pool initialized: size=4 workers=4 ready`
- Backend shutdown logs all workers terminated cleanly

## Out of scope

- Cross-process (multi-uvicorn-worker) pool sharing — single-process only for v1
- Memory limits per worker — defer until measured
- Auto-scaling pool size based on load — defer
- Fallback path when claude CLI version updates — manual recycle for now

## Risks

| Risk | Mitigation |
|---|---|
| `claude` CLI doesn't support stdin reuse | Investigation gate; if true, downgrade ticket |
| State leakage across requests (cached conversation history) | Send explicit reset between requests OR start fresh worker |
| Worker zombie processes if backend crashes | OS-level cleanup; document in ops runbook |
| Pool size 4 too small under load | Tune via config; add metric `cc_cli_pool_acquire_wait_ms` |

## Workflow

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-KB-CLI-POOL.md
```

Worktree: branch `feat/kb-cli-pool` off `checkpoint/pre-reclassify` (after T-MERGE-CC-AGENTIC-INTO-MAIN). Investigation gate first.
