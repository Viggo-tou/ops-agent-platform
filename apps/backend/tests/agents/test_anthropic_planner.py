from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.service import PrimaryAgentPlanner, build_fallback_plan_payload  # noqa: E402


def _settings(provider: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(
        primary_agent_provider=provider,
        primary_agent_model="gpt-4o-mini",
        primary_agent_timeout_seconds=30.0,
        semantic_translator_model="MiniMax-Text-01",
        minimax_planner_timeout_seconds=90.0,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        minimax_api_key=None,
        minimax_base_url="https://api.minimaxi.com",
        anthropic_api_key="sk-test",
        anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-sonnet-4-20250514",
    )


class AnthropicPlannerTests(unittest.TestCase):
    def test_generate_plan_anthropic_auto(self) -> None:
        plan_payload = build_fallback_plan_payload(
            "Where is the Firebase config?",
            scenario="process_question",
        )
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(plan_payload.model_dump(mode="json")),
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 34},
        }

        with patch("app.agents.service.httpx.post", return_value=response) as post:
            result = PrimaryAgentPlanner(settings=_settings("auto")).generate_plan(
                task_id="task-1",
                request_text="Where is the Firebase config?",
                scenario="process_question",
                actor_name="tester",
            )

        self.assertEqual(result.provider_name, "anthropic")
        self.assertEqual(result.model_name, "claude-sonnet-4-20250514")
        self.assertFalse(result.used_fallback)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "sk-test")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")

    def test_generate_plan_anthropic_fallback_on_error(self) -> None:
        with patch("app.agents.service.httpx.post", side_effect=httpx.HTTPError("boom")):
            result = PrimaryAgentPlanner(settings=_settings("auto")).generate_plan(
                task_id="task-1",
                request_text="Where is the Firebase config?",
                scenario="process_question",
                actor_name="tester",
            )

        self.assertEqual(result.provider_name, "mock")
        self.assertTrue(result.used_fallback)
        self.assertIn("boom", result.fallback_reason or "")


if __name__ == "__main__":
    unittest.main()
