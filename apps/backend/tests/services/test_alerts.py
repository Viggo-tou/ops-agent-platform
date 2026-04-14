from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.alerts import AlertEngine, AlertResult, AlertRule, HealthData, WebhookDispatcher  # noqa: E402


def _healthy_data() -> HealthData:
    return HealthData(
        db_connected=True,
        pending_approval_count=0,
        task_failed_1h=0,
        tool_failure_rate_1h=0.0,
        last_successful_task_minutes_ago=5.0,
    )


class AlertEngineTests(unittest.TestCase):
    def _results_by_name(self, health: HealthData) -> dict[str, AlertResult]:
        return {result.rule_name: result for result in AlertEngine().evaluate(health)}

    def test_alert_engine_no_alerts_fired(self) -> None:
        results = AlertEngine().evaluate(_healthy_data())

        self.assertTrue(results)
        self.assertFalse(any(result.fired for result in results))

    def test_alert_db_down(self) -> None:
        health = _healthy_data()
        health.db_connected = False

        result = self._results_by_name(health)["db_down"]

        self.assertTrue(result.fired)
        self.assertEqual(result.severity, "critical")

    def test_alert_high_tool_failure_rate(self) -> None:
        health = _healthy_data()
        health.tool_failure_rate_1h = 0.5

        result = self._results_by_name(health)["high_tool_failure_rate"]

        self.assertTrue(result.fired)

    def test_alert_approval_backlog(self) -> None:
        health = _healthy_data()
        health.pending_approval_count = 15

        result = self._results_by_name(health)["approval_backlog"]

        self.assertTrue(result.fired)

    def test_alert_no_recent_success(self) -> None:
        health = _healthy_data()
        health.last_successful_task_minutes_ago = 120

        result = self._results_by_name(health)["no_recent_success"]

        self.assertTrue(result.fired)

    def test_custom_rules(self) -> None:
        rules = [
            AlertRule("custom", "Custom rule", lambda h: h.task_failed_1h > 2, "warning"),
        ]

        results = AlertEngine(rules=rules).evaluate(
            HealthData(
                db_connected=False,
                pending_approval_count=99,
                task_failed_1h=3,
                tool_failure_rate_1h=1.0,
                last_successful_task_minutes_ago=120,
            )
        )

        self.assertEqual([result.rule_name for result in results], ["custom"])
        self.assertTrue(results[0].fired)


class WebhookDispatcherTests(unittest.TestCase):
    def test_webhook_dispatcher_no_url(self) -> None:
        alerts = [AlertResult("db_down", "critical", True, "Database unreachable")]

        self.assertFalse(WebhookDispatcher().dispatch(alerts))

    def test_webhook_dispatcher_no_fired_alerts(self) -> None:
        alerts = [AlertResult("db_down", "critical", False, "Database unreachable")]

        self.assertFalse(WebhookDispatcher("https://example.test/webhook").dispatch(alerts))


if __name__ == "__main__":
    unittest.main()
