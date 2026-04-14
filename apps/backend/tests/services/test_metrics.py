from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.llm_usage import LlmUsage  # noqa: E402
from app.services.cost_tracking import CostTracker  # noqa: E402
from app.services.metrics import (  # noqa: E402
    get_metrics_output,
    record_task_completed,
    record_tool_execution,
)


class MetricsTests(unittest.TestCase):
    def test_record_task_completed_no_crash(self) -> None:
        record_task_completed("process_question", "completed", 1.25)

    def test_record_tool_execution_no_crash(self) -> None:
        record_tool_execution("knowledge.search", "succeeded", 0.5)

    def test_get_metrics_output(self) -> None:
        output = get_metrics_output()

        if output is None:
            self.assertIsNone(output)
        else:
            body, content_type = output
            self.assertIsInstance(body, bytes)
            self.assertIsInstance(content_type, str)


class CostTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        self.db = session_factory()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_cost_tracker_record_usage(self) -> None:
        usage = CostTracker(self.db).record_usage(
            task_id="task-1",
            actor_name="alice",
            provider_name="openai",
            model_name="gpt-4",
            input_tokens=1000,
            output_tokens=500,
            purpose="planning",
        )

        saved = self.db.get(LlmUsage, usage.id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.task_id, "task-1")
        self.assertEqual(saved.actor_name, "alice")
        self.assertEqual(saved.provider_name, "openai")
        self.assertEqual(saved.model_name, "gpt-4")
        self.assertEqual(saved.input_tokens, 1000)
        self.assertEqual(saved.output_tokens, 500)
        self.assertEqual(saved.total_tokens, 1500)
        self.assertEqual(saved.purpose, "planning")
        self.assertGreater(saved.estimated_cost_usd, 0.0)

    def test_cost_tracker_estimate_cost(self) -> None:
        cost = CostTracker._estimate_cost("openai", "gpt-4", 1000, 500)

        self.assertIsInstance(cost, float)
        self.assertGreater(cost, 0.0)

    def test_cost_tracker_get_costs_by_task(self) -> None:
        tracker = CostTracker(self.db)
        tracker.record_usage(
            task_id="task-1",
            actor_name="alice",
            provider_name="openai",
            model_name="gpt-4",
            input_tokens=1000,
            output_tokens=500,
            purpose="planning",
        )
        tracker.record_usage(
            task_id="task-2",
            actor_name="bob",
            provider_name="minimax",
            model_name="minimax-text",
            input_tokens=2000,
            output_tokens=1000,
            purpose="review",
        )

        costs = tracker.get_costs(group_by="task")

        self.assertEqual(len(costs), 2)
        self.assertEqual({row["key"] for row in costs}, {"task-1", "task-2"})

    def test_cost_tracker_get_costs_empty(self) -> None:
        costs = CostTracker(self.db).get_costs()

        self.assertEqual(costs, [])


if __name__ == "__main__":
    unittest.main()
