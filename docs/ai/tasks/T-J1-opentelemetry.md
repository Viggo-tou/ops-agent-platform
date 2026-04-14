# T-J1 — OpenTelemetry Tracing

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: xhigh -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add OpenTelemetry tracing to the backend. Each task execution produces a root span with child spans for key orchestrator stages and tool calls. Store `trace_id` on the Task model for cross-referencing with external tracing backends.

## Background

Phase J of the multi-agent MVP roadmap. Depends on Phase I (structlog is in place). structlog tells you "what happened"; OTel traces show **causality and time distribution** — which stage is slow, where the bottleneck is.

Important constraint: the OTel SDK packages may not be installable in the codex sandbox (no network). Write the code so it **gracefully degrades** if OTel packages are not installed — the app must still start and work without them. Use a try/except import pattern.

## Design

### 1. Telemetry configuration

New file: `apps/backend/app/core/telemetry.py`

```python
from __future__ import annotations
import os

_tracer = None
_otel_available = False

def configure_telemetry() -> None:
    """Set up OTel tracing. No-op if packages are missing."""
    global _tracer, _otel_available
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        _otel_available = False
        return

    _otel_available = True
    resource = Resource.create({"service.name": "ops-agent-platform"})
    provider = TracerProvider(resource=resource)

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
        except ImportError:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("ops-agent-platform")

def get_tracer():
    """Return the configured tracer, or a no-op proxy."""
    global _tracer
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace
        return trace.get_tracer("ops-agent-platform")
    except ImportError:
        return _NoOpTracer()

def is_otel_available() -> bool:
    return _otel_available

def get_current_trace_id() -> str | None:
    """Return the current span's trace ID as a hex string, or None."""
    if not _otel_available:
        return None
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, '032x')
    except Exception:
        pass
    return None

class _NoOpTracer:
    """Fallback when OTel is not installed."""
    def start_as_current_span(self, name, **kwargs):
        return _NoOpContextManager()

class _NoOpContextManager:
    def __enter__(self): return _NoOpSpan()
    def __exit__(self, *args): pass

class _NoOpSpan:
    def set_attribute(self, key, value): pass
    def set_status(self, status): pass
    def record_exception(self, exc): pass
```

### 2. Task model — add trace_id

Add to `apps/backend/app/models/task.py`:

```python
trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

Add SQLite schema shim in `app/core/db.py` (same pattern as `inverse_action_json` migration).

### 3. Orchestrator spans

In `apps/backend/app/orchestrator/service.py`, wrap key methods with tracer spans:

- `bootstrap_task()` → root span `"task.bootstrap"` with attributes `task_id`, `scenario`, `actor_name`. Store `trace_id` on the task.
- `_translate_request()` → child span `"task.translate"`
- `generate_plan()` call → child span `"task.plan"`
- `review_plan()` call → child span `"task.review"`
- `_execute_plan()` → child span `"task.execute"`

Pattern:
```python
from app.core.telemetry import get_tracer, get_current_trace_id

tracer = get_tracer()

def bootstrap_task(self, task, *, actor_name):
    with tracer.start_as_current_span("task.bootstrap") as span:
        span.set_attribute("task.id", task.id)
        span.set_attribute("task.scenario", task.scenario)
        task.trace_id = get_current_trace_id()
        # ... existing code ...
```

### 4. Tool gateway spans

In `apps/backend/app/tools/gateway.py`, wrap `execute()` and `_execute_once()`:

```python
with tracer.start_as_current_span("tool.execute") as span:
    span.set_attribute("tool.name", tool_name)
    # ... existing retry loop ...
```

### 5. Wire into app startup

In `apps/backend/app/main.py`, call `configure_telemetry()` after `configure_logging()`.

### 6. Requirements

Add to `apps/backend/requirements.txt` (all optional — code works without them):
```
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
```

## Files to create

1. `apps/backend/app/core/telemetry.py`
2. `apps/backend/tests/services/test_telemetry.py`

## Files to edit

3. `apps/backend/app/models/task.py` — add `trace_id` column.
4. `apps/backend/app/core/db.py` — add schema shim for `trace_id`.
5. `apps/backend/app/orchestrator/service.py` — wrap key methods with spans.
6. `apps/backend/app/tools/gateway.py` — wrap execute with span.
7. `apps/backend/app/main.py` — call `configure_telemetry()`.
8. `apps/backend/requirements.txt` — add OTel packages.

## Tests

All in `apps/backend/tests/services/test_telemetry.py`. Use `unittest.TestCase`. Tests must pass **whether or not OTel packages are installed** (test the graceful degradation).

1. **`test_noop_tracer_works`** — Create `_NoOpTracer`, use `start_as_current_span`, call `set_attribute` on the span. Assert no exception.
2. **`test_get_tracer_returns_object`** — Call `get_tracer()`. Assert it has `start_as_current_span` method.
3. **`test_get_current_trace_id_without_span`** — Call `get_current_trace_id()` outside any span. Assert returns `None` or a valid hex string (depending on whether OTel is installed).
4. **`test_configure_telemetry_no_crash`** — Call `configure_telemetry()`. Assert no exception regardless of OTel availability.
5. **`test_is_otel_available`** — Call `is_otel_available()`. Assert returns `bool`.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 5 new tests pass (with or without OTel packages installed).
- Full suite still green.
- App starts without OTel packages (graceful degradation).
- If OTel packages are installed, `bootstrap_task()` produces a trace with nested spans.
- `Task.trace_id` is populated when OTel is active.

## Workflow (for the executor)

<!-- Effort: xhigh — context propagation + graceful degradation is complex -->

1. Read `app/core/logging.py` (Phase I pattern), `app/orchestrator/service.py`, `app/tools/gateway.py`, `app/models/task.py`, `app/core/db.py`, `app/main.py`.
2. Create `app/core/telemetry.py` with full graceful degradation.
3. Add `trace_id` to Task model + DB shim.
4. Wrap orchestrator and gateway methods with spans.
5. Wire `configure_telemetry()` in main.py.
6. Create tests.
7. Run `python -m compileall app && python -m unittest tests.services.test_telemetry -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-J1-opentelemetry.md
```
