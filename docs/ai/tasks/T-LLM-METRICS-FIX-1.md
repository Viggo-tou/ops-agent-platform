# T-LLM-METRICS-FIX-1 — Two-bug patch on top of T-LLM-METRICS

<!-- Effort: low -->
<!-- Executor: codex -->

## Goal

Patch the just-landed T-LLM-METRICS implementation so it doesn't break the calling path on existing DBs.

## Two bugs found in smoke test

### Bug 1: missing schema migration for `event.task_id`

The model in `apps/backend/app/models/event.py` was changed:
```python
task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), index=True, nullable=True)
```

But existing SQLite DBs still have the original NOT NULL constraint. When `record_llm_call` writes an LLM_CALL event with `task_id=None` (which happens for any LLM call outside a task — e.g. the knowledge search HTTP endpoint or `build_cards.py`), SQLite raises `IntegrityError: NOT NULL constraint failed: event.task_id`.

**Fix:** add an idempotent migration in `apps/backend/app/core/db.py` (similar to existing ALTER TABLE patterns at lines 164-220) that rebuilds the event table to allow nullable task_id when the existing column is NOT NULL. Pattern for SQLite (no DROP NOT NULL):

```python
# In db.py init step
existing = inspector.get_columns("event")
task_id_col = next((c for c in existing if c["name"] == "task_id"), None)
if task_id_col and not task_id_col["nullable"]:
    # SQLite rebuild pattern
    connection.execute(text("CREATE TABLE event_new AS SELECT * FROM event"))
    connection.execute(text("DROP TABLE event"))
    # Recreate from SQLAlchemy metadata (now with nullable=True)
    Base.metadata.tables["event"].create(bind=connection, checkfirst=True)
    connection.execute(text("INSERT INTO event SELECT * FROM event_new"))
    connection.execute(text("DROP TABLE event_new"))
```

(Adjust the rebuild pattern for your specific column / index needs — this is illustrative.)

### Bug 2: telemetry session-poisoning breaks calling path

Even with the visible-error counter pattern, when `record_llm_call`'s flush raises an IntegrityError, the **shared SQLAlchemy session** enters PendingRollbackError state, which causes the request's parent operation (knowledge search) to also fail with a 500.

This defeats the design goal "telemetry never breaks the calling path".

**Fix:** isolate the telemetry write so its failure cannot poison the calling session. Two options, pick one:

**Option A (preferred):** Use `db.begin_nested()` (savepoint) around the telemetry write inside `record_llm_call`. If the savepoint write fails, only the savepoint is rolled back; the parent transaction continues.

```python
def record_llm_call(db: Session, call: LlmCall) -> None:
    # ... build payload ...
    try:
        with db.begin_nested():  # savepoint
            db.add(Event(task_id=call.task_id, ...))
            db.flush()
    except Exception as exc:
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(...)
```

**Option B:** Use a separate Session bound to the same engine for telemetry writes. Heavier; only needed if savepoints don't work for some backend reason.

## Files to edit

1. `apps/backend/app/core/db.py` — add the event.task_id migration (idempotent on already-migrated DBs)
2. `apps/backend/app/services/llm_telemetry.py` — wrap LlmUsage and Event writes in nested savepoints
3. `apps/backend/tests/services/test_llm_telemetry.py` — add tests:
   - `test_telemetry_failure_does_not_break_caller_session` — simulate IntegrityError during flush; assert parent session can still commit subsequent writes
   - `test_event_task_id_migration_idempotent` — run init twice, no error second time, schema correct

## Acceptance

- `python -m compileall app` clean
- All existing telemetry tests still pass (10/10)
- 2 new tests pass
- Manual smoke from Tomonkyo bash:
  - Stop backend on 8004 if running
  - Start backend from `D:/项目/ops-worktrees/llm-metrics/apps/backend` on 8004 (it already has a copied DB with old NOT NULL constraint — migration must run on startup)
  - `curl 'http://127.0.0.1:8004/api/knowledge/search?query=Where+is+Firebase+configured&source_name=hosteddashboard' -H 'X-Actor-Name: probe'` returns 200, NOT 500
  - `curl 'http://127.0.0.1:8004/api/metrics/llm-calls?since_minutes=60'` returns events grouped by purpose (synthesis at minimum), telemetry_failure_count remains 0

## Why this is a P1 fix

Without this patch, T-LLM-METRICS as landed actually breaks the search endpoint when used against any DB that pre-dates the model change. That includes the production DB if anyone copies it. Defeats the spec's "telemetry must not break caller" guarantee.

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/llm-metrics" -c model_reasoning_effort=low - < docs/ai/tasks/T-LLM-METRICS-FIX-1.md
```

Worktree: same `D:/项目/ops-worktrees/llm-metrics` on `feat/llm-metrics`. Patches the just-landed implementation; commit on top.
