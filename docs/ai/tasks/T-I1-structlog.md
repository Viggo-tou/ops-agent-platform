# T-I1 — Structured Logging with structlog

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add structlog to the backend, outputting JSON-structured logs to stdout. Bridge every `record_event()` call to also emit a structlog entry. Add FastAPI request logging middleware.

## Background

Phase I of the multi-agent MVP roadmap. Currently the backend's observability relies entirely on the `Event` DB table. This is fine for audit, but:
- Not accessible in real-time without DB queries
- Lost if the app crashes before commit
- Can't feed into external log aggregation (ELK, Loki, CloudWatch)

structlog outputs to stdout in JSON, which Docker/K8s automatically collect.

## Design

### 1. structlog configuration

New file: `apps/backend/app/core/logging.py`

```python
import structlog

def configure_logging(*, json_output: bool = True) -> None:
    """Configure structlog for the application."""
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(**kwargs) -> structlog.BoundLogger:
    return structlog.get_logger(**kwargs)
```

### 2. Event bridge

In `apps/backend/app/services/events.py`, at the end of `record_event()`, add:

```python
from app.core.logging import get_logger

_event_logger = get_logger(component="events")

# At the end of record_event():
_event_logger.info(
    "lifecycle_event",
    task_id=task_id,
    event_type=event_type.value if hasattr(event_type, 'value') else str(event_type),
    source=source.value if hasattr(source, 'value') else str(source),
    stage=stage.value if stage and hasattr(stage, 'value') else str(stage),
    role=role.value if role and hasattr(role, 'value') else str(role),
    tool_name=tool_name,
    message=message,
)
```

### 3. Request logging middleware

New file: `apps/backend/app/core/middleware.py`

```python
import time
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.logging import get_logger

_request_logger = get_logger(component="http")

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)
        _request_logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            actor_role=request.headers.get("X-Actor-Role"),
        )
        return response
```

### 4. Wire into app startup

In `apps/backend/app/main.py`:
- Call `configure_logging()` early (before app creation).
- Add `RequestLoggingMiddleware` to the FastAPI app.

### 5. Add structlog to requirements

Add `structlog>=24.1.0` to `apps/backend/requirements.txt`.

## Files to create

1. `apps/backend/app/core/logging.py`
2. `apps/backend/app/core/middleware.py`

## Files to edit

3. `apps/backend/app/services/events.py` — add structlog bridge at end of `record_event()`.
4. `apps/backend/app/main.py` — call `configure_logging()`, add middleware.
5. `apps/backend/requirements.txt` — add `structlog>=24.1.0`.

## Tests

Add to `apps/backend/tests/services/test_structlog.py`. Use `unittest.TestCase`.

1. **`test_configure_logging_json`** — Call `configure_logging(json_output=True)`. Get logger, log a message. Assert no exception.
2. **`test_configure_logging_console`** — Call `configure_logging(json_output=False)`. Get logger, log a message. Assert no exception.
3. **`test_get_logger_returns_bound_logger`** — Assert `get_logger()` returns an object with `.info()`, `.warning()`, `.error()` methods.
4. **`test_event_bridge_emits_log`** — Patch `structlog.get_logger` to capture output. Call `record_event()` with known params. Assert the structlog entry contains `task_id`, `event_type`, `message`.
5. **`test_request_middleware_logs`** — Use FastAPI TestClient with the middleware. Make a GET request. Capture structlog output. Assert log contains `method`, `path`, `status_code`, `duration_ms`.

## Acceptance criteria

- `pip install structlog>=24.1.0` succeeds.
- `python -m compileall app` exits 0.
- All 5 new tests pass.
- Full suite still green.
- Starting the backend prints JSON log lines to stdout for each HTTP request.
- Every `record_event()` call emits a corresponding structlog entry.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read `app/services/events.py`, `app/main.py`, `requirements.txt`.
2. `pip install structlog>=24.1.0` and add to requirements.txt.
3. Create `app/core/logging.py` and `app/core/middleware.py`.
4. Edit `events.py` to add bridge. Edit `main.py` to wire startup + middleware.
5. Create tests.
6. Run `python -m compileall app && python -m unittest tests.services.test_structlog -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-I1-structlog.md
```
