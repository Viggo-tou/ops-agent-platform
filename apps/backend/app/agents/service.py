from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schemas import (
    ApprovalRequirement,
    FinalOutputContract,
    GeneratedPlan,
    GeneratedPlanPayload,
    GeneratedReview,
    GeneratedReviewPayload,
    GeneratedSemanticTranslation,
    PlanGenerationResult,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
    PolicyCheck,
    ReviewFinding,
    ReviewGenerationResult,
)
from app.core.config import Settings, get_settings
from app.core.enums import RiskLevel, RoleName, TaskStatus, ToolPermissionCategory
from app.core.jira import extract_jira_issue_reference
from app.core.timeouts import external_http_timeout
from app.models.knowledge_document import KnowledgeDocument
from app.services.llm_telemetry import LlmCall, record_llm_call
from app.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)

PLAN_STRING_LIST_LIMITS = {
    "assumptions": 8,
    "missing_information": 8,
    "approval_reasons": 6,
    "required_fields": 12,
}


def _short_request_summary(request_text: str) -> str:
    normalized = " ".join(request_text.strip().split())
    if len(normalized) <= 120:
        return normalized
    return f"{normalized[:117]}..."


def _trim_string_list(values: list[str], *, limit: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.strip().split())
        if not normalized:
            continue
        dedupe_key = normalized.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


def _trim_text(value: str, *, limit: int) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 1)]}..."


def _looks_like_test_path(path: str) -> bool:
    """Return True if ``path`` is a test file by the usual conventions.

    Used by the planner sanitizer to keep test files out of
    ``must_touch_files`` for code-change scenarios — the fix should
    live in source, not in a freshly-edited test file. Conservative:
    only matches obvious test patterns (path starts with ``tests/``
    or basename matches ``test_*.py``/``*_test.py``).
    """
    if not isinstance(path, str):
        return False
    norm = path.replace("\\", "/").strip().lstrip("/")
    if not norm:
        return False
    if norm.startswith("tests/") or "/tests/" in f"/{norm}":
        return True
    base = norm.rsplit("/", 1)[-1].lower()
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    if base in {"conftest.py"}:
        return True
    return False


def _extract_plan_locations_from_knowledge(planning_knowledge: dict[str, Any] | None) -> list[PlanCodeLocation]:
    if not planning_knowledge:
        return []

    raw_citations = planning_knowledge.get("citations")
    if not isinstance(raw_citations, list):
        return []

    locations: list[PlanCodeLocation] = []
    for citation in raw_citations:
        if not isinstance(citation, dict):
            continue
        source_name = citation.get("source_name")
        relative_path = citation.get("relative_path")
        if not isinstance(source_name, str) or not source_name.strip():
            continue
        if not isinstance(relative_path, str) or not relative_path.strip():
            continue

        title = citation.get("title") if isinstance(citation.get("title"), str) else relative_path
        reason = f"{title} ranked highly during planning-time retrieval for this request."
        line_start = citation.get("line_start") if isinstance(citation.get("line_start"), int) else None
        line_end = citation.get("line_end") if isinstance(citation.get("line_end"), int) else None
        locations.append(
            PlanCodeLocation(
                source_name=source_name.strip(),
                relative_path=relative_path.strip(),
                reason=reason[:240],
                line_start=line_start,
                line_end=line_end,
            )
        )
        if len(locations) >= 6:
            break
    return locations


def _validate_must_touch_against_kb(must_touch: list[str], db: Session) -> tuple[list[str], list[str]]:
    """Return (kept, dropped), dropping paths not present in KnowledgeDocument."""
    if not must_touch:
        return [], []

    indexed_paths = set(
        db.execute(
            select(KnowledgeDocument.relative_path).where(KnowledgeDocument.relative_path.in_(must_touch))
        ).scalars()
    )
    kept: list[str] = []
    dropped: list[str] = []
    for path in must_touch:
        if path in indexed_paths:
            kept.append(path)
        else:
            dropped.append(path)
    return kept, dropped


def _log_plan_target_warnings(
    *,
    scenario: str,
    must_touch_files: list[str],
    expected_new_files: list[str],
) -> None:
    if scenario in {"jira_issue_develop", "bug_fix"} and not must_touch_files and not expected_new_files:
        logger.info(
            "plan target files under-specified: scenario=%s has empty must_touch_files and expected_new_files",
            scenario,
        )
    if scenario == "process_question" and must_touch_files:
        logger.info(
            "plan target files over-specified: process_question has must_touch_files=%s",
            must_touch_files,
        )


def _materialize_plan(
    *,
    payload: GeneratedPlanPayload,
    task_id: str,
    provider: dict[str, Any],
) -> GeneratedPlan:
    return GeneratedPlan(
        task_id=task_id,
        provider=provider,
        **payload.model_dump(mode="python"),
    )


def _materialize_review(
    *,
    payload: GeneratedReviewPayload,
    task_id: str,
    plan_id: str,
    provider: dict[str, Any],
) -> GeneratedReview:
    return GeneratedReview(
        task_id=task_id,
        plan_id=plan_id,
        provider=provider,
        **payload.model_dump(mode="python"),
    )


