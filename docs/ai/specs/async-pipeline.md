# Spec: Async Pipeline Execution (T-ASYNC)

## Goal

`POST /api/tasks` must return `201 TaskDetail` within **<1 second** and let the
full develop/plan pipeline run in the background. Today the handler blocks for
4+ minutes because `TaskService.create_task` calls
`orchestrator.bootstrap_task(...)` synchronously before returning. The frontend
spinner hangs the entire time.

Constraint from `CLAUDE.md` — **single runtime only**. No Celery, no Redis, no
separate worker process. Use a process-internal `ThreadPoolExecutor`.

## Files to touch

1. `apps/backend/app/core/config.py`
   - Add `pipeline_max_workers: int = 2` on the `Settings` class (respect
     `OPS_AGENT_PIPELINE_MAX_WORKERS` env var via the existing env_prefix).

2. `apps/backend/app/core/pipeline_executor.py` *(new file)*
   - Module-level `ThreadPoolExecutor` wrapper with these exports:
     - `init_pipeline_executor(max_workers: int) -> None` — create the
       executor; idempotent; must not recreate if already initialised.
     - `shutdown_pipeline_executor(wait: bool = True) -> None` — shut it down;
       also idempotent.
     - `submit_pipeline_job(fn: Callable[..., Any], *args, **kwargs) -> Future`
       — raises `RuntimeError("Pipeline executor not initialised")` if called
       before init.
     - `get_pipeline_executor() -> ThreadPoolExecutor | None` — used by tests.
   - Include a **test override hook**: `set_pipeline_executor_override(executor
     | None)`. When set, `submit_pipeline_job` delegates to it instead. This
     lets tests install a synchronous `_ImmediateExecutor` and keep
     determinism. The override takes precedence over the real executor.
   - `_ImmediateExecutor` is *not* defined here — it lives in the test helpers.
     Just make the override hook accept any object with a `.submit(fn, *a,
     **kw) -> Future`-ish interface.

3. `apps/backend/app/main.py`
   - In `lifespan`, call `init_pipeline_executor(settings.pipeline_max_workers)`
     after `bootstrap_model_catalog`, before `yield`.
   - After `yield`, call `shutdown_pipeline_executor(wait=True)`.

4. `apps/backend/app/services/tasks.py`
   - Split `create_task(payload)`:
     - **Synchronous portion (kept)**: build the `Task` row, record
       `TASK_CREATED` + `USER_REQUEST_RECEIVED` events, `flush`, `commit`,
       **then** submit the background job.
     - **Remove** the `self.orchestrator.bootstrap_task(task, ...)` call from
       `create_task`. It moves to the background job.
   - Add new module-level function (not a `TaskService` method — must not
     depend on the request-scoped db session):

     ```python
     def run_pipeline_job(task_id: str, actor_name: str) -> None:
         """Runs inside the thread pool. Owns its own SessionLocal."""
     ```

     Behaviour:
     1. Open `db = SessionLocal()`.
     2. `task = db.get(Task, task_id)`; if `None`, log warning + return.
     3. Wrap in `try/except Exception as exc:`:
        - Instantiate `PrimaryOrchestrator(db)`.
        - Call `orchestrator.bootstrap_task(task, actor_name=actor_name)`.
        - `db.commit()`.
     4. On exception:
        - `db.rollback()`
        - Re-open a fresh session (the previous one may be in a bad state):
          `db2 = SessionLocal()`; reload the task; call
          `set_task_status(db2, task=task, new_status=TaskStatus.FAILED,
          new_stage=WorkflowStage.DONE, role=RoleName.SYSTEM,
          message=f"Pipeline crashed: {type(exc).__name__}: {exc}",
          payload={"error_type": type(exc).__name__, "error": str(exc)})`;
          also `record_event(db2, ..., event_type=EventType.EXECUTION_FAILED,
          ..., message="Background pipeline job raised an unhandled
          exception.", payload={...})`; `db2.commit()`; `db2.close()`.
        - Use `logging.getLogger("app.services.tasks").exception(...)` so the
          traceback reaches stdout — must NOT be swallowed.
     5. Finally: `db.close()`.

   - At the end of the existing `create_task`, after `self.db.commit()`,
     **before** the `return`, call
     `submit_pipeline_job(run_pipeline_job, task.id, payload.actor_name)`.
     Import from `app.core.pipeline_executor`.

