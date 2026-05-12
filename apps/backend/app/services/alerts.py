from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx


@dataclass
class HealthData:
    db_connected: bool
    pending_approval_count: int
    task_failed_1h: int
    tool_failure_rate_1h: float
    last_successful_task_minutes_ago: float | None


@dataclass
class AlertRule:
    name: str
    description: str
    check: Callable[[HealthData], bool]
    severity: str


@dataclass
class AlertResult:
    rule_name: str
    severity: str
    fired: bool
    message: str


class AlertEngine:
    def __init__(self, rules: list[AlertRule] | None = None):
        self.rules = rules or self._default_rules()

    def evaluate(self, health: HealthData) -> list[AlertResult]:
        results: list[AlertResult] = []
        for rule in self.rules:
            fired = False
            try:
                fired = bool(rule.check(health))
            except Exception:
                fired = False
            results.append(
                AlertResult(
                    rule_name=rule.name,
                    severity=rule.severity,
                    fired=fired,
                    message=rule.description,
                )
            )
        return results

    @staticmethod
    def _default_rules() -> list[AlertRule]:
        return [
            AlertRule("db_down", "Database unreachable", lambda h: not h.db_connected, "critical"),
            AlertRule(
                "high_tool_failure_rate",
                "Tool failure rate > 20%",
                lambda h: h.tool_failure_rate_1h > 0.20,
                "warning",
            ),
            AlertRule(
                "approval_backlog",
                "More than 10 pending approvals",
                lambda h: h.pending_approval_count > 10,
                "warning",
            ),
            AlertRule(
                "no_recent_success",
                "No successful task in 60 minutes",
                lambda h: h.last_successful_task_minutes_ago is not None
                and h.last_successful_task_minutes_ago > 60,
                "warning",
            ),
        ]


class WebhookDispatcher:
    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url

    def dispatch(self, alerts: list[AlertResult]) -> bool:
        """POST fired alerts to webhook URL. Returns True if sent."""
        fired = [alert for alert in alerts if alert.fired]
        if not fired or not self.webhook_url:
            return False

        payload = {
            "alerts": [
                {"rule": alert.rule_name, "severity": alert.severity, "message": alert.message}
                for alert in fired
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ops-agent-platform",
        }
        try:
            httpx.post(self.webhook_url, json=payload, timeout=10)
            return True
        except Exception:
            return False