def build_fallback_plan_payload(
    request_text: str,
    *,
    scenario: str,
    semantic_translation: GeneratedSemanticTranslation | None = None,
    planning_knowledge: dict[str, Any] | None = None,
    issue_context: dict[str, Any] | None = None,
) -> GeneratedPlanPayload:
    request_summary = _short_request_summary(request_text)
    registry = ToolRegistry()
    change_summary = semantic_translation.objective if semantic_translation else request_summary
    change_explanation = (
        semantic_translation.normalized_request
        if semantic_translation and semantic_translation.normalized_request
        else request_summary
    )
    affected_code_locations = _extract_plan_locations_from_knowledge(planning_knowledge)

    # For develop scenarios, citations are grounding context, not edit targets.
    # Using them as affected_code_locations silently misleads downstream codegen
    # (it tries to modify grounding files) and the completeness checker (it greps
    # the wrong files). Leave it empty; the pipeline has multi-path new-file
    # detection (plan/disk/request-text) to recover the correct targets.
    if scenario == "jira_issue_develop":
        develop_affected_code_locations: list[PlanCodeLocation] = []
    else:
        develop_affected_code_locations = affected_code_locations

    # T-038-B: when the request mentions a destructive verb and retrieval
    # surfaced citation files, mark those files as must_touch_files so the
    # spec-conformance gate can enforce that the patch actually edits them.
    _DESTRUCTIVE_VERBS_FOR_MUST_TOUCH = (
        "remove", "delete", "clean", "rename", "refactor", "fix",
        "replace", "simplify", "strip", "eliminate", "drop", "disable",
    )
    must_touch_paths: list[str] = []
    if scenario == "jira_issue_develop" and affected_code_locations:
        request_lower = (request_text or "").lower()
        if any(verb in request_lower for verb in _DESTRUCTIVE_VERBS_FOR_MUST_TOUCH):
            seen: set[str] = set()
            for loc in affected_code_locations:
                rel = (loc.relative_path or "").strip()
                if rel and rel not in seen:
                    seen.add(rel)
                    must_touch_paths.append(rel)
                if len(must_touch_paths) >= 6:
                    break

    if scenario == "jira_issue_develop":
        codegen_tool = "codegen.generate_patch"
        codegen_permission = registry.get_permission_category(codegen_tool)
        issue_summary = str(issue_context.get("summary") or "").strip() if issue_context else ""
        issue_description = str(issue_context.get("description") or "").strip() if issue_context else ""
        develop_change_summary = issue_summary or change_summary
        explanation_lines = [
            change_explanation,
            issue_description,
        ]
        if affected_code_locations:
            explanation_lines.append(
                "Likely source files were inferred from repository retrieval before planning."
            )
        return GeneratedPlanPayload(
            objective="Implement the Jira issue by generating code changes, applying patches, running tests, and reviewing results.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=develop_change_summary[:320],
            change_explanation=" ".join(line for line in explanation_lines if line).strip()[:1200],
            assumptions=[
                "The referenced Jira issue exists and contains enough context to generate code.",
                "Affected source files can be read from the knowledge index or sandbox.",
            ],
            missing_information=[],
            risk_level=RiskLevel.MEDIUM,
            requires_approval=codegen_permission == ToolPermissionCategory.APPROVAL_REQUIRED,
            approval_reasons=["Code generation modifies source files and requires human approval."]
            if codegen_permission == ToolPermissionCategory.APPROVAL_REQUIRED
            else [],
            affected_code_locations=develop_affected_code_locations,
            must_touch_files=must_touch_paths,
            tools=[
                PlanTool(
                    tool_name=codegen_tool,
                    permission_category=codegen_permission,
                    purpose="Generate code changes that implement the Jira issue requirements.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Analyze Jira issue requirements and affected files",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Issue requirements mapped to affected source files.",
                    success_criteria="The issue context and file locations are identified.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Generate code patch from plan and file context",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=codegen_tool,
                    expected_output="Unified diff implementing the required changes.",
                    success_criteria="A valid patch is generated covering the affected files.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Apply patch and run tests in sandbox",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Patch applied, test results collected.",
                    success_criteria="Patch applies cleanly and tests pass or are skipped.",
                ),
                PlanStep(
                    step_id="step_4",
                    title="Review generated changes",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_3"],
                    tool_name=None,
                    expected_output="Reviewed code changes ready for merge.",
                    success_criteria="Changes are correct, safe, and match the Jira issue requirements.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="jira_issue_develop",
                required_fields=["issue_key", "files_changed", "diff", "summary"],
            ),
        )

    if scenario == "jira_issue_plan":
        tool_name = "jira.get_issue"
        permission_category = registry.get_permission_category(tool_name)
        issue_summary = str(issue_context.get("summary") or "").strip() if issue_context else ""
        issue_description = str(issue_context.get("description") or "").strip() if issue_context else ""
        change_summary = issue_summary or change_summary
        explanation_lines = [
            change_explanation,
            issue_description,
        ]
        if affected_code_locations:
            explanation_lines.append(
                "Likely source files were inferred from repository retrieval before planning."
            )
        return GeneratedPlanPayload(
            objective="Read the Jira issue context and produce an implementation plan for the work item.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=change_summary[:320],
            change_explanation=" ".join(line for line in explanation_lines if line).strip()[:1200],
            assumptions=["The referenced Jira issue exists and contains enough context to plan the work."],
            missing_information=[],
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            approval_reasons=[],
            affected_code_locations=affected_code_locations,
            tools=[
                PlanTool(
                    tool_name=tool_name,
                    permission_category=permission_category,
                    purpose="Load the Jira issue context used to build the implementation plan.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Load the Jira issue context",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Issue summary, acceptance context, and planning constraints.",
                    success_criteria="The Jira issue context is available to the planner.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Refresh the Jira issue details for execution trace",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=tool_name,
                    expected_output="Current Jira issue metadata and description.",
                    success_criteria="The referenced issue can be read successfully.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Review the generated implementation plan",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Reviewed plan ready for dashboard display.",
                    success_criteria="The generated plan is complete and grounded in the Jira issue context.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="jira_issue_plan",
                required_fields=["issue_key", "summary", "agent_plan"],
            ),
        )

    if scenario == "slack_message":
        tool_name = "slack.post_message"
        permission_category = registry.get_permission_category(tool_name)
        requires_approval = permission_category == ToolPermissionCategory.APPROVAL_REQUIRED
        return GeneratedPlanPayload(
            objective="Send an operational message to Slack through the unified tool gateway.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=change_summary[:320],
            change_explanation=change_explanation[:1200],
            assumptions=["The request can be converted into a Slack channel message."],
            missing_information=[],
            risk_level=RiskLevel.LOW,
            requires_approval=requires_approval,
            approval_reasons=["Slack messaging is mapped to approval_required in the current tool policy."]
            if requires_approval
            else [],
            affected_code_locations=[],
            tools=[
                PlanTool(
                    tool_name=tool_name,
                    permission_category=permission_category,
                    purpose="Post the requested operational message to Slack.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Normalize the Slack notification request",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Structured channel and message payload.",
                    success_criteria="The destination and message body are explicit.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Send the Slack message",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=tool_name,
                    expected_output="Slack delivery status and message timestamp.",
                    success_criteria="The Slack API accepts the message request.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Review the Slack delivery result",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Reviewed Slack execution result.",
                    success_criteria="The output includes the target channel and delivery status.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="slack_message_result",
                required_fields=["status", "tool_name", "channel", "text"],
            ),
        )

    if scenario == "jira_issue_create":
        tool_name = "jira.create_issue"
        permission_category = registry.get_permission_category(tool_name)
        requires_approval = permission_category == ToolPermissionCategory.APPROVAL_REQUIRED
        return GeneratedPlanPayload(
            objective="Create a Jira issue through the unified tool gateway.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=change_summary[:320],
            change_explanation=change_explanation[:1200],
            assumptions=["The request can be translated into a Jira issue summary and description."],
            missing_information=[],
            risk_level=RiskLevel.MEDIUM,
            requires_approval=requires_approval,
            approval_reasons=["Jira issue creation is mapped to approval_required in the current tool policy."]
            if requires_approval
            else [],
            affected_code_locations=[],
            tools=[
                PlanTool(
                    tool_name=tool_name,
                    permission_category=permission_category,
                    purpose="Create the requested Jira issue.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Normalize the Jira issue request",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Structured Jira project, issue type, and summary intent.",
                    success_criteria="The request summary is clear and executable.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Create the Jira issue",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=tool_name,
                    expected_output="Jira issue key and summary.",
                    success_criteria="The issue is created successfully.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Validate the Jira output",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Reviewed result ready for dashboard display.",
                    success_criteria="The output contains all required fields.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="jira_issue_result",
                required_fields=["status", "tool_name", "issue_key", "summary"],
            ),
        )

    if scenario == "jira_issue_writeback":
        comment_tool = "jira.add_comment"
        transition_tool = "jira.transition_issue"
        comment_category = registry.get_permission_category(comment_tool)
        transition_category = registry.get_permission_category(transition_tool)
        issue_summary = str(issue_context.get("summary") or "").strip() if issue_context else ""
        current_status = str(issue_context.get("issue_status") or "").strip() if issue_context else ""
        explanation_lines = [
            change_explanation,
            f"Current Jira status: {current_status}." if current_status else "",
        ]
        return GeneratedPlanPayload(
            objective="Write status and comments back to the Jira issue.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=(issue_summary or change_summary)[:320],
            change_explanation=" ".join(line for line in explanation_lines if line).strip()[:1200],
            assumptions=[
                "The referenced Jira issue exists.",
                "The requested transition is available in the issue's current workflow state.",
            ],
            missing_information=[],
            risk_level=RiskLevel.MEDIUM,
            requires_approval=(
                comment_category == ToolPermissionCategory.APPROVAL_REQUIRED
                or transition_category == ToolPermissionCategory.APPROVAL_REQUIRED
            ),
            approval_reasons=(
                ["Jira writeback tools are mapped to approval_required in the current tool policy."]
                if comment_category == ToolPermissionCategory.APPROVAL_REQUIRED
                or transition_category == ToolPermissionCategory.APPROVAL_REQUIRED
                else []
            ),
            affected_code_locations=[],
            tools=[
                PlanTool(
                    tool_name=comment_tool,
                    permission_category=comment_category,
                    purpose="Post a progress comment to the Jira issue.",
                ),
                PlanTool(
                    tool_name=transition_tool,
                    permission_category=transition_category,
                    purpose="Transition the Jira issue to the requested workflow status.",
                ),
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Validate the writeback request",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Validated issue key, transition name, and comment text.",
                    success_criteria="The request contains a valid issue key and at least one writeback action.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Post progress comment to Jira",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=comment_tool,
                    expected_output="Comment ID and creation timestamp.",
                    success_criteria="The comment is visible on the Jira issue.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Transition the Jira issue status",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_2"],
                    tool_name=transition_tool,
                    expected_output="From-status and to-status confirmation.",
                    success_criteria="The issue status matches the requested target.",
                ),
                PlanStep(
                    step_id="step_4",
                    title="Review the writeback results",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_3"],
                    tool_name=None,
                    expected_output="Confirmed writeback result for dashboard display.",
                    success_criteria="Both comment and transition completed successfully.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="jira_writeback_result",
                required_fields=["status", "issue_key", "comment", "transition"],
            ),
        )

    if scenario in {"internal_api_request", "action_with_approval"}:
        tool_name = "internal_api.request"
        permission_category = registry.get_permission_category(tool_name)
        requires_approval = permission_category == ToolPermissionCategory.APPROVAL_REQUIRED
        return GeneratedPlanPayload(
            objective="Call an internal enterprise API through the unified tool gateway.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=change_summary[:320],
            change_explanation=change_explanation[:1200],
            assumptions=["The requested API operation can be represented as a single internal API call."],
            missing_information=[],
            risk_level=RiskLevel.HIGH,
            requires_approval=requires_approval,
            approval_reasons=["The internal API tool is mapped to approval_required in the current tool policy."]
            if requires_approval
            else [],
            affected_code_locations=[],
            tools=[
                PlanTool(
                    tool_name=tool_name,
                    permission_category=permission_category,
                    purpose="Call the requested internal API endpoint.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Normalize the internal API request",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Structured method, path, and request body.",
                    success_criteria="The endpoint and intended operation are explicit.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Execute the internal API request",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=tool_name,
                    expected_output="Internal API response status and payload.",
                    success_criteria="The internal API responds successfully.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Review the internal API result",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Reviewed internal API execution result.",
                    success_criteria="The output contains the response status and target path.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="internal_api_result",
                required_fields=["status", "tool_name", "response_status", "path"],
            ),
        )

    if scenario == "internal_db_query":
        tool_name = "internal_db.query"
        permission_category = registry.get_permission_category(tool_name)
        requires_approval = permission_category == ToolPermissionCategory.APPROVAL_REQUIRED
        return GeneratedPlanPayload(
            objective="Run a guarded internal database lookup through the unified tool gateway.",
            request_summary=request_summary,
            scenario=scenario,
            change_summary=change_summary[:320],
            change_explanation=change_explanation[:1200],
            assumptions=["The request can be reduced to a read-only SQL lookup."],
            missing_information=[],
            risk_level=RiskLevel.HIGH,
            requires_approval=requires_approval,
            approval_reasons=["The internal database tool is mapped to approval_required in the current tool policy."]
            if requires_approval
            else [],
            affected_code_locations=[],
            tools=[
                PlanTool(
                    tool_name=tool_name,
                    permission_category=permission_category,
                    purpose="Run the requested read-only internal DB query.",
                )
            ],
            steps=[
                PlanStep(
                    step_id="step_1",
                    title="Normalize the internal database lookup",
                    kind="analysis",
                    owner_role=RoleName.PLANNER,
                    depends_on=[],
                    tool_name=None,
                    expected_output="Structured read-only SQL request.",
                    success_criteria="The query intent is explicit and safe for execution.",
                ),
                PlanStep(
                    step_id="step_2",
                    title="Run the internal database query",
                    kind="action",
                    owner_role=RoleName.ACTION,
                    depends_on=["step_1"],
                    tool_name=tool_name,
                    expected_output="Read-only database rows and row count.",
                    success_criteria="The query executes successfully and returns rows or an empty result.",
                ),
                PlanStep(
                    step_id="step_3",
                    title="Review the database result",
                    kind="review",
                    owner_role=RoleName.REVIEWER,
                    depends_on=["step_2"],
                    tool_name=None,
                    expected_output="Reviewed database result ready for the dashboard.",
                    success_criteria="The output contains the row count and returned records.",
                ),
            ],
            final_output_contract=FinalOutputContract(
                type="internal_db_result",
                required_fields=["status", "tool_name", "row_count", "rows"],
            ),
        )

    return GeneratedPlanPayload(
        objective="Retrieve enterprise knowledge and package a structured answer for the request.",
        request_summary=request_summary,
        scenario=scenario,
        change_summary=change_summary[:320],
        change_explanation=(
            "Answer the question with grounded evidence from the repository. "
            f"{change_explanation}"
        )[:1200],
        assumptions=["The answer should be grounded in the configured repository knowledge source."],
        missing_information=[],
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=affected_code_locations,
        tools=[
            PlanTool(
                tool_name="knowledge.search",
                permission_category=ToolPermissionCategory.READ_ONLY,
                purpose="Search the configured repository knowledge base and package the answer.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Interpret the process question",
                kind="analysis",
                owner_role=RoleName.PLANNER,
                depends_on=[],
                tool_name=None,
                expected_output="Question scope and retrieval target.",
                success_criteria="The question is clear enough for lookup.",
            ),
            PlanStep(
                step_id="step_2",
                title="Search and package repository knowledge",
                kind="knowledge",
                owner_role=RoleName.KNOWLEDGE,
                depends_on=["step_1"],
                tool_name="knowledge.search",
                expected_output="Packaged context and source list.",
                success_criteria="Relevant knowledge is retrieved successfully.",
            ),
            PlanStep(
                step_id="step_3",
                title="Review the knowledge answer",
                kind="review",
                owner_role=RoleName.REVIEWER,
                depends_on=["step_2"],
                tool_name=None,
                expected_output="Reviewed knowledge answer.",
                success_criteria="The response is complete and safe to show.",
            ),
        ],
        final_output_contract=FinalOutputContract(
            type="knowledge_answer",
            required_fields=["answer", "citations", "answer_trace"],
        ),
    )


class PrimaryAgentPlanner:
    def __init__(self, settings: Settings | None = None, *, db: Session | None = None):
        self.settings = settings or get_settings()
        self.db = db

    def _resolve_model_name(self, provider_name: str) -> str:
        if provider_name == "claude_code":
            return "claude-code"
        if provider_name == "anthropic":
            return getattr(self.settings, "anthropic_model", "claude-sonnet-4-20250514")
        configured_model = self.settings.primary_agent_model.strip()
        if provider_name == "minimax" and configured_model.lower().startswith("gpt"):
            return self.settings.semantic_translator_model
        return configured_model

    def generate_plan(
        self,
        *,
        task_id: str,
        request_text: str,
        scenario: str,
        actor_name: str,
        semantic_translation: GeneratedSemanticTranslation | None = None,
        planning_knowledge: dict[str, Any] | None = None,
        issue_context: dict[str, Any] | None = None,
    ) -> PlanGenerationResult:
        """Generate a plan using the provider chain. On failure, cascade to the next provider."""
        # UI-set per-stage override (runtime_overrides.json) wins over .env.
        # When unset, falls through bytewise to the historical .env-driven path.
        from app.services.runtime_override import effective_provider
        planner_override = effective_provider(
            "planner", getattr(self.settings, "planner_provider", None)
        )
        if planner_override and planner_override != "auto":
            provider_mode = planner_override
        else:
            provider_mode = effective_provider(
                "primary_agent", self.settings.primary_agent_provider
            )

        anthropic_api_key = getattr(self.settings, "anthropic_api_key", None)
        openai_api_key = getattr(self.settings, "openai_api_key", None)
        minimax_api_key = getattr(self.settings, "minimax_api_key", None)

        # Build ordered provider chain
        providers: list[str] = []
        if provider_mode == "auto":
            if shutil.which(self.settings.claude_code_command):
                providers.append("claude_code")
            if anthropic_api_key:
                providers.append("anthropic")
            if openai_api_key:
                providers.append("openai")
            if minimax_api_key:
                providers.append("minimax")
        elif provider_mode == "codex":
            # codex is a codegen provider, not a planner — fallback to anthropic
            if anthropic_api_key:
                providers.append("anthropic")
        elif provider_mode != "mock":
            providers.append(provider_mode)

        default_payload = build_fallback_plan_payload(
            request_text,
            scenario=scenario,
            semantic_translation=semantic_translation,
            planning_knowledge=planning_knowledge,
            issue_context=issue_context,
        )

        last_error: str | None = None
        prompt_fingerprint = hashlib.sha256(f"{scenario}\n{request_text}".encode("utf-8")).hexdigest()[:10]
        for fallback_step, provider_name in enumerate(providers):
            started = time.perf_counter()
            try:
                result = self._try_plan_provider(
                    provider_name=provider_name,
                    task_id=task_id,
                    request_text=request_text,
                    scenario=scenario,
                    actor_name=actor_name,
                    default_payload=default_payload,
                )
                self._record_plan_call(
                    task_id=task_id,
                    actor_name=actor_name,
                    provider_name=provider_name,
                    model_name=result.model_name or self._resolve_model_name(provider_name),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=True,
                    fallback_step=fallback_step,
                    prompt_fingerprint=prompt_fingerprint,
                )
                return result
            except Exception as exc:
                last_error = str(exc)
                self._record_plan_call(
                    task_id=task_id,
                    actor_name=actor_name,
                    provider_name=provider_name,
                    model_name=self._resolve_model_name(provider_name),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    fallback_step=fallback_step,
                    prompt_fingerprint=prompt_fingerprint,
                    error_type=type(exc).__name__,
                )
                continue  # cascade to next provider

        # All providers failed or none configured — use mock fallback
        default_payload = self._validate_plan_targets(default_payload)
        fallback_plan = _materialize_plan(
            payload=default_payload,
            task_id=task_id,
            provider={
                "name": "mock",
                "mode": f"fallback_after_all_providers_failed" if providers else "no_provider_configured",
                "attempted_providers": providers,
                "error": last_error,
            },
        )
        return PlanGenerationResult(
            plan=fallback_plan,
            provider_name="mock",
            model_name=None,
            used_fallback=True,
            fallback_reason=last_error,
        )

    def _record_plan_call(
        self,
        *,
        task_id: str,
        actor_name: str,
        provider_name: str,
        model_name: str | None,
        latency_ms: int,
        success: bool,
        fallback_step: int,
        prompt_fingerprint: str,
        error_type: str | None = None,
    ) -> None:
        if self.db is None:
            return
        record_llm_call(
            self.db,
            LlmCall(
                purpose="planner",
                provider=provider_name,
                model=model_name or "",
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                success=success,
                fallback_step=fallback_step,
                error_type=error_type,
                prompt_fingerprint=prompt_fingerprint,
                task_id=task_id,
                actor_name=actor_name,
            ),
        )

    def _try_plan_provider(
        self,
        *,
        provider_name: str,
        task_id: str,
        request_text: str,
        scenario: str,
        actor_name: str,
        default_payload: dict[str, Any],
    ) -> PlanGenerationResult:
        """Attempt plan generation with a single provider. Raises on failure."""
        model_name = self._resolve_model_name(provider_name)

        if provider_name == "claude_code":
            plan_payload = self._generate_plan_with_claude_code(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                default_payload=default_payload,
            )
            mode = "claude_code_cli"
            model_name = "claude-code"
        elif provider_name == "anthropic":
            plan_payload = self._generate_plan_with_anthropic(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
            )
            mode = "messages_api"
        elif provider_name == "openai":
            plan_payload = self._generate_plan_with_openai(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
            )
            mode = "responses_api"
        elif provider_name == "minimax":
            plan_payload = self._generate_plan_with_minimax(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
            )
            mode = "chatcompletion_v2"
        else:
            raise ValueError(f"Unknown plan provider: {provider_name}")

        plan_payload = self._validate_plan_targets(plan_payload)
        plan = _materialize_plan(
            payload=plan_payload,
            task_id=task_id,
            provider={
                "name": provider_name,
                "mode": mode,
                "model": model_name,
            },
        )
        return PlanGenerationResult(
            plan=plan,
            provider_name=provider_name,
            model_name=model_name,
        )

    def _validate_plan_targets(self, plan_payload: GeneratedPlanPayload) -> GeneratedPlanPayload:
        must_touch = list(plan_payload.must_touch_files or [])
        if self.db is not None and must_touch:
            kept, dropped = _validate_must_touch_against_kb(must_touch, self.db)
            if dropped:
                logger.warning("must_touch_files entries dropped (not in KB): %s", dropped)
            if kept != must_touch:
                plan_payload = plan_payload.model_copy(update={"must_touch_files": kept})

        _log_plan_target_warnings(
            scenario=plan_payload.scenario,
            must_touch_files=list(plan_payload.must_touch_files or []),
            expected_new_files=list(plan_payload.expected_new_files or []),
        )
        return plan_payload

    def _generate_plan_with_openai(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        model_name: str,
        default_payload: GeneratedPlanPayload | None = None,
    ) -> GeneratedPlanPayload:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPS_AGENT_OPENAI_API_KEY is not configured")

        payload = {
            "model": model_name,
            "instructions": self._build_planning_instructions(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Actor: {actor_name}\n"
                                f"Scenario: {scenario}\n"
                                f"Request:\n{request_text}\n\n"
                                "Return only the structured execution plan."
                            ),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ops_agent_phase2_plan",
                    "strict": True,
                    "schema": GeneratedPlanPayload.model_json_schema(),
                }
            },
        }

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=external_http_timeout(self.settings.primary_agent_timeout_seconds)) as client:
            response = client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/responses",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()

        content = self._extract_response_text(response_payload)
        raw_plan = json.loads(content)
        if default_payload is not None:
            raw_plan = self._merge_plan_payload_with_defaults(raw_plan, default_payload)
        sanitized = self._sanitize_plan_payload(raw_plan)
        if default_payload is not None:
            sanitized = self._fill_empty_fields_from_default(sanitized, default_payload)
        return GeneratedPlanPayload.model_validate(sanitized)

    def _generate_plan_with_anthropic(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        model_name: str,
        default_payload: GeneratedPlanPayload | None = None,
    ) -> GeneratedPlanPayload:
        if not self.settings.anthropic_api_key:
            raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY is not configured")

        payload = {
            "model": model_name,
            "max_tokens": 8192,
            "system": self._build_planning_instructions(),
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Actor: {actor_name}\n"
                        f"Scenario: {scenario}\n"
                        f"Request:\n{request_text}\n\n"
                        "Return only valid JSON that matches the requested plan schema."
                    ),
                }
            ],
            "temperature": 0.2,
        }

        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        response = httpx.post(
            f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages",
            headers=headers,
            json=payload,
            timeout=external_http_timeout(max(self.settings.primary_agent_timeout_seconds, 120)),
        )
        response.raise_for_status()
        response_payload = response.json()

        content = self._extract_anthropic_content(response_payload)
        raw_plan = self._parse_json_content(content)
        if default_payload is not None:
            raw_plan = self._merge_plan_payload_with_defaults(raw_plan, default_payload)
        sanitized = self._sanitize_plan_payload(raw_plan)
        if default_payload is not None:
            sanitized = self._fill_empty_fields_from_default(sanitized, default_payload)
        return GeneratedPlanPayload.model_validate(sanitized)

    def _generate_plan_with_claude_code(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        default_payload: GeneratedPlanPayload | None = None,
    ) -> GeneratedPlanPayload:
        """Invoke Claude Code CLI (``claude --print``) as the planner."""
        claude_cmd = shutil.which(self.settings.claude_code_command)
        claude_args = self.settings.claude_code_args.split()
        if not claude_cmd:
            raise RuntimeError(f"Claude Code CLI not found: {self.settings.claude_code_command}")

        user_prompt = (
            f"Actor: {actor_name}\n"
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            "Return ONLY valid JSON that matches the requested plan schema. "
            "No markdown fences, no explanations, no commentary — pure JSON only."
        )

        system_prompt = self._build_planning_instructions()

        # Write prompt to a temp file and pipe via stdin file descriptor.
        # On Windows, large prompts fail when piped through npx.CMD using
        # subprocess.run(input=...) due to pipe buffer issues.
        full_prompt = (
            f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"
            f"USER REQUEST:\n{user_prompt}"
        )

        import tempfile
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        prompt_file.write(full_prompt)
        prompt_file.close()

        cmd = [
            claude_cmd, *claude_args,
            "--print",
            "-",  # read prompt from stdin
        ]

        env = {**os.environ}
        # Do NOT pass ANTHROPIC_API_KEY — Claude Code CLI should use its own
        # OAuth session, not the possibly-exhausted API key.
        env.pop("ANTHROPIC_API_KEY", None)
        # Windows: Claude Code CLI needs git-bash path
        if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
            configured = getattr(self.settings, "claude_code_git_bash_path", None)
            if configured:
                env["CLAUDE_CODE_GIT_BASH_PATH"] = configured
            else:
                for candidate in [
                    r"D:\Git\bin\bash.exe",
                    r"C:\Program Files\Git\bin\bash.exe",
                    r"C:\Program Files (x86)\Git\bin\bash.exe",
                ]:
                    if os.path.isfile(candidate):
                        env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                        break

        timeout_sec = int(self.settings.claude_code_timeout_seconds)
        try:
            with open(prompt_file.name, "r", encoding="utf-8") as stdin_f:
                proc = subprocess.Popen(
                    cmd,
                    stdin=stdin_f,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
                    raise RuntimeError(
                        f"Claude Code CLI timed out after {timeout_sec}s"
                    )
        finally:
            try:
                os.unlink(prompt_file.name)
            except OSError:
                pass

        result = type("Result", (), {
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        })()

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:500]
            raise RuntimeError(f"Claude Code CLI failed (rc={result.returncode}): {stderr}")

        content = (result.stdout or "").strip()
        if not content:
            raise RuntimeError("Claude Code CLI returned empty output")

        raw_plan = self._parse_json_content(content)
        if default_payload is not None:
            raw_plan = self._merge_plan_payload_with_defaults(raw_plan, default_payload)
        sanitized = self._sanitize_plan_payload(raw_plan)
        if default_payload is not None:
            sanitized = self._fill_empty_fields_from_default(sanitized, default_payload)
        return GeneratedPlanPayload.model_validate(sanitized)

    def _generate_plan_with_minimax(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        model_name: str,
        default_payload: GeneratedPlanPayload | None = None,
    ) -> GeneratedPlanPayload:
        if not self.settings.minimax_api_key:
            raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY is not configured")

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "name": "Ops Agent Planner",
                    "content": self._build_planning_instructions(),
                },
                {
                    "role": "user",
                    "name": actor_name,
                    "content": (
                        f"Scenario: {scenario}\n"
                        f"Request:\n{request_text}\n\n"
                        "Return only valid JSON that matches the requested plan schema."
                    ),
                },
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key}",
            "Content-Type": "application/json",
        }

        planner_timeout = max(
            self.settings.primary_agent_timeout_seconds,
            self.settings.minimax_planner_timeout_seconds,
        )

        with httpx.Client(timeout=external_http_timeout(planner_timeout)) as client:
            response = client.post(
                f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()

        content = self._extract_minimax_content(response_payload)
        raw_plan = self._parse_json_content(content)
        if default_payload is not None:
            raw_plan = self._merge_plan_payload_with_defaults(raw_plan, default_payload)
        sanitized = self._sanitize_plan_payload(raw_plan)
        if default_payload is not None:
            sanitized = self._fill_empty_fields_from_default(sanitized, default_payload)
        return GeneratedPlanPayload.model_validate(sanitized)

    @staticmethod
    def _extract_anthropic_content(response_payload: dict[str, Any]) -> str:
        content_blocks = response_payload.get("content")
        if not isinstance(content_blocks, list):
            raise RuntimeError("Anthropic response did not include content blocks")

        text_parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])

        content = "".join(text_parts).strip()
        if content:
            return content
        raise RuntimeError("Anthropic response did not include text content")

    @staticmethod
    def _extract_response_text(response_payload: dict[str, Any]) -> str:
        output = response_payload.get("output", [])
        for item in output:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        return text

        raise RuntimeError("Structured plan text was not present in the provider response")

    @staticmethod
    def _extract_minimax_content(response_payload: dict[str, Any]) -> str:
        choices = response_payload.get("choices")
        if not isinstance(choices, list):
            raise RuntimeError("MiniMax response did not include choices")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        raise RuntimeError("MiniMax response did not include assistant content")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        normalized = content.strip()
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
            normalized = re.sub(r"\s*```$", "", normalized)
        parsed = json.loads(normalized)
        if not isinstance(parsed, dict):
            raise RuntimeError("Planner response did not return a JSON object.")
        return parsed

    @staticmethod
    def _merge_plan_payload_with_defaults(
        payload: dict[str, Any],
        default_payload: GeneratedPlanPayload,
    ) -> dict[str, Any]:
        merged = default_payload.model_dump(mode="python")
        raw = dict(payload)

        for field_name in (
            "objective",
            "request_summary",
            "scenario",
            "change_summary",
            "change_explanation",
            "risk_level",
        ):
            value = raw.get(field_name)
            if isinstance(value, str) and value.strip():
                merged[field_name] = value

        # NOTE: requires_approval is NOT merged from LLM output — it is always
        # determined by the tool permission policy in the base plan.  Allowing
        # the LLM to set requires_approval=true would re-introduce approval
        # gates that the admin has intentionally disabled.

        for field_name in ("assumptions", "missing_information", "approval_reasons"):
            value = raw.get(field_name)
            if isinstance(value, list) and value:
                merged[field_name] = value

        for field_name in ("affected_code_locations", "must_touch_files", "expected_new_files", "tools", "steps", "acceptance_tests"):
            value = raw.get(field_name)
            if isinstance(value, list) and value:
                merged[field_name] = value

        final_output_contract = raw.get("final_output_contract")
        if isinstance(final_output_contract, dict) and final_output_contract:
            merged["final_output_contract"] = final_output_contract

        return merged

    @staticmethod
    def _fill_empty_fields_from_default(
        sanitized: dict[str, Any],
        default_payload: GeneratedPlanPayload,
    ) -> dict[str, Any]:
        """After sanitize, if required list fields are empty, pull them from default.

        Sanitize filters out malformed steps/tools from LLM output; if that leaves
        the lists empty, Pydantic validation would fail (steps requires min 1).
        Fall back to the scenario's default plan so the pipeline always has a
        working skeleton even when the LLM produces only partial content.
        """
        defaults = default_payload.model_dump(mode="python")
        for field_name in ("steps", "tools"):
            current = sanitized.get(field_name)
            if not isinstance(current, list) or not current:
                sanitized[field_name] = defaults.get(field_name, [])
        return sanitized

    @staticmethod
    def _sanitize_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(payload)

        def required_text(field_name: str, fallback: str) -> str:
            raw_value = sanitized.get(field_name)
            if isinstance(raw_value, str) and raw_value.strip():
                return " ".join(raw_value.strip().split())
            return fallback

        sanitized["objective"] = _trim_text(
            required_text("objective", "Generate an execution plan for the request."),
            limit=320,
        )
        sanitized["request_summary"] = _trim_text(
            required_text("request_summary", sanitized["objective"]),
            limit=220,
        )
        sanitized["scenario"] = _trim_text(
            required_text("scenario", "process_question"),
            limit=64,
        )
        sanitized["change_summary"] = _trim_text(
            required_text("change_summary", sanitized["objective"]),
            limit=320,
        )
        sanitized["change_explanation"] = _trim_text(
            required_text("change_explanation", sanitized["request_summary"]),
            limit=1200,
        )

        risk_level = str(sanitized.get("risk_level") or "").strip().lower()
        sanitized["risk_level"] = risk_level if risk_level in {"low", "medium", "high"} else "medium"
        sanitized["requires_approval"] = bool(sanitized.get("requires_approval"))

        for field_name, limit in PLAN_STRING_LIST_LIMITS.items():
            raw_values = sanitized.get(field_name)
            if not isinstance(raw_values, list):
                sanitized[field_name] = []
                continue
            sanitized[field_name] = _trim_string_list(
                [value for value in raw_values if isinstance(value, str)],
                limit=limit,
            )

        for field_name, limit in (("must_touch_files", 12), ("expected_new_files", 8)):
            raw_values = sanitized.get(field_name)
            if not isinstance(raw_values, list):
                sanitized[field_name] = []
                continue
            sanitized[field_name] = _trim_string_list(
                [value for value in raw_values if isinstance(value, str)],
                limit=limit,
            )

        # 2026-05-10 Class E counter-measure at planner level: for code-
        # change scenarios (jira_issue_develop / bug_fix), test files do
        # NOT belong in must_touch_files. The fix lives in source code;
        # tests belong in their own subtree and the SWE-bench
        # FAIL_TO_PASS suite is pre-existing. When the planner emits
        # tests/* paths in must_touch the model takes that as
        # permission to write a test as the "fix" (regression v8 task 4
        # django-12284: planner emitted ['tests/.../tests.py', ...,
        # 'django/db/models/enums.py']; model produced test-only diff).
        if sanitized.get("scenario") in {"jira_issue_develop", "bug_fix"}:
            sanitized["must_touch_files"] = [
                p for p in sanitized.get("must_touch_files", [])
                if not _looks_like_test_path(p)
            ]

        if sanitized["scenario"] == "process_question":
            sanitized["missing_information"] = []
            sanitized["requires_approval"] = False

        raw_locations = sanitized.get("affected_code_locations")
        locations: list[dict[str, Any]] = []
        if isinstance(raw_locations, list):
            for raw_location in raw_locations:
                if not isinstance(raw_location, dict):
                    continue
                source_name = raw_location.get("source_name")
                relative_path = raw_location.get("relative_path")
                reason = raw_location.get("reason")
                if not all(isinstance(value, str) and value.strip() for value in (source_name, relative_path, reason)):
                    continue
                entry: dict[str, Any] = {
                    "source_name": _trim_text(source_name, limit=80),
                    "relative_path": _trim_text(relative_path, limit=260),
                    "reason": _trim_text(reason, limit=240),
                }
                if isinstance(raw_location.get("line_start"), int) and raw_location["line_start"] > 0:
                    entry["line_start"] = raw_location["line_start"]
                if isinstance(raw_location.get("line_end"), int) and raw_location["line_end"] > 0:
                    entry["line_end"] = raw_location["line_end"]
                locations.append(entry)
                if len(locations) >= 8:
                    break
        sanitized["affected_code_locations"] = locations

        raw_tools = sanitized.get("tools")
        tools: list[dict[str, Any]] = []
        if isinstance(raw_tools, list):
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, dict):
                    continue
                tool_name = raw_tool.get("tool_name")
                permission_category = raw_tool.get("permission_category")
                purpose = raw_tool.get("purpose")
                if not all(isinstance(value, str) and value.strip() for value in (tool_name, permission_category, purpose)):
                    continue
                tools.append(
                    {
                        "tool_name": _trim_text(tool_name, limit=120),
                        "permission_category": permission_category.strip(),
                        "purpose": _trim_text(purpose, limit=200),
                    }
                )
                if len(tools) >= 6:
                    break
        sanitized["tools"] = tools

        raw_steps = sanitized.get("steps")
        steps: list[dict[str, Any]] = []
        if isinstance(raw_steps, list):
            for index, raw_step in enumerate(raw_steps, start=1):
                if not isinstance(raw_step, dict):
                    continue
                title = raw_step.get("title")
                expected_output = raw_step.get("expected_output")
                success_criteria = raw_step.get("success_criteria")
                if not all(isinstance(value, str) and value.strip() for value in (title, expected_output, success_criteria)):
                    continue
                kind = str(raw_step.get("kind") or "").strip().lower()
                owner_role = str(raw_step.get("owner_role") or "").strip().lower()
                steps.append(
                    {
                        "step_id": _trim_text(
                            (
                                str(raw_step.get("step_id")).strip()
                                if isinstance(raw_step.get("step_id"), str) and str(raw_step.get("step_id")).strip()
                                else f"step_{index}"
                            ),
                            limit=64,
                        ),
                        "title": _trim_text(title, limit=200),
                        "kind": kind if kind in {"analysis", "knowledge", "action", "review"} else "analysis",
                        "owner_role": owner_role if owner_role in {"primary", "planner", "knowledge", "action", "reviewer", "system"} else "planner",
                        "depends_on": _trim_string_list(
                            [value for value in raw_step.get("depends_on", []) if isinstance(value, str)]
                            if isinstance(raw_step.get("depends_on"), list)
                            else [],
                            limit=8,
                        ),
                        "tool_name": (
                            str(raw_step.get("tool_name")).strip()
                            if isinstance(raw_step.get("tool_name"), str) and str(raw_step.get("tool_name")).strip()
                            else None
                        ),
                        "expected_output": _trim_text(expected_output, limit=240),
                        "success_criteria": _trim_text(success_criteria, limit=240),
                    }
                )
                if len(steps) >= 10:
                    break
        sanitized["steps"] = steps

        # Acceptance tests (Tier 1.3) — validate each entry has a known
        # kind. Drop entries with unknown kind rather than fail-closed,
        # so older planners that don't emit this field stay compatible.
        _ALLOWED_AC_KINDS = {
            "diff_contains_pattern",
            "diff_contains_pattern_in_file",
            "function_signature_unchanged",
            "function_signature_changed",
            "no_new_file_outside",
            "import_added",
            "forbids_pattern_in_diff",
            "test_must_reference_existing_symbol",
        }
        raw_acceptance = sanitized.get("acceptance_tests")
        acceptance: list[dict[str, Any]] = []
        if isinstance(raw_acceptance, list):
            for raw_test in raw_acceptance:
                if not isinstance(raw_test, dict):
                    continue
                kind = str(raw_test.get("kind") or "").strip()
                if kind not in _ALLOWED_AC_KINDS:
                    continue
                entry: dict[str, Any] = {
                    "kind": kind,
                    "pattern": _trim_text(str(raw_test.get("pattern") or ""), limit=240),
                    "file": (
                        _trim_text(str(raw_test.get("file")), limit=400)
                        if isinstance(raw_test.get("file"), str) and str(raw_test.get("file")).strip()
                        else None
                    ),
                    "function": (
                        _trim_text(str(raw_test.get("function")), limit=200)
                        if isinstance(raw_test.get("function"), str) and str(raw_test.get("function")).strip()
                        else None
                    ),
                    "scope": (
                        _trim_text(str(raw_test.get("scope")), limit=400)
                        if isinstance(raw_test.get("scope"), str) and str(raw_test.get("scope")).strip()
                        else None
                    ),
                    "rationale": _trim_text(str(raw_test.get("rationale") or ""), limit=240),
                }
                acceptance.append(entry)
                if len(acceptance) >= 10:
                    break
        sanitized["acceptance_tests"] = acceptance

        raw_contract = sanitized.get("final_output_contract")
        if isinstance(raw_contract, dict):
            contract_type = raw_contract.get("type")
            required_fields = raw_contract.get("required_fields")
            sanitized["final_output_contract"] = {
                "type": contract_type.strip() if isinstance(contract_type, str) and contract_type.strip() else "structured_result",
                "required_fields": _trim_string_list(
                    [value for value in required_fields if isinstance(value, str)] if isinstance(required_fields, list) else [],
                    limit=12,
                )
                or ["status"],
            }
        return sanitized

    @staticmethod
    def _build_planning_instructions() -> str:
        return (
            "You are the planner role for an enterprise operations console. "
            "You must generate a structured execution plan for a single-runtime workflow with planner, reviewer, and action stages. "
            "The request may include a Semantic Translation JSON block, optional Jira Issue Context, and optional Planning Repository Context. "
            "Use those as grounded context instead of reinterpreting the request loosely. "
            "Only these tool actions exist: knowledge.search, slack.post_message, jira.get_issue, jira.create_issue, jira.add_comment, jira.transition_issue, internal_api.request, internal_db.query, codegen.generate_patch. "
            "Permission categories are read_only, write, and approval_required. "
            "If the scenario is slack_message, the only tool is slack.post_message. "
            "If the scenario is jira_issue_plan, the only tool is jira.get_issue. "
            "If the scenario is jira_issue_create, the only tool is jira.create_issue. "
            "If the scenario is jira_issue_writeback, the allowed tools are jira.add_comment and jira.transition_issue. "
            "If the scenario is jira_issue_develop, the only tool is codegen.generate_patch. "
            "For jira_issue_develop, produce exactly these four steps in order: "
            "(1) analyze the Jira issue and identify affected files, owner_role=planner, kind=analysis, tool_name=null; "
            "(2) generate the code patch, owner_role=action, kind=action, tool_name=codegen.generate_patch; "
            "(3) apply the patch and run tests in sandbox, owner_role=action, kind=action, tool_name=null; "
            "(4) review the generated changes, owner_role=reviewer, kind=review, tool_name=null. "
            "If the scenario is internal_api_request or action_with_approval, the only tool is internal_api.request. "
            "If the scenario is internal_db_query, the only tool is internal_db.query. "
            "If the scenario is process_question, the only tool is knowledge.search and "
            "requires_approval must be false. "
            "Always fill change_summary and change_explanation in natural language. "
            "Planning Repository Context contains GROUNDING citations from semantic search \u2014 these are files that ranked high by relevance, "
            "NOT necessarily files you should change. Treat citations as read-only background context. "
            "For affected_code_locations, output ONLY files that will actually be created or modified by this task: "
            "- If the user request asks to CREATE new files, list the NEW file paths (infer from the request text, e.g. filenames mentioned like 'database.rules.json'). "
            "- If the user request asks to MODIFY existing files, list the target paths (which may or may not overlap with citations). "
            "- If unsure, leave affected_code_locations empty rather than copying citations. "
            "For must_touch_files, populate it when the task will edit one or more EXISTING files in the repository to satisfy the request, "
            "AND repository retrieval has located those files. The trigger is not just destructive verbs \u2014 also \"add to existing\", "
            "\"integrate into\", \"extend\", \"embed in\", \"wire up\", or any modification of an existing component. "
            "Populate must_touch_files when ALL of these hold: "
            "(a) The patch will be invalid if those files are not modified. "
            "(b) Repository retrieval has located one or more citation files that demonstrably contain the targeted symbol/string/component or the natural extension point for the request. "
            "(c) The task is jira_issue_develop, bug_fix, refactor, or feature-on-existing-file. "
            "List the relative paths from retrieval citations that hold the existing code that must be edited or extended. "
            "Leave must_touch_files empty ONLY for: "
            "- process_question (knowledge Q&A) "
            "- pure scaffolding tasks where every change is in new files (use expected_new_files instead) "
            "- jira_issue_plan (plan-only scenarios) "
            "- internal_db_query / internal_api_request / slack_message. "
            "DO NOT include test files (paths under tests/, test_*.py, *_test.py, conftest.py) in must_touch_files for jira_issue_develop or bug_fix scenarios. "
            "The fix lives in source code, not in a new or edited test file. Editing a test file as 'the fix' is a hallucination pattern (Class E); the harness will strip such entries from must_touch_files automatically. "
            "For expected_new_files, list any files that MUST BE CREATED to satisfy the task and that do not yet exist in the repository. "
            "Examples: a new Firebase rules file (database.rules.json), a new config file (firebase.json), a new migration, a new schema, a new test fixture. "
            "Infer new file paths from the request text when the task is clearly asking for a new artifact (keywords: create, add new file, write a new, set up, introduce). "
            "Do NOT include existing files (those belong in must_touch_files). "
            "Do NOT pad the list with files that are only nice-to-have \u2014 only files the task cannot be considered complete without. "
            "Leave expected_new_files empty when the task only modifies existing code. "
            "Return 3 to 5 short steps (exactly 4 for jira_issue_develop). Each step owner_role should be one of primary, planner, knowledge, action, reviewer. "
            "EVERY step MUST include all of these non-empty string fields: step_id, title, kind, owner_role, expected_output, success_criteria. "
            "Steps missing any of these fields will be discarded. "
            "Do not invent extra systems, tools, or business modules. "
            "For jira_issue_develop and bug_fix scenarios, populate acceptance_tests with 1-3 STRUCTURAL assertions about what the resulting patch must contain. "
            "These are checked against the raw diff text — they are not LLM judgements. The reviewer rejects the patch if any test fails. "
            "Only emit a test if you can already name the symbol or pattern from the issue text + repository context. If you cannot, leave acceptance_tests empty rather than guess. "
            "Each entry has fields: kind, pattern, file (optional), function (optional), scope (optional, glob), rationale (short reason). "
            "Allowed kinds, pick the simplest that fits: "
            "(a) diff_contains_pattern — the unified diff must contain a regex/literal pattern in any added line. Example: kind=diff_contains_pattern, pattern=\"def fix_arithmetic\", rationale=\"adds the missing helper named in the issue\". "
            "(b) diff_contains_pattern_in_file — same as (a) but pattern must appear inside hunks for `file`. Use this when you can already name the file from must_touch_files. "
            "(c) function_signature_unchanged — kind, file, function. Use when the issue says \"do not change the API\" or \"keep backward compatibility\". "
            "(d) function_signature_changed — kind, file, function. Use when the issue requires a signature change (add/remove a parameter). "
            "(e) no_new_file_outside — kind, scope (glob). Use when the issue scopes the change to a directory. Example: scope=\"django/db/models/**\" — patch must not create files outside that subtree. "
            "(f) import_added — kind, file, pattern (the imported name). Use when the fix demonstrably requires a new import. "
            "(g) forbids_pattern_in_diff — kind, pattern (regex), optional file. Use to BAN a shape that would indicate a hallucinated bypass. Example for an ORM/query bug where the fix must edit executable code (not introduce a new flag): kind=forbids_pattern_in_diff, pattern=\"^[A-Z_]+ = (True|False)$\", rationale=\"this issue is in query-construction logic, not in module-level configuration; a new boolean settings flag is not a valid fix\". "
            "(h) test_must_reference_existing_symbol — kind, optional scope (default tests/). Catches \"new test only validates a freshly-invented symbol\" patterns. Emit it whenever the bug class makes self-justifying test files plausible. "
            "DO NOT emit tests like \"all tests pass\" or \"behavior is correct\" — those are not structural. Tests must be checkable against diff text alone. "
            "DO NOT emit acceptance_tests for process_question, slack_message, jira_issue_plan, jira_issue_create, jira_issue_writeback, internal_api_request, internal_db_query — leave the list empty for those scenarios."
        )


