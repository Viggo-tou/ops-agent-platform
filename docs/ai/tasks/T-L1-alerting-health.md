# T-L1 — Alerting & Health Enhancement

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

Enhance the `/health` endpoint with operational metrics and add a config-driven alert rules engine with webhook dispatch.

## Background

Phase L of the multi-agent MVP roadmap. Without health details and alerting, issues are only discovered by manual inspection. This phase makes the system self-reporting.

## Design

### 1. Enhanced health endpoint

Extend `apps/backend/app/api/health.py`. The existing `GET /health` returns a simple status. Enhance it to return:

```json
{
  "status": "healthy",
  "db_connected": true,
  "last_successful_task_at": "2026-04-12T10:30:00Z",
  "pending_approval_count": 3,
  "task_counts_1h": {"completed": 5, "failed": 1, "total": 6},
  "tool_failure_rate_1h": 0.05,
  "uptime_seconds": 3600
}
```

Use SQLAlchemy queries on Task, Approval, ToolExecution tables. Filter by `created_at >= now - 1 hour` for rate calculations. Store app start time in a module-level variable.

### 2. Alert rules engine

New file: `apps/backend/app/services/alerts.py`

```python
@dataclass
class AlertRule:
    name: str
    description: str
    check: Callable[[HealthData], bool]  # returns True if alert should fire
    severity: str  # "warning" | "critical"

@dataclass
class AlertResult:
    rule_name: str
    severity: str
    fired: bool
    message: str

@dataclass
class HealthData:
    db_connected: bool
    pending_approval_count: int
    task_failed_1h: int
    tool_failure_rate_1h: float
    last_successful_task_minutes_ago: float | None

class AlertEngine:
    def __init__(self, rules: list[AlertRule] | None = None):
        self.rules = rules or self._default_rules()

    def evaluate(self, health: HealthData) -> list[AlertResult]:
        ...

    @staticmethod
    def _default_rules() -> list[AlertRule]:
        return [
            AlertRule("db_down", "Database unreachable", lambda h: not h.db_connected, "critical"),
            AlertRule("high_tool_failure_rate", "Tool failure rate > 20%", lambda h: h.tool_failure_rate_1h > 0.20, "warning"),
            AlertRule("approval_backlog", "More than 10 pending approvals", lambda h: h.pending_approval_count > 10, "warning"),
            AlertRule("no_recent_success", "No successful task in 60 minutes", lambda h: h.last_successful_task_minutes_ago is not None and h.last_successful_task_minutes_ago > 60, "warning"),
        ]
```

### 3. Webhook dispatch

In `apps/backend/app/services/alerts.py`:

```python
class WebhookDispatcher:
    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url

    def dispatch(self, alerts: list[AlertResult]) -> bool:
        """POST fired alerts to webhook URL. Returns True if sent, False if no URL or no fired alerts."""
        fired = [a for a in alerts if a.fired]
        if not fired or not self.webhook_url:
            return False
        # Use httpx.post (already a dependency)
        payload = {
            "alerts": [{"rule": a.rule_name, "severity": a.severity, "message": a.message} for a in fired],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ops-agent-platform",
        }
        try:
            httpx.post(self.webhook_url, json=payload, timeout=10)
            return True
        except Exception:
            return False
```

### 4. Config

Add to `apps/backend/app/core/config.py` Settings:

```python
alert_webhook_url: str | None = None
```

### 5. Alert check endpoint

Add to health router:

```python
@router.get("/health/alerts")
def check_alerts(db: Session = Depends(get_db)):
    """Evaluate alert rules and return results. Does NOT dispatch webhook (use POST for that)."""
    ...

@router.post("/health/alerts/dispatch")
def dispatch_alerts(db: Session = Depends(get_db)):
    """Evaluate and dispatch fired alerts via webhook."""
    ...
```

## Files to create

1. `apps/backend/app/services/alerts.py`
2. `apps/backend/tests/services/test_alerts.py`

## Files to edit

3. `apps/backend/app/api/health.py` — enhance `/health`, add `/health/alerts` and `/health/alerts/dispatch`.
4. `apps/backend/app/core/config.py` — add `alert_webhook_url`.

## Tests

All in `apps/backend/tests/services/test_alerts.py`. Use `unittest.TestCase`.

1. **`test_alert_engine_no_alerts_fired`** — Healthy data. Assert no alerts fired.
2. **`test_alert_db_down`** — `db_connected=False`. Assert `db_down` rule fires with severity `critical`.
3. **`test_alert_high_tool_failure_rate`** — `tool_failure_rate_1h=0.5`. Assert `high_tool_failure_rate` fires.
4. **`test_alert_approval_backlog`** — `pending_approval_count=15`. Assert `approval_backlog` fires.
5. **`test_alert_no_recent_success`** — `last_successful_task_minutes_ago=120`. Assert fires.
6. **`test_webhook_dispatcher_no_url`** — No webhook URL. Assert `dispatch()` returns False.
7. **`test_webhook_dispatcher_no_fired_alerts`** — URL set but no alerts fired. Assert returns False.
8. **`test_custom_rules`** — Pass custom rules list to AlertEngine. Assert only custom rules evaluated.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 8 new tests pass.
- Full suite still green.
- `/health` returns enhanced operational data.
- `/health/alerts` returns alert evaluation results.
- Alert rules are configurable.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read `app/api/health.py`, `app/core/config.py`, `app/models/task.py`, `app/models/tool_execution.py`, `app/models/approval.py`.
2. Create `app/services/alerts.py`.
3. Enhance `app/api/health.py`.
4. Add `alert_webhook_url` to config.
5. Create tests.
6. Run `python -m compileall app && python -m unittest tests.services.test_alerts -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-L1-alerting-health.md
```
