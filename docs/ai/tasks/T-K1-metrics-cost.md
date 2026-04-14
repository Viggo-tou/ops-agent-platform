# T-K1 — Prometheus Metrics & LLM Cost Tracking

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

Add Prometheus-compatible metrics collection and an LLM cost tracking model. Expose a `/metrics` endpoint and a `/api/admin/costs` aggregation endpoint.

## Background

Phase K of the multi-agent MVP roadmap. The system needs operational metrics (task counts, tool durations, approval wait times) and cost visibility (LLM token usage per task/user). Without metrics there's no capacity planning or budget control.

Important: `prometheus_client` may not be installable in the codex sandbox. Use try/except import like Phase J's OTel pattern — app must work without the package.

## Design

### 1. Metrics service

New file: `apps/backend/app/services/metrics.py`

Graceful degradation pattern (same as telemetry.py):

```python
_prometheus_available = False

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _prometheus_available = True
except ImportError:
    pass
```

Define metrics (only if prometheus available):
- `ops_task_total` — Counter, labels: `scenario`, `status`
- `ops_task_duration_seconds` — Histogram, labels: `scenario`
- `ops_tool_execution_total` — Counter, labels: `tool_name`, `status`
- `ops_tool_duration_seconds` — Histogram, labels: `tool_name`
- `ops_approval_wait_seconds` — Histogram, labels: `approver_role`
- `ops_reviewer_verdict_total` — Counter, labels: `verdict`

Helper functions:
```python
def record_task_completed(scenario: str, status: str, duration_seconds: float) -> None: ...
def record_tool_execution(tool_name: str, status: str, duration_seconds: float) -> None: ...
def record_approval_wait(approver_role: str, wait_seconds: float) -> None: ...
def record_reviewer_verdict(verdict: str) -> None: ...
def get_metrics_output() -> tuple[bytes, str] | None:
    """Return (body, content_type) for /metrics, or None if prometheus not available."""
```

All functions are no-ops if prometheus_client is not installed.

### 2. LLM Usage model

New file: `apps/backend/app/models/llm_usage.py`

```python
class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), index=True, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(128), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    purpose: Mapped[str] = mapped_column(String(64), default="unknown")  # "translation", "planning", "review", "codegen"
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
```

### 3. Cost tracking service

New file: `apps/backend/app/services/cost_tracking.py`

```python
class CostTracker:
    def __init__(self, db: Session):
        self.db = db

    def record_usage(self, *, task_id: str | None, actor_name: str | None,
                     provider_name: str, model_name: str,
                     input_tokens: int, output_tokens: int,
                     purpose: str = "unknown") -> LlmUsage:
        total = input_tokens + output_tokens
        cost = self._estimate_cost(provider_name, model_name, input_tokens, output_tokens)
        usage = LlmUsage(task_id=task_id, actor_name=actor_name,
                         provider_name=provider_name, model_name=model_name,
                         input_tokens=input_tokens, output_tokens=output_tokens,
                         total_tokens=total, estimated_cost_usd=cost, purpose=purpose)
        self.db.add(usage)
        self.db.flush()
        return usage

    def get_costs(self, *, group_by: str = "task") -> list[dict]:
        """Aggregate costs by task, actor_name, or day."""
        ...

    @staticmethod
    def _estimate_cost(provider: str, model: str, inp: int, out: int) -> float:
        """Simple cost estimation based on known pricing. Returns 0.0 for unknown models."""
        # Rough per-1K-token rates
        rates = {
            "minimax": (0.001, 0.002),
            "openai": (0.01, 0.03),
        }
        inp_rate, out_rate = rates.get(provider.lower(), (0.0, 0.0))
        return (inp * inp_rate + out * out_rate) / 1000.0
```

### 4. API endpoints

New file: `apps/backend/app/api/metrics.py`

```python
router = APIRouter(tags=["metrics"])

@router.get("/metrics")
def prometheus_metrics():
    """Prometheus scrape endpoint."""
    ...

@router.get("/api/admin/costs")
def get_costs(group_by: str = "task", db: Session = Depends(get_db)):
    """LLM cost aggregation."""
    ...
```

### 5. Wire into app

In `app/main.py`: include the metrics router. Register `LlmUsage` in `app/models/__init__.py`.

In `app/core/db.py`: add create_all for LlmUsage table.

## Files to create

1. `apps/backend/app/services/metrics.py`
2. `apps/backend/app/services/cost_tracking.py`
3. `apps/backend/app/models/llm_usage.py`
4. `apps/backend/app/api/metrics.py`
5. `apps/backend/tests/services/test_metrics.py`

## Files to edit

6. `apps/backend/app/main.py` — include metrics router.
7. `apps/backend/app/models/__init__.py` — import LlmUsage.
8. `apps/backend/app/core/db.py` — ensure LlmUsage table created.
9. `apps/backend/requirements.txt` — add `prometheus_client>=0.20.0` (optional).

## Tests

All in `apps/backend/tests/services/test_metrics.py`. Use `unittest.TestCase`.

1. **`test_record_task_completed_no_crash`** — Call `record_task_completed()`. Assert no exception (works with or without prometheus).
2. **`test_record_tool_execution_no_crash`** — Same for `record_tool_execution()`.
3. **`test_get_metrics_output`** — Call `get_metrics_output()`. Assert returns `None` (no prometheus) or `(bytes, str)` tuple.
4. **`test_cost_tracker_record_usage`** — Create in-memory SQLite DB, instantiate `CostTracker`, call `record_usage()`. Assert `LlmUsage` row created with correct fields.
5. **`test_cost_tracker_estimate_cost`** — Call `_estimate_cost("openai", "gpt-4", 1000, 500)`. Assert returns a positive float.
6. **`test_cost_tracker_get_costs_by_task`** — Insert 2 usage rows for different tasks. Call `get_costs(group_by="task")`. Assert 2 groups returned.
7. **`test_cost_tracker_get_costs_empty`** — Empty DB. Assert `get_costs()` returns empty list.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 7 new tests pass.
- Full suite still green.
- App starts without prometheus_client (graceful degradation).
- `/metrics` returns prometheus format if package installed, 501 otherwise.
- `/api/admin/costs` returns aggregated cost data.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read `app/main.py`, `app/models/__init__.py`, `app/core/db.py`, `requirements.txt`.
2. Create model, services, API files.
3. Wire into main.py and models/__init__.py.
4. Create tests.
5. Run `python -m compileall app && python -m unittest tests.services.test_metrics -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-K1-metrics-cost.md
```
