"""Integration tests for intent resolution V2 fallback wiring."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import GeneratedSemanticTranslation  # noqa: E402
from app.core.enums import ActorRole, TaskStatus, WorkflowStage  # noqa: E402
from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402
from app.services.intent_resolution import MCPNotConfiguredError, ResolvedIntent  # noqa: E402
from app.services.request_refinement import RefinedRequest  # noqa: E402


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
    request_text: str = "complete OPS-123",
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
        "summary": "Add audit logging",
        "description": "Record audit events when tasks are created.",
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
    orchestrator.tool_gateway.settings.intent_resolution_version = "auto"
    orchestrator.tool_gateway.settings.intent_resolution_timeout = 90.0
    orchestrator.tool_gateway.settings.intent_resolution_max_tools = 3
    orchestrator._sync_retry_count = Mock()
    return orchestrator


@patch("app.orchestrator.service.record_event")
@patch("app.services.request_refinement.refine_request_cli")
@patch(
    "app.services.intent_resolution.resolve_intent",
    return_value=ResolvedIntent(
        refined_text="1. Update src/audit.py: add audit logging to create_task.",
        tool_calls_made=1,
        sources_consulted=["jira:OPS-123"],
        elapsed_seconds=1.2,
    ),
)
def test_v2_success_path_returns_refined_text(
    mock_v2: Mock,
    mock_v1: Mock,
    mock_record: Mock,
) -> None:
    del mock_record
    orch = _orchestrator()

    result = orch._refine_request(
        task=_task(),
        actor_name="tester",
        issue_context=_issue_context(),
        semantic_translation=_semantic_translation(),
    )

    assert result == "1. Update src/audit.py: add audit logging to create_task."
    mock_v2.assert_called_once()
    mock_v1.assert_not_called()


@patch("app.orchestrator.service.record_event")
@patch(
    "app.services.request_refinement.refine_request_cli",
    return_value=RefinedRequest(
        refined_text="Add audit logging to src/audit.py.",
        confidence=0.8,
        raw_response="Add audit logging to src/audit.py.",
    ),
)
@patch(
    "app.services.intent_resolution.resolve_intent",
    side_effect=MCPNotConfiguredError("no Jira MCP"),
)
def test_v2_mcp_not_configured_falls_back_to_v1(
    mock_v2: Mock,
    mock_v1: Mock,
    mock_record: Mock,
) -> None:
    del mock_record
    orch = _orchestrator()

    result = orch._refine_request(
        task=_task(),
        actor_name="tester",
        issue_context=_issue_context(),
        semantic_translation=_semantic_translation(),
    )

    assert result == "Add audit logging to src/audit.py."
    mock_v2.assert_called_once()
    mock_v1.assert_called_once()


@patch("app.orchestrator.service.record_event")
@patch(
    "app.services.request_refinement.refine_request_cli",
    return_value=RefinedRequest(
        refined_text="Add audit logging to src/audit.py.",
        confidence=0.8,
        raw_response="Add audit logging to src/audit.py.",
    ),
)
@patch(
    "app.services.intent_resolution.resolve_intent",
    side_effect=RuntimeError("agent failed"),
)
def test_v2_error_falls_back_to_v1(
    mock_v2: Mock,
    mock_v1: Mock,
    mock_record: Mock,
) -> None:
    del mock_record
    orch = _orchestrator()

    result = orch._refine_request(
        task=_task(),
        actor_name="tester",
        issue_context=_issue_context(),
        semantic_translation=_semantic_translation(),
    )

    assert result == "Add audit logging to src/audit.py."
    mock_v2.assert_called_once()
    mock_v1.assert_called_once()


@patch("app.orchestrator.service.record_event")
@patch(
    "app.services.request_refinement.refine_request_cli",
    return_value=RefinedRequest(
        refined_text="Add audit logging to src/audit.py.",
        confidence=0.8,
        raw_response="Add audit logging to src/audit.py.",
    ),
)
@patch("app.services.intent_resolution.resolve_intent")
def test_v1_still_works_when_v2_disabled(
    mock_v2: Mock,
    mock_v1: Mock,
    mock_record: Mock,
) -> None:
    del mock_record
    orch = _orchestrator()
    orch.tool_gateway.settings.intent_resolution_version = "v1"

    result = orch._refine_request(
        task=_task(),
        actor_name="tester",
        issue_context=_issue_context(),
        semantic_translation=_semantic_translation(),
    )

    assert result == "Add audit logging to src/audit.py."
    mock_v2.assert_not_called()
    mock_v1.assert_called_once()


@patch("app.orchestrator.service.record_event")
@patch("app.services.request_refinement.refine_request_cli", side_effect=RuntimeError("cli failed"))
@patch("app.services.intent_resolution.resolve_intent", side_effect=RuntimeError("agent failed"))
def test_v2_then_v1_failure_returns_none(
    mock_v2: Mock,
    mock_v1: Mock,
    mock_record: Mock,
) -> None:
    del mock_record
    orch = _orchestrator()

    result = orch._refine_request(
        task=_task(),
        actor_name="tester",
        issue_context=_issue_context(),
        semantic_translation=_semantic_translation(),
    )

    assert result is None
    mock_v2.assert_called_once()
    mock_v1.assert_called_once()