5. `apps/backend/app/api/tasks.py`
   - No functional change required — `create_task` handler already just calls
     `service.create_task(payload)`. But **do add a docstring comment** noting
     that the returned `TaskDetail` will have `status=CREATED` and the client
     should poll `GET /tasks/{id}` for progress.

## Tests

### Test infrastructure

6. `apps/backend/tests/conftest.py` (or closest existing conftest — check
   first; do not duplicate a fixture that already exists)
   - Add an autouse fixture that installs a synchronous executor override for
     the whole test session:

     ```python
     class _ImmediateExecutor:
         def submit(self, fn, *args, **kwargs):
             fut = Future()
             try:
                 fut.set_result(fn(*args, **kwargs))
             except BaseException as exc:
                 fut.set_exception(exc)
             return fut
         def shutdown(self, wait=True): pass
     ```

   - Fixture:

     ```python
     @pytest.fixture(autouse=True)
     def _sync_pipeline_executor():
         from app.core.pipeline_executor import set_pipeline_executor_override
         set_pipeline_executor_override(_ImmediateExecutor())
         yield
         set_pipeline_executor_override(None)
     ```

   - This means all existing tests that assume `create_task` runs the pipeline
     synchronously **keep working unchanged** — the pipeline still runs inside
     `create_task` under this override.

### New tests

7. `apps/backend/tests/services/test_async_pipeline.py` *(new)*
   - Test 1: **override makes it synchronous** — with the autouse fixture,
     calling `TaskService.create_task` leaves the returned task with
     `AWAITING_APPROVAL` or `FAILED` or `COMPLETED` (whatever the mock
     orchestrator produces), proving the job ran.
   - Test 2: **without override, the handler returns before pipeline runs** —
     temporarily override with an executor that captures the submitted
     callable but **does not run it**. Assert the returned task has
     `status=CREATED` and `workflow_stage=INTAKE`. Then call the captured
     callable manually and assert status advances.
   - Test 3: **pipeline exception path** — override with an executor that runs
     synchronously. Monkey-patch `PrimaryOrchestrator.bootstrap_task` to raise
     `RuntimeError("boom")`. Call `create_task`. Re-fetch task from DB;
     assert `status=FAILED`, `workflow_stage=DONE`, and an `EXECUTION_FAILED`
     event exists with the error message in its payload.

### Guard existing behaviour

8. Do **not** delete any existing test. The autouse sync override fixture
   should keep `test_create_task`, integration tests, etc. green.

## Non-requirements (do NOT implement)

- Do NOT add task recovery on service restart (deferred — user's explicit
  decision).
- Do NOT add SSE, WebSocket, or any new progress endpoint. Clients keep
  polling `GET /tasks/{id}`.
- Do NOT touch any frontend code.
- Do NOT add retry/backoff inside the pipeline job. A single top-level
  try/except is enough.
- Do NOT introduce `asyncio` into the orchestrator. Stay in sync Python inside
  the worker thread.

## Acceptance criteria

- `POST /api/tasks` returns within 1 second in manual/Playwright testing.
  (Manual verification by user after merge — not a pytest assertion.)
- All previously passing tests still pass with the autouse sync fixture in
  place (`pytest apps/backend`).
- The 3 new tests in `test_async_pipeline.py` pass.
- `from app.core.pipeline_executor import submit_pipeline_job` works and is
  the only way `create_task` reaches the orchestrator.
- On service shutdown, the executor drains in-flight jobs (verified by
  `shutdown(wait=True)` in lifespan — no test required).

## Out of scope for this task

Frontend changes, GateStatusPanel, DiffViewer highlighting, git push/PR, E2E
fixture expansion — all deferred to subsequent tasks.