class ActionAgent:
    CHANNEL_PATTERN = re.compile(r"(#[a-z0-9._-]+)", re.IGNORECASE)
    PATH_PATTERN = re.compile(r"(/[a-zA-Z0-9._~!$&'()*+,;=:@/%-]+)")
    SQL_PATTERN = re.compile(r"\b(select|with)\b[\s\S]*", re.IGNORECASE)
    JIRA_PROJECT_PATTERN = re.compile(r"\bproject\s+([A-Z][A-Z0-9]{1,9})\b")
    JIRA_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b", re.IGNORECASE)
    SLACK_PREFIX_PATTERN = re.compile(
        r"^(send|post|notify|announce)\b.*?(to\s+)?(#[a-z0-9._-]+)?\s*:?\s*",
        re.IGNORECASE,
    )
    JIRA_PREFIX_PATTERN = re.compile(
        r"^(create|open|file)\s+(a\s+)?jira\s+(bug|story|task|issue)\s+(for|about)\s+",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def build_payload(
        self,
        *,
        task_id: str,
        request_text: str,
        scenario: str,
        semantic_translation: GeneratedSemanticTranslation | None = None,
    ) -> dict[str, Any]:
        normalized = " ".join(request_text.strip().split())

        if scenario == "slack_message":
            channel_match = self.CHANNEL_PATTERN.search(request_text)
            channel = channel_match.group(1) if channel_match else self.settings.slack_default_channel
            message_text = (
                semantic_translation.normalized_request
                if semantic_translation and semantic_translation.normalized_request
                else normalized
            )
            if channel_match:
                after_channel = request_text[channel_match.end() :].strip(" :,-")
                if after_channel:
                    message_text = " ".join(after_channel.split())
            stripped_prefix = self.SLACK_PREFIX_PATTERN.sub("", message_text).strip()
            if stripped_prefix:
                message_text = stripped_prefix
            return {
                "channel": channel,
                "text": message_text,
                "task_id": task_id,
            }

        if scenario == "jira_issue_create":
            lowered = request_text.lower()
            issue_type = (
                "Bug"
                if re.search(r"\bbug\b", lowered)
                else "Story"
                if re.search(r"\bstory\b", lowered)
                else self.settings.jira_issue_type
            )
            project_match = self.JIRA_PROJECT_PATTERN.search(request_text)
            project_key = project_match.group(1) if project_match else self.settings.jira_project_key
            summary_seed = (
                semantic_translation.objective
                if semantic_translation and semantic_translation.objective
                else self.JIRA_PREFIX_PATTERN.sub("", normalized).strip() or normalized
            )
            summary = summary_seed[:120]
            return {
                "project_key": project_key,
                "summary": summary,
                "description": semantic_translation.normalized_request if semantic_translation else request_text,
                "issue_type": issue_type,
                "task_id": task_id,
            }

        if scenario == "jira_issue_plan":
            issue_reference = extract_jira_issue_reference(request_text)
            return {
                "issue_key": (
                    semantic_translation.issue_key
                    if semantic_translation and semantic_translation.issue_key
                    else issue_reference.issue_key if issue_reference else ""
                ),
                "task_id": task_id,
            }

        if scenario == "jira_issue_writeback":
            issue_reference = extract_jira_issue_reference(request_text)
            issue_key = (
                semantic_translation.issue_key
                if semantic_translation and semantic_translation.issue_key
                else issue_reference.issue_key if issue_reference else ""
            )

            transition_name = ""
            transition_keywords = {
                "in progress": "In Progress",
                "to do": "To Do",
                "done": "Done",
                "in review": "In Review",
                "complete": "Done",
                "close": "Done",
                "reopen": "To Do",
            }
            lowered = request_text.lower()
            for keyword, mapped_name in transition_keywords.items():
                if keyword in lowered:
                    transition_name = mapped_name
                    break

            if semantic_translation and semantic_translation.objective:
                obj_lower = semantic_translation.objective.lower()
                for keyword, mapped_name in transition_keywords.items():
                    if keyword in obj_lower:
                        transition_name = mapped_name
                        break

            comment_text = ""
            comment_markers = [
                "comment:",
                "Comment:",
                "Comment: ",
                "Remark:",
                "Remark: ",
                "note:",
                "comment ",
                "Comment ",
                "Remark ",
                "note ",
            ]
            for marker in comment_markers:
                idx = lowered.find(marker)
                if idx >= 0:
                    comment_text = request_text[idx + len(marker) :].strip()
                    break

            if not comment_text and semantic_translation and semantic_translation.normalized_request:
                comment_text = semantic_translation.normalized_request

            return {
                "issue_key": issue_key,
                "transition_name": transition_name,
                "text": comment_text,
                "task_id": task_id,
            }

        if scenario in {"internal_api_request", "action_with_approval"}:
            path_match = self.PATH_PATTERN.search(request_text)
            method = "POST" if any(word in request_text.lower() for word in ("create", "post", "send", "trigger")) else "GET"
            if any(word in request_text.lower() for word in ("update", "patch")):
                method = "PATCH"
            if any(word in request_text.lower() for word in ("delete", "remove")):
                method = "DELETE"
            return {
                "method": method,
                "path": path_match.group(1) if path_match else "/",
                "body": {"request_text": request_text, "task_id": task_id},
            }

        if scenario == "internal_db_query":
            sql_match = self.SQL_PATTERN.search(request_text)
            sql = sql_match.group(0).strip() if sql_match else "SELECT 1 AS status"
            return {
                "sql": sql,
                "params": {},
                "task_id": task_id,
            }

        translated_queries = semantic_translation.search_queries if semantic_translation else []
        # Detect language from original request_text, not translated query
        from app.orchestrator.service import detect_user_language
        user_lang = detect_user_language(request_text)
        return {
            "query": translated_queries[0] if translated_queries else request_text,
            "top_k": 4,
            "semantic_translation": semantic_translation.model_dump(mode="json") if semantic_translation else None,
            "language": user_lang,
        }


class ReviewerAgent:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.registry = ToolRegistry(self.settings)

    def review_plan(
        self,
        *,
        task_id: str,
        actor_name: str,
        plan: GeneratedPlan,
    ) -> ReviewGenerationResult:
        findings: list[ReviewFinding] = []
        missing_information = list(plan.missing_information)
        policy_checks: list[PolicyCheck] = []
        approval_requirements: list[ApprovalRequirement] = []

        if missing_information:
            findings.append(
                ReviewFinding(
                    code="missing_information",
                    severity="error",
                    message="Planner left required information unresolved.",
                    field="missing_information",
                )
            )

        for tool in plan.tools:
            try:
                effective_definition = self.registry.get_definition(tool.tool_name)
            except ValueError:
                findings.append(
                    ReviewFinding(
                        code="unsupported_tool",
                        severity="error",
                        message=f"Planner referenced unsupported tool {tool.tool_name}.",
                        field="tools",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name=f"tool_scope:{tool.tool_name}",
                        status="failed",
                        detail=f"{tool.tool_name} is not registered in the current tool runtime.",
                    )
                )
                continue

            if tool.permission_category != effective_definition.permission_category:
                findings.append(
                    ReviewFinding(
                        code="tool_permission_mismatch",
                        severity="warning",
                        message=f"Planner used {tool.permission_category.value} but the runtime maps {tool.tool_name} to {effective_definition.permission_category.value}.",
                        field="tools.permission_category",
                    )
                )

            if not effective_definition.enabled:
                findings.append(
                    ReviewFinding(
                        code="tool_not_configured",
                        severity="error",
                        message=f"{tool.tool_name} is registered but not enabled in the current environment.",
                        field="tools",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name=f"tool_enabled:{tool.tool_name}",
                        status="failed",
                        detail=f"{tool.tool_name} is missing required runtime configuration.",
                    )
                )
                continue

            policy_checks.append(
                PolicyCheck(
                    name=f"tool_scope:{tool.tool_name}",
                    status="passed",
                    detail=f"{tool.tool_name} is registered under {effective_definition.provider_name} with {effective_definition.permission_category.value} access.",
                )
            )

            if effective_definition.permission_category == ToolPermissionCategory.APPROVAL_REQUIRED:
                approval_requirements.append(
                    ApprovalRequirement(
                        action_name=tool.tool_name,
                        reason="The planned tool is classified as approval-required.",
                        approver_role="team_lead",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="approval_gate",
                        status="warning",
                        detail=f"{tool.tool_name} requires manual approval before execution.",
                    )
                )

        if plan.requires_approval and not approval_requirements:
            approval_requirements.append(
                ApprovalRequirement(
                    action_name=plan.tools[0].tool_name,
                    reason="Planner marked the task as approval-required.",
                    approver_role="team_lead",
                )
            )

        has_blocking_issues = any(finding.severity == "error" for finding in findings) or any(
            check.status == "failed" for check in policy_checks
        )

        if missing_information:
            verdict = "needs_info"
            ready_for_execution = False
            recommended_status = TaskStatus.FAILED.value
            summary = "Reviewer blocked execution because required information is missing."
        elif has_blocking_issues:
            verdict = "rejected"
            ready_for_execution = False
            recommended_status = TaskStatus.FAILED.value
            summary = "Reviewer rejected the plan because tool scope, configuration, or policy checks failed."
        elif approval_requirements:
            verdict = "requires_approval"
            ready_for_execution = False
            recommended_status = TaskStatus.AWAITING_APPROVAL.value
            summary = "Reviewer accepted the plan but requires manual approval before execution."
        else:
            verdict = "approved"
            ready_for_execution = True
            recommended_status = TaskStatus.EXECUTING.value
            summary = f"Reviewer approved the plan for {actor_name} and execution can proceed."

        review = _materialize_review(
            payload=GeneratedReviewPayload(
                review_stage="pre_execution",
                verdict=verdict,
                ready_for_execution=ready_for_execution,
                summary=summary,
                findings=findings,
                missing_information=missing_information,
                policy_checks=policy_checks,
                approval_requirements=approval_requirements,
                recommended_status=recommended_status,
            ),
            task_id=task_id,
            plan_id=plan.plan_id,
            provider={"name": "mock", "mode": "deterministic_reviewer"},
        )
        return ReviewGenerationResult(review=review, provider_name="mock")

    def review_output(
        self,
        *,
        task_id: str,
        plan: GeneratedPlan,
        result: dict[str, Any],
    ) -> ReviewGenerationResult:
        missing_fields = [
            field
            for field in plan.final_output_contract.required_fields
            if field not in result or result.get(field) in (None, "", [])
        ]

        findings: list[ReviewFinding] = []
        if missing_fields:
            findings.append(
                ReviewFinding(
                    code="output_contract_missing_fields",
                    severity="error",
                    message="Execution output did not satisfy the final output contract.",
                    field="final_output_contract.required_fields",
                )
            )

        summary = "Reviewer approved the execution output."
        recommended_status = TaskStatus.COMPLETED.value
        verdict = "approved"
        policy_checks = [
            PolicyCheck(
                name="output_contract",
                status="failed" if missing_fields else "passed",
                detail=(
                    f"Missing required fields: {', '.join(missing_fields)}"
                    if missing_fields
                    else "Execution output satisfied the required output contract."
                ),
            )
        ]

        if plan.final_output_contract.type == "knowledge_answer":
            citations = result.get("citations")
            citation_count = len(citations) if isinstance(citations, list) else 0
            answer_trace = result.get("answer_trace") if isinstance(result.get("answer_trace"), dict) else {}
            hallucination_risk = answer_trace.get("hallucination_risk")
            token_coverage = answer_trace.get("token_coverage")
            top_score = answer_trace.get("top_score")
            if citation_count == 0:
                verdict = "rejected"
                recommended_status = TaskStatus.FAILED.value
                findings.append(
                    ReviewFinding(
                        code="knowledge_missing_citations",
                        severity="error",
                        message="Knowledge answer did not include any repository citations.",
                        field="citations",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="citation_grounding",
                        status="failed",
                        detail="No citations were returned for the knowledge answer.",
                    )
                )
                summary = "Reviewer rejected the knowledge answer because it was not grounded in citations."
            elif isinstance(token_coverage, (int, float)) and float(token_coverage) < 0.35:
                findings.append(
                    ReviewFinding(
                        code="knowledge_token_coverage_low",
                        severity="warning",
                        message="Repository matches cover too little of the query vocabulary.",
                        field="answer_trace.token_coverage",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="retrieval_relevance",
                        status="warning",
                        detail=f"Token coverage is {float(token_coverage):.2f}, so the repository match may be incomplete.",
                    )
                )
                summary = "Reviewer approved the knowledge answer with low retrieval coverage."
            elif hallucination_risk == "high":
                findings.append(
                    ReviewFinding(
                        code="knowledge_hallucination_risk_high",
                        severity="warning",
                        message="Knowledge answer has elevated hallucination risk due to weak retrieval coverage.",
                        field="answer_trace.hallucination_risk",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="citation_grounding",
                        status="warning",
                        detail="Citations exist, but retrieval confidence is weak and hallucination risk is high.",
                    )
                )
                summary = "Reviewer approved the knowledge answer with elevated hallucination risk."
            elif isinstance(top_score, (int, float)) and float(top_score) < 10:
                findings.append(
                    ReviewFinding(
                        code="knowledge_top_score_weak",
                        severity="warning",
                        message="The highest-ranked repository citation is only a weak relevance match.",
                        field="answer_trace.top_score",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="retrieval_relevance",
                        status="warning",
                        detail=f"Top retrieval score is {float(top_score):.2f}, which is weaker than the preferred threshold.",
                    )
                )
                summary = "Reviewer approved the knowledge answer with weak top-citation relevance."
            else:
                policy_checks.append(
                    PolicyCheck(
                        name="citation_grounding",
                        status="passed",
                        detail=f"Knowledge answer is grounded by {citation_count} repository citation(s).",
                    )
                )
                policy_checks.append(
                    PolicyCheck(
                        name="retrieval_relevance",
                        status="passed",
                        detail=(
                            f"Token coverage is {float(token_coverage):.2f} and top retrieval score is "
                            f"{float(top_score):.2f}."
                            if isinstance(token_coverage, (int, float)) and isinstance(top_score, (int, float))
                            else "Retrieval relevance metrics are within the expected range."
                        ),
                    )
                )

        review = _materialize_review(
            payload=GeneratedReviewPayload(
                review_stage="post_execution",
                verdict="rejected" if missing_fields else verdict,
                ready_for_execution=False,
                summary=(
                    "Reviewer rejected the execution output because required fields are missing."
                    if missing_fields
                    else summary
                ),
                findings=findings,
                missing_information=[],
                policy_checks=policy_checks,
                approval_requirements=[],
                recommended_status=TaskStatus.FAILED.value if missing_fields else recommended_status,
            ),
            task_id=task_id,
            plan_id=plan.plan_id,
            provider={"name": "mock", "mode": "deterministic_reviewer"},
        )
        return ReviewGenerationResult(review=review, provider_name="mock")
