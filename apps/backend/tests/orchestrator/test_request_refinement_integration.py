"""Integration tests for the request refinement gate in PrimaryOrchestrator.

These tests mock the CLI subprocess (refine_request_cli), not the actual LLM,
to verify the orchestrator's skip/call/fallback logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import GeneratedSemanticTranslation  # noqa: E402
from app.core.enums import ActorRole, RiskLevel, TaskStatus, WorkflowStage  # noqa: E402
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.request_refinement import RefinedRequest  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _semantic_translation(issue_key: str | None = "OPS-123") -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id="task-1",
        provider={"name": "test"},
        normalized_request="implement OPS-123",
        intent="develop_jira_issue",
        work_type="feature",
        objective="Implement OPS-123.",
        issue_key=issue_key,
        issue_url=None,
        candidate_modules=[],
        search_queries=[],
        constraints=[],
        requested_outputs=["jira_issue_develop"],
        grounding_terms=[],
        missing_information=[],
        confidence=0.9,
    )


def _task(
    request_text: str = "完成OPS-123",
    scenario: str = "jira_issue_develop",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        session_id="session-1",
        actor_name="tester",
        actor_role=ActorRole.ADMIN,
        request_text=request_text,
        scenario=scenario,
        status=TaskStatus.QUEUED,
        workflow_stage=WorkflowStage.INTAKE,
        translation_json={"issue_key": "OPS-123"},
        plan_json=None,
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
    )


def _issue_context() -> dict:
    return {
        "key": "OPS-123",
        "summary": "Add dark mode toggle to settings",
        "description": "Implement a dark mode toggle in the user settings page.",
        "status": "To Do",
        "issue_type": "Story",
        "priority": "Medium",
    }


def _orchestrator() -> PrimaryOrchestrator:
    orchestrator = PrimaryOrchestrator(db=Mock())
    orchestrator.tool_gateway.settings.knowledge_source_path = None
    orchestrator.tool_gateway.settings.claude_code_command = "claude"
    orchestrator.tool_gateway.settings.claude_code_args = "--print"
    orchestrator.tool_gateway.settings.claude_code_timeout_seconds = 120.0
    orchestrator._sync_retry_count = Mock()
    return orchestrator


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVagueInputTriggersRefinement:
    """When user input is vague (short, no file paths), the refinement gate
    should call refine_request_cli and return the refined text."""

    @patch("app.orchestrator.service.record_event")
    @patch(
        "app.services.request_refinement.refine_request_cli",
        return_value=RefinedRequest(
            refined_text="Add a dark mode toggle button to src/pages/Settings.tsx that persists the preference to localStorage.",
            confidence=0.8,
            raw_response="Add a dark mode toggle button to src/pages/Settings.tsx that persists the preference to localStorage.",
        ),
    )
    def test_vague_input_triggers_refinement(
        self, mock_cli: Mock, mock_record: Mock
    ) -> None:
        orch = _orchestrator()
        task = _task(request_text="完成OPS-123")
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        assert result is not None
        assert "dark mode toggle" in result
        assert "Settings.tsx" in result
        mock_cli.assert_called_once()

        # Verify events: TOOL_CALL_REQUESTED then TOOL_SUCCEEDED
        event_types = [
            call.kwargs.get("event_type") or call[1].get("event_type")
            for call in mock_record.call_args_list
            if (call.kwargs.get("tool_name") or (call[1] or {}).get("tool_name")) == "request_refinement"
        ]
        assert len(event_types) >= 2


class TestPreciseInputSkipsRefinement:
    """When user input contains a file extension pattern, refinement should be
    skipped and the method should return None."""

    @patch("app.orchestrator.service.record_event")
    def test_precise_input_skips_refinement(self, mock_record: Mock) -> None:
        orch = _orchestrator()
        task = _task(request_text="fix src/data/mockUsers.js for OPS-123")
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        assert result is None

    @patch("app.orchestrator.service.record_event")
    def test_long_input_skips_refinement(self, mock_record: Mock) -> None:
        orch = _orchestrator()
        long_text = "implement OPS-123 " + "details " * 30  # > 200 chars
        task = _task(request_text=long_text)
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        assert result is None

    @patch("app.orchestrator.service.record_event")
    def test_non_develop_scenario_skips_refinement(self, mock_record: Mock) -> None:
        orch = _orchestrator()
        task = _task(request_text="plan OPS-123", scenario="jira_issue_plan")
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        assert result is None

    @patch("app.orchestrator.service.record_event")
    def test_synthetic_context_skips_refinement(self, mock_record: Mock) -> None:
        orch = _orchestrator()
        task = _task(request_text="完成OPS-123")
        synthetic_issue = {**_issue_context(), "_synthetic": True}
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=synthetic_issue,
            semantic_translation=translation,
        )

        assert result is None


class TestCLIFailureFallback:
    """When the CLI subprocess raises an exception, the refinement gate should
    return None so the orchestrator falls back to _augment_request_with_context."""

    @patch("app.orchestrator.service.record_event")
    @patch(
        "app.services.request_refinement.refine_request_cli",
        side_effect=RuntimeError("CLI not available"),
    )
    def test_cli_failure_falls_back(self, mock_cli: Mock, mock_record: Mock) -> None:
        orch = _orchestrator()
        task = _task(request_text="完成OPS-123")
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        # Should return None (fallback), NOT raise
        assert result is None
        mock_cli.assert_called_once()

    @patch("app.orchestrator.service.record_event")
    @patch(
        "app.services.request_refinement.refine_request_cli",
        side_effect=ValueError("Refinement response too short (5 chars): 'hello'"),
    )
    def test_value_error_falls_back(self, mock_cli: Mock, mock_record: Mock) -> None:
        orch = _orchestrator()
        task = _task(request_text="完成OPS-123")
        issue = _issue_context()
        translation = _semantic_translation()

        result = orch._refine_request(
            task=task,
            actor_name="tester",
            issue_context=issue,
            semantic_translation=translation,
        )

        assert result is None
        mock_cli.assert_called_once()
