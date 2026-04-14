from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import GeneratedSemanticTranslation  # noqa: E402
from app.agents.service import ActionAgent, PrimaryAgentPlanner  # noqa: E402
from app.core.enums import RiskLevel  # noqa: E402
from app.orchestrator.service import PrimaryOrchestrator, classify_request  # noqa: E402


def _semantic_translation(
    *,
    objective: str = "Move OPS-123 to In Progress and add a progress comment.",
    normalized_request: str = "把 OPS-123 标记为 in progress 评论: 已开始处理",
) -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id="task-1",
        provider={"name": "test"},
        normalized_request=normalized_request,
        intent="writeback_jira_issue",
        work_type="operations",
        objective=objective,
        issue_key="OPS-123",
        issue_url=None,
        candidate_modules=[],
        search_queries=[],
        constraints=[],
        requested_outputs=["jira_writeback_payload"],
        grounding_terms=[],
        missing_information=[],
        confidence=0.9,
    )


class JiraWritebackScenarioTests(unittest.TestCase):
    def test_classify_request_writeback_with_transition(self) -> None:
        self.assertEqual(classify_request("把 OPS-123 标记为 in progress"), "jira_issue_writeback")

    def test_classify_request_writeback_with_comment(self) -> None:
        self.assertEqual(classify_request("在 OPS-123 上加评论"), "jira_issue_writeback")

    def test_classify_request_plan_not_writeback(self) -> None:
        self.assertEqual(classify_request("plan the implementation for OPS-123"), "jira_issue_plan")

    def test_generate_plan_writeback(self) -> None:
        planner = PrimaryAgentPlanner(settings=SimpleNamespace(primary_agent_provider="mock"))

        result = planner.generate_plan(
            task_id="task-1",
            request_text="把 OPS-123 标记为 in progress 评论: 已开始处理",
            scenario="jira_issue_writeback",
            actor_name="tester",
            semantic_translation=_semantic_translation(),
            issue_context={
                "issue_key": "OPS-123",
                "summary": "Investigate failing import",
                "issue_status": "To Do",
            },
        )

        plan = result.plan
        self.assertEqual([tool.tool_name for tool in plan.tools], ["jira.add_comment", "jira.transition_issue"])
        # Current tool policy maps jira.add_comment/transition_issue to write tier (not approval_required),
        # so requires_approval is dynamically False. Keep the assertion permissive for policy swaps.
        self.assertFalse(plan.requires_approval)
        self.assertEqual(plan.risk_level, RiskLevel.MEDIUM)

    def test_build_payload_writeback(self) -> None:
        payload = ActionAgent(settings=SimpleNamespace()).build_payload(
            task_id="task-1",
            request_text="把 OPS-123 标记为 in progress 评论: 已开始处理",
            scenario="jira_issue_writeback",
            semantic_translation=_semantic_translation(),
        )

        self.assertEqual(payload["issue_key"], "OPS-123")
        self.assertEqual(payload["transition_name"], "In Progress")
        self.assertIn("已开始处理", payload["text"])

    def test_execute_writeback_plan_reuses_single_approval(self) -> None:
        translation = _semantic_translation()
        plan = PrimaryAgentPlanner(settings=SimpleNamespace(primary_agent_provider="mock")).generate_plan(
            task_id="task-1",
            request_text="把 OPS-123 标记为 in progress 评论: 已开始处理",
            scenario="jira_issue_writeback",
            actor_name="tester",
            semantic_translation=translation,
            issue_context={"issue_key": "OPS-123", "summary": "Investigate failing import", "issue_status": "To Do"},
        ).plan
        task = SimpleNamespace(
            id="task-1",
            request_text="把 OPS-123 标记为 in progress 评论: 已开始处理",
            scenario="jira_issue_writeback",
            translation_json=None,
            session_id="session-1",
            latest_result_json=None,
            pending_approval=True,
            retry_count=0,
        )
        orchestrator = PrimaryOrchestrator(db=Mock())
        orchestrator.semantic_translator.translate = Mock(return_value=SimpleNamespace(translation=translation))
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                {"status": "commented", "issue_key": "OPS-123", "comment_id": "101"},
                {
                    "status": "transitioned",
                    "issue_key": "OPS-123",
                    "from_status": "To Do",
                    "to_status": "In Progress",
                },
            ]
        )
        orchestrator._sync_retry_count = Mock()

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._execute_writeback_plan(
                task=task,
                actor_name="tester",
                plan=plan,
                approval_id="approval-1",
            )

        self.assertEqual(orchestrator.tool_gateway.execute.call_count, 2)
        first_call = orchestrator.tool_gateway.execute.call_args_list[0].kwargs
        second_call = orchestrator.tool_gateway.execute.call_args_list[1].kwargs
        self.assertEqual(first_call["tool_name"], "jira.add_comment")
        self.assertEqual(second_call["tool_name"], "jira.transition_issue")
        self.assertEqual(first_call["approval_id"], "approval-1")
        self.assertEqual(second_call["approval_id"], "approval-1")
        self.assertFalse(task.pending_approval)
        self.assertEqual(task.latest_result_json["status"], "completed")


if __name__ == "__main__":
    unittest.main()
