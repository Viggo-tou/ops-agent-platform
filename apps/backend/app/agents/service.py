from __future__ import annotations

import concurrent.futures
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
from app.core.enums import (
    EventSource,
    EventType,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.core.jira import extract_jira_issue_reference
from app.core.timeouts import external_http_timeout
from app.models.knowledge_document import KnowledgeDocument
from app.services.events import record_event
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


_ANDROID_MAP_PLAN_RE = re.compile(
    r"android_map_location|org\.osmdroid|osmdroid|mapview|map-based|map based|address picker|location picker|map-based address",
    re.IGNORECASE,
)


def _plan_mentions_android_map(payload: dict[str, Any]) -> bool:
    fields = (
        "objective",
        "request_summary",
        "change_summary",
        "change_explanation",
    )
    text = "\n".join(str(payload.get(field) or "") for field in fields)
    text += "\n" + "\n".join(str(v) for v in payload.get("assumptions") or [])
    return bool(_ANDROID_MAP_PLAN_RE.search(text))


def _normalize_library_acceptance_tests(
    acceptance: list[dict[str, Any]],
    *,
    plan_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if not acceptance or not _plan_mentions_android_map(plan_payload):
        return acceptance
    normalized: list[dict[str, Any]] = []
    replacement = "MapEventsOverlay|MapEventsReceiver|singleTapConfirmedHelper"
    for entry in acceptance:
        item = dict(entry)
        pattern = str(item.get("pattern") or "")
        if "setOnMapClickListener" in pattern:
            item["pattern"] = re.sub(
                r"\bsetOnMapClickListener\b",
                replacement,
                pattern,
            )
            rationale = str(item.get("rationale") or "").strip()
            note = (
                "OSMDroid MapView does not expose setOnMapClickListener; "
                "tap selection must use MapEventsOverlay/MapEventsReceiver."
            )
            item["rationale"] = _trim_text(
                f"{rationale} {note}".strip(),
                limit=240,
            )
        normalized.append(item)
    return normalized


def _demote_unsourced_android_map_new_files(
    sanitized: dict[str, Any],
) -> None:
    """Avoid broad helper-component creation for existing map integrations.

    Android map/location work usually has to land in existing KYC/signup
    forms. If the planner already selected multiple existing targets and the
    user/Jira-facing summary did not explicitly name a new file, treating a
    helper component as required creates an extra codegen batch and a
    cross-file consistency problem. Keep it as likely_touch context instead.
    """
    if sanitized.get("scenario") not in {"jira_issue_develop", "bug_fix"}:
        return
    if not _plan_mentions_android_map(sanitized):
        return
    expected_new = list(sanitized.get("expected_new_files") or [])
    must_touch = list(sanitized.get("must_touch_files") or [])
    if not expected_new or len(must_touch) < 2:
        return
    grounding_text = "\n".join(
        str(sanitized.get(field) or "")
        for field in ("objective", "request_summary")
    ).lower()
    kept: list[str] = []
    demoted: list[str] = []
    for path in expected_new:
        leaf = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if leaf and leaf in grounding_text:
            kept.append(path)
        else:
            demoted.append(path)
    if not demoted:
        return
    likely = list(sanitized.get("likely_touch_files") or [])
    for path in demoted:
        if path not in likely:
            likely.append(path)
    sanitized["expected_new_files"] = kept
    sanitized["likely_touch_files"] = _trim_string_list(likely, limit=12)


_CODE_CHANGE_SCENARIOS = frozenset({"jira_issue_develop", "bug_fix"})


def _collect_external_text(value: Any) -> list[str]:
    """Collect user/Jira-provided text for provenance checks.

    Do not feed planner-authored fields into this helper. It is specifically
    for source text that existed before the model wrote the plan.
    """
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict):
        parts: list[str] = []
        for child in value.values():
            parts.extend(_collect_external_text(child))
        return parts
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for child in value:
            parts.extend(_collect_external_text(child))
        return parts
    return []


def _expected_new_file_is_source_bound(path: str, source_text: str) -> bool:
    """Return True when a planned new path is explicitly grounded.

    Existing-feature tasks often tempt the planner to invent a helper
    component and then make downstream coverage require it. For mixed
    existing-file + new-file plans, accept a new file only when the original
    user/Jira text names the path or basename. Migration files are the one
    common generated-name exception: the exact timestamped filename is often
    impossible for the requester to know, but the request must still mention
    migrations.
    """
    norm = str(path or "").strip().replace("\\", "/").strip("/")
    if not norm:
        return False
    text = (source_text or "").casefold()
    if not text:
        return False
    path_lower = norm.casefold()
    leaf = path_lower.rsplit("/", 1)[-1]
    if path_lower in text or leaf in text:
        return True
    if "/migrations/" in f"/{path_lower}" and "migration" in text:
        return True
    return False


def _source_bind_expected_new_files(
    plan_payload: GeneratedPlanPayload,
    *,
    request_text: str,
    issue_context: dict[str, Any] | None = None,
) -> tuple[GeneratedPlanPayload, list[str]]:
    """Demote planner-invented new files on existing-code tasks.

    This is the general version of the P69-19 lesson: a planner may not
    create a new artifact requirement from its own prose. If the task already
    has existing must-touch files, any expected new file must be named by the
    requester/Jira source text, otherwise it becomes non-binding
    likely-touch context.
    """
    if plan_payload.scenario not in _CODE_CHANGE_SCENARIOS:
        return plan_payload, []

    expected_new = list(plan_payload.expected_new_files or [])
    must_touch = list(plan_payload.must_touch_files or [])
    if not expected_new or not must_touch:
        return plan_payload, []

    external_text = "\n".join(
        [request_text or "", *_collect_external_text(issue_context or {})]
    )
    kept: list[str] = []
    demoted: list[str] = []
    for path in expected_new:
        if _expected_new_file_is_source_bound(path, external_text):
            kept.append(path)
        else:
            demoted.append(path)

    if not demoted:
        return plan_payload, []

    demoted_norm = {p.replace("\\", "/").strip("/") for p in demoted}
    demoted_stems = {
        p.rsplit("/", 1)[-1].split(".", 1)[0].casefold()
        for p in demoted_norm
        if p.rsplit("/", 1)[-1].split(".", 1)[0]
    }
    kept_acceptance = []
    for test in list(plan_payload.acceptance_tests or []):
        test_file = str(getattr(test, "file", "") or "").replace("\\", "/").strip("/")
        pattern = str(getattr(test, "pattern", "") or "").casefold()
        if test_file in demoted_norm:
            continue
        if any(stem and stem in pattern for stem in demoted_stems):
            continue
        kept_acceptance.append(test)

    likely = list(plan_payload.likely_touch_files or [])
    for path in demoted:
        if path not in likely:
            likely.append(path)

    updated = plan_payload.model_copy(
        update={
            "expected_new_files": kept,
            "likely_touch_files": _trim_string_list(likely, limit=12),
            "acceptance_tests": kept_acceptance,
        }
    )
    return updated, demoted


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


def build_domain_fast_path_plan(
    *,
    task_id: str,
    request_text: str,
    scenario: str,
    matched_playbook: dict[str, Any] | None,
    semantic_translation: GeneratedSemanticTranslation | None = None,
    issue_context: dict[str, Any] | None = None,
    candidate_files: list[dict[str, Any]] | None = None,
) -> PlanGenerationResult | None:
    """Build a deterministic plan for domains already covered by a playbook.

    The fast path is intentionally narrow: it removes slow/fragile planner
    model calls only when the domain playbook already defines the contract and
    the normal preplan discovery has supplied candidate files. The ordinary
    orchestrator gates still run after this plan is materialized.
    """
    domain_id = str((matched_playbook or {}).get("id") or "").strip()
    if scenario != "jira_issue_develop" or domain_id not in {
        "android_map_location",
        "android_job_default_address",
    }:
        return None

    issue_text_parts = [request_text or ""]
    if isinstance(issue_context, dict):
        for key in ("summary", "description", "acceptance_criteria"):
            value = str(issue_context.get(key) or "").strip()
            if value:
                issue_text_parts.append(value)
    issue_text = "\n".join(part for part in issue_text_parts if part).strip()

    try:
        from app.services.domain_classifier import (
            synthesize_acceptance_tests_from_playbook,
            synthesize_must_touch_files_from_candidates,
        )

        acceptance_tests = synthesize_acceptance_tests_from_playbook(
            playbook=matched_playbook,
            acceptance_tests=[],
        )
        must_touch_files = synthesize_must_touch_files_from_candidates(
            playbook=matched_playbook,
            candidate_files=candidate_files or [],
            issue_text=issue_text,
            existing_must_touch=[],
            expected_new_files=[],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("domain fast-path synthesis failed: %s", exc)
        acceptance_tests = []
        must_touch_files = []

    try:
        from app.services.domain_classifier import classify_android_map_location_subtype

        domain_subtype = classify_android_map_location_subtype(issue_text)
    except Exception:  # noqa: BLE001
        domain_subtype = "generic_map_location"

    if domain_id == "android_job_default_address" or domain_subtype == "job_default_address":
        objective = (
            "Pre-fill the Android job creation location step from the user's "
            "saved home/account address."
        )
        change_summary = (
            "Load the saved user address into the job posting location state "
            "and keep later work-location edits on the job payload."
        )
        change_explanation = (
            "Read the current user's saved address from the existing session/"
            "Firebase profile path on a fresh job; populate locationAddress "
            "and map coordinates only when the user has not edited them; "
            "geocode the saved address off the main thread with Locale; "
            "preserve the existing JobPostingFlow map picker and job payload "
            "sink so updated work locations are saved to the job, not back to "
            "the user's home address."
        )
        assumptions = [
            "The job posting flow is the edit target, not signup/KYC profile editing.",
            "The user's saved home address already exists in the User profile fields.",
            "If no saved address exists, the current manual/map job-location flow remains usable.",
        ]
    else:
        objective = (
            "Implement the Android map-location address picker in the "
            "existing signup and KYC address flows."
        )
        change_summary = (
            "Add OSMDroid map address selection to existing Android forms."
        )
        change_explanation = (
            "Use OSMDroid MapView and MapEventsOverlay for tap selection; "
            "run Geocoder calls on Dispatchers.IO with Locale.getDefault(); "
            "sync selected coordinates and address fields into existing "
            "form state and persistence payloads; preserve existing "
            "Firebase loops and payload fields; avoid duplicate manual "
            "address inputs and avoid new helper files unless the source "
            "explicitly names them."
        )
        assumptions = [
            "The existing signup and KYC address screens are the edit targets.",
            "The project already provides OSMDroid dependencies.",
            "The generated patch should modify existing flows instead of creating a detached component.",
        ]

    base_payload = build_fallback_plan_payload(
        request_text,
        scenario=scenario,
        semantic_translation=semantic_translation,
        planning_knowledge=None,
        issue_context=issue_context,
    )
    payload_data = base_payload.model_dump(mode="python")
    payload_data.update(
        {
            "objective": objective,
            "change_summary": change_summary,
            "change_explanation": change_explanation,
            "assumptions": assumptions,
            "missing_information": [],
            "must_touch_files": must_touch_files,
            "expected_new_files": [],
            "acceptance_tests": acceptance_tests,
        }
    )
    payload = GeneratedPlanPayload.model_validate(payload_data)
    provider = {
        "name": "harness:android_map_location_plan",
        "model": "deterministic-v1",
        "domain_playbook_id": domain_id,
    }
    plan = _materialize_plan(
        payload=payload,
        task_id=task_id,
        provider=provider,
    )
    return PlanGenerationResult(
        plan=plan,
        provider_name="harness:android_map_location_plan",
        model_name="deterministic-v1",
        used_fallback=False,
        fallback_reason=None,
    )


class PlanTargetsDroppedToEmpty(Exception):
    """Phase 1.3 (2026-05-11): the planner emitted must_touch_files but
    every one of them was filtered out by KB validation, and there are
    no expected_new_files to compensate. Surfacing this distinct from
    a generic "empty plan" lets the operator see the real cause:
    wrong source / hallucinated paths / unsynced KB.
    """


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

    # ----- T-LEARNING-LOOP-V1 Phase 2 — planner failure-memory injection ---
    #
    # Before the planner LLM call, retrieve the top-3 most relevant
    # ``failure_observation`` rows from agent_memory and render them
    # into a ``prior_failures_block`` that gets appended to the user
    # message. The block is labeled "risk warnings only — not verified
    # fixes" so the LLM doesn't treat past failures as repair recipes.
    #
    # Scoring (per the ticket spec):
    #   task_family_match * 5  + scope_match * 3
    #   + keyword_similarity * 2 + trust_level_weight + recency_weight
    #
    # Trust ladder: verified > human_confirmed > auto_classified.
    # prompt_eligible whitelist enforced — rows lacking
    # ``planner_warning`` in their eligible list are dropped.
    #
    # Empty pool / no DB / disabled memory → returns "" and the planner
    # prompt is byte-identical to the pre-Phase-2 version.

    _SCOPE_FOR_PLANNER = (
        "gate:must_touch",
        "gate:contract_coverage",
        "gate:compile_repair",
        "provider:liveness",
        "provider:codegen",
        "review:semantic",
        "review:reservations",
    )

    _TRUST_WEIGHT = {
        "verified": 3.0,
        "human_confirmed": 2.0,
        "auto_classified": 1.0,
    }

    def _build_prior_failures_block(
        self,
        *,
        request_text: str,
        scenario: str,
        candidate_files: list[dict] | None,
        top_k: int = 3,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return (rendered_text, audit_payload) for planner injection.

        On any error or when memory is unavailable, returns ("", []).
        Audit payload is a list of dicts with ``memory_id``,
        ``failure_class``, ``task_family``, ``trust_level``, and
        ``score`` for the orchestrator's ``planner.failure_memory_injected``
        event.
        """
        if self.db is None or not bool(getattr(self.settings, "memory_enabled", True)):
            return "", []
        try:
            from app.services.failure_classifier import detect_task_family
            from app.services.memory import MemoryService

            inferred_family = detect_task_family(
                request_text=request_text,
                plan_json={"objective": request_text, "must_touch_files": [
                    str(c.get("path") or c.get("file") or "")
                    for c in (candidate_files or [])
                    if isinstance(c, dict)
                ]},
            )

            svc = MemoryService(self.db)
            candidates: list[Any] = []
            for scope in self._SCOPE_FOR_PLANNER:
                # Pull WITHOUT task_family filter so we still surface
                # cross-family observations when no family match exists.
                # We re-rank afterwards.
                got = svc.query(
                    scope=scope,
                    memory_kind="failure_observation",
                    prompt_context="planner_warning",
                    text_hint=(request_text or "")[:200],
                    top_n=10,
                )
                candidates.extend(got)
            # Deduplicate by memory id (an aggressive scope sweep can
            # return the same row twice if it lives at the boundary).
            seen: set[str] = set()
            unique: list[Any] = []
            for m in candidates:
                if m.id in seen:
                    continue
                seen.add(m.id)
                unique.append(m)
            if not unique:
                return "", []

            # Score per the ticket: family * 5 + scope_match * 3
            # + keyword_similarity * 2 + trust + recency.
            req_lower = (request_text or "").lower()
            scored: list[tuple[float, Any]] = []
            for m in unique:
                family_match = (
                    5.0 if (inferred_family and m.task_family == inferred_family) else 0.0
                )
                scope_match = 3.0 if m.scope in self._SCOPE_FOR_PLANNER else 0.0
                # Lightweight keyword overlap on observation text.
                obs_lower = (m.observation or "").lower()
                keyword_hits = sum(
                    1 for w in re.findall(r"[a-z][a-z0-9_]{3,}", req_lower)
                    if w in obs_lower
                )
                keyword_similarity = min(2.0, 0.5 * keyword_hits)
                trust = self._TRUST_WEIGHT.get(m.trust_level or "", 0.0)
                # Recency: rough 7-day half-life. Avoid datetime import
                # ordering surprises — older rows still surface, just lower.
                recency = 1.0
                try:
                    if m.created_at is not None:
                        from datetime import datetime, timezone, timedelta
                        age = datetime.now(timezone.utc) - (
                            m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=timezone.utc)
                        )
                        if age < timedelta(days=7):
                            recency = 1.5
                        elif age > timedelta(days=30):
                            recency = 0.5
                except Exception:  # noqa: BLE001
                    recency = 1.0
                score = family_match + scope_match + keyword_similarity + trust + recency
                scored.append((score, m))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            # Strict family filter: only surface rows whose task_family
            # matches the inferred family of the current request. Cross-
            # family observations introduce noise more often than they
            # produce signal (a Python refactor task shouldn't be warned
            # about Android map_location failures). If no family is
            # inferred, fall back to "scope-only" retrieval but require
            # a much higher score floor (verified trust + scope match +
            # keyword similarity together).
            if inferred_family:
                eligible = [(s, m) for (s, m) in scored if m.task_family == inferred_family]
                threshold = 1.0
            else:
                eligible = scored
                threshold = 7.0  # essentially: verified + scope + keywords
            chosen = eligible[: max(1, top_k)]
            if not chosen or chosen[0][0] < threshold:
                return "", []

            lines: list[str] = ["Relevant prior failure observations:"]
            audit_rows: list[dict[str, Any]] = []
            for idx, (score, m) in enumerate(chosen, start=1):
                lines.append(
                    f"{idx}. [{m.trust_level or 'auto_classified'}] "
                    f"{m.failure_class or 'unspecified'} / "
                    f"{m.task_family or 'cross-family'} "
                    f"(scope: {m.scope})"
                )
                # Observation = what happened; resolution = the lesson.
                # Render both because the planner needs the lesson but
                # the observation grounds it ("we have ev that...").
                obs_snip = (m.observation or "").splitlines()[0][:280]
                if obs_snip:
                    lines.append(f"   Observation: {obs_snip}")
                lesson_snip = " ".join((m.resolution or "").split())[:480]
                if lesson_snip:
                    lines.append(f"   Lesson for planning: {lesson_snip}")
                audit_rows.append({
                    "memory_id": m.id,
                    "failure_class": m.failure_class,
                    "task_family": m.task_family,
                    "trust_level": m.trust_level,
                    "score": round(float(score), 2),
                })
            lines.append("")
            lines.append(
                "Planner instruction: treat these as risk warnings only — "
                "they are NOT verified fixes. If relevant, strengthen "
                "must_touch_files, required_contracts, and acceptance_tests "
                "so the patch covers the surface the previous run missed."
            )
            return "\n".join(lines), audit_rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner failure-memory retrieval failed (non-fatal): %s", exc)
            return "", []

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
        candidate_files: list[dict] | None = None,
    ) -> PlanGenerationResult:
        """Generate a plan using the provider chain. On failure, cascade to the next provider.

        Phase B.2.a (2026-05-11): ``candidate_files`` is the pre-plan
        repository discovery result. Surfaced into the planner prompt
        so DeepSeek (which can't browse the repo) has a concrete file
        menu to pick must_touch_files from.
        """
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
        deepseek_api_key = getattr(self.settings, "deepseek_api_key", None)

        # Build ordered provider chain
        providers: list[str] = []
        if provider_mode == "auto":
            if shutil.which(self.settings.claude_code_command):
                providers.append("claude_code")
            if anthropic_api_key:
                providers.append("anthropic")
            if openai_api_key:
                providers.append("openai")
            if deepseek_api_key:
                providers.append("deepseek")
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

        # T-LEARNING-LOOP-V1 Phase 2 — retrieve failure_observation rows
        # ONCE, before the provider loop. The same block is presented to
        # whichever provider eventually answers, so the planner cannot
        # see different failure context depending on which fallback
        # provider fires. Empty-string return when memory is unavailable.
        prior_failures_block, _injected_audit = self._build_prior_failures_block(
            request_text=request_text,
            scenario=scenario,
            candidate_files=candidate_files,
        )
        if _injected_audit and self.db is not None:
            try:
                record_event(
                    self.db,
                    task_id=task_id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.PLANNING,
                    role=RoleName.PLANNER,
                    tool_name="planner.failure_memory_injected",
                    message=(
                        f"Injected {len(_injected_audit)} prior failure observation(s) "
                        "into planner prompt as risk warnings."
                    ),
                    payload={
                        "count": len(_injected_audit),
                        "memory_ids": [r["memory_id"] for r in _injected_audit],
                        "rows": _injected_audit,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "planner.failure_memory_injected event emit failed (non-fatal): %s",
                    exc,
                )

        last_error: str | None = None
        prompt_fingerprint = hashlib.sha256(f"{scenario}\n{request_text}".encode("utf-8")).hexdigest()[:10]
        for fallback_step, provider_name in enumerate(providers):
            started = time.perf_counter()
            try:
                if self.db is not None:
                    try:
                        record_event(
                            self.db,
                            task_id=task_id,
                            event_type=EventType.TOOL_CALL_REQUESTED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.PLANNING,
                            role=RoleName.PLANNER,
                            tool_name=f"planner.{provider_name}",
                            message=(
                                "Planner provider call started "
                                f"({provider_name})."
                            ),
                            payload={
                                "provider": provider_name,
                                "fallback_step": fallback_step,
                                "timeout_seconds": self._planner_provider_timeout_seconds(
                                    provider_name
                                ),
                                "prompt_fingerprint": prompt_fingerprint,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "planner provider start event emit failed "
                            "(non-fatal): %s",
                            exc,
                        )
                result = self._try_plan_provider_with_timeout(
                    provider_name=provider_name,
                    task_id=task_id,
                    request_text=request_text,
                    scenario=scenario,
                    actor_name=actor_name,
                    default_payload=default_payload,
                    issue_context=issue_context,
                    candidate_files=candidate_files,
                    prior_failures_block=prior_failures_block,
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
                if self.db is not None:
                    try:
                        record_event(
                            self.db,
                            task_id=task_id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.PLANNING,
                            role=RoleName.PLANNER,
                            tool_name=f"planner.{provider_name}",
                            message=(
                                "Planner provider call completed "
                                f"({provider_name})."
                            ),
                            payload={
                                "provider": provider_name,
                                "fallback_step": fallback_step,
                                "latency_ms": int(
                                    (time.perf_counter() - started) * 1000
                                ),
                                "model_name": result.model_name
                                or self._resolve_model_name(provider_name),
                                "prompt_fingerprint": prompt_fingerprint,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "planner provider success event emit failed "
                            "(non-fatal): %s",
                            exc,
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
                if self.db is not None:
                    try:
                        record_event(
                            self.db,
                            task_id=task_id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.PLANNING,
                            role=RoleName.PLANNER,
                            tool_name=f"planner.{provider_name}",
                            message=(
                                "Planner provider call failed "
                                f"({provider_name}): {str(exc)[:240]}"
                            ),
                            payload={
                                "provider": provider_name,
                                "fallback_step": fallback_step,
                                "latency_ms": int(
                                    (time.perf_counter() - started) * 1000
                                ),
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:1000],
                                "prompt_fingerprint": prompt_fingerprint,
                            },
                        )
                    except Exception as event_exc:  # noqa: BLE001
                        logger.warning(
                            "planner provider failure event emit failed "
                            "(non-fatal): %s",
                            event_exc,
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

    def _planner_provider_timeout_seconds(self, provider_name: str) -> float:
        configured = float(
            getattr(self.settings, "planner_provider_timeout_seconds", 180.0)
            or 180.0
        )
        if provider_name == "claude_code":
            provider_cap = float(
                getattr(self.settings, "claude_code_timeout_seconds", configured)
                or configured
            )
            return min(configured, provider_cap) if configured > 0 else provider_cap
        if provider_name == "minimax":
            provider_cap = float(
                getattr(self.settings, "minimax_planner_timeout_seconds", configured)
                or configured
            )
            return min(configured, provider_cap) if configured > 0 else provider_cap
        return configured

    def _try_plan_provider_with_timeout(
        self,
        *,
        provider_name: str,
        task_id: str,
        request_text: str,
        scenario: str,
        actor_name: str,
        default_payload: dict[str, Any],
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> PlanGenerationResult:
        timeout_seconds = self._planner_provider_timeout_seconds(provider_name)
        if timeout_seconds <= 0:
            return self._try_plan_provider(
                provider_name=provider_name,
                task_id=task_id,
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                default_payload=default_payload,
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
            )

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._try_plan_provider,
            provider_name=provider_name,
            task_id=task_id,
            request_text=request_text,
            scenario=scenario,
            actor_name=actor_name,
            default_payload=default_payload,
            issue_context=issue_context,
            candidate_files=candidate_files,
            prior_failures_block=prior_failures_block,
        )
        try:
            return future.result(timeout=float(timeout_seconds))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                "Planner provider "
                f"{provider_name!r} timed out after {timeout_seconds:.1f}s"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _try_plan_provider(
        self,
        *,
        provider_name: str,
        task_id: str,
        request_text: str,
        scenario: str,
        actor_name: str,
        default_payload: dict[str, Any],
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> PlanGenerationResult:
        """Attempt plan generation with a single provider. Raises on failure.

        P0-1 fix (2026-05-11): ``issue_context`` is now plumbed through
        to every provider method so the planner LLM actually sees Jira
        summary/description, not just the bare key. Previously the
        fetched issue context was used only by the fallback payload
        builder; LLM planners saw `request_text="P69-19"` and gave
        generic stub plans.
        """
        model_name = self._resolve_model_name(provider_name)

        if provider_name == "claude_code":
            plan_payload = self._generate_plan_with_claude_code(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                default_payload=default_payload,
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
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
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
            )
            mode = "messages_api"
        elif provider_name == "openai":
            plan_payload = self._generate_plan_with_openai(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
            )
            mode = "responses_api"
        elif provider_name == "minimax":
            plan_payload = self._generate_plan_with_minimax(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
            )
            mode = "chatcompletion_v2"
        elif provider_name == "deepseek":
            plan_payload = self._generate_plan_with_deepseek(
                request_text=request_text,
                scenario=scenario,
                actor_name=actor_name,
                model_name=model_name,
                default_payload=default_payload,
                issue_context=issue_context,
                candidate_files=candidate_files,
                prior_failures_block=prior_failures_block,
            )
            mode = "chatcompletion_openai_compat"
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

    @staticmethod
    def _format_candidate_files_for_prompt(
        candidate_files: list[dict] | None,
    ) -> str:
        """Phase B.2.a (2026-05-11): render candidate-file list for the
        planner prompt. The planner LLM (esp. DeepSeek API) doesn't
        browse the repository on its own; without an explicit file
        menu it returns empty must_touch_files. This block forces a
        bounded set of real paths into the prompt so the planner
        chooses from them (or explicitly says needs_more_context).
        """
        if not candidate_files:
            return ""
        lines = [
            "Candidate files (pre-plan repository search; pick must_touch from here):",
        ]
        for idx, cand in enumerate(candidate_files[:10], start=1):
            path = str(cand.get("path") or "").strip()
            score = cand.get("score")
            terms = cand.get("matched_terms") or []
            reason = str(cand.get("reason") or "").strip()
            terms_str = ", ".join([str(t) for t in terms[:5]])
            if path:
                lines.append(
                    f"  {idx}. {path}  [score={score}, matches: {terms_str}]"
                )
                if reason:
                    lines.append(f"     reason: {reason}")
        lines.append(
            "Rules: choose must_touch_files only from this list. If none "
            "fit, set must_touch_files=[] and explain in change_summary "
            "what additional context is needed. Do NOT invent paths."
        )
        return "\n".join(lines).strip()

    @staticmethod
    def _format_issue_context_for_prompt(
        issue_context: dict[str, Any] | None,
    ) -> str:
        """Render Jira issue context as a stable prompt section. P0-1 fix
        (2026-05-11): planner methods were previously only seeing the
        bare request_text (e.g. "P69-19"), giving generic stub plans.
        This helper extracts summary / description / acceptance / status
        and formats them in a consistent, cache-friendly way so every
        provider gets the same surface.
        """
        if not issue_context:
            return ""
        parts: list[str] = []
        issue_key = str(issue_context.get("issue_key") or "").strip()
        summary = str(issue_context.get("summary") or "").strip()
        description = str(issue_context.get("description") or "").strip()
        status = str(issue_context.get("issue_status") or "").strip()
        acceptance = str(issue_context.get("acceptance_criteria") or "").strip()
        if issue_key:
            parts.append(f"Jira issue: {issue_key}")
        if summary:
            parts.append(f"Summary: {summary}")
        if status:
            parts.append(f"Status: {status}")
        if description:
            parts.append("Description:")
            parts.append(description)
        if acceptance:
            parts.append("Acceptance criteria:")
            parts.append(acceptance)
        return "\n".join(parts).strip()

    def _validate_plan_targets(self, plan_payload: GeneratedPlanPayload) -> GeneratedPlanPayload:
        must_touch = list(plan_payload.must_touch_files or [])
        expected_new = list(plan_payload.expected_new_files or [])
        # Phase 1.3 (2026-05-11): no-silent-drop. The previous validator
        # silently dropped any must_touch entry not found in the KB,
        # which made "planner picked wrong paths" indistinguishable
        # from "planner picked no paths" downstream. Now we log raw /
        # validated / dropped separately, and if validation collapses
        # must_touch to empty (when the planner DID emit paths), we
        # raise PLAN_TARGETS_DROPPED_TO_EMPTY so the orchestrator can
        # surface the actionable error instead of falling into the
        # downstream "no files in plan" cascade.
        logger.warning(
            "plan_validate raw_must_touch=%s expected_new=%s",
            must_touch,
            expected_new,
        )
        dropped: list[str] = []
        if self.db is not None and must_touch:
            kept, dropped = _validate_must_touch_against_kb(must_touch, self.db)
            if dropped:
                logger.warning(
                    "must_touch_files entries dropped during plan_validate "
                    "(not in KB): paths=%s kept=%s",
                    dropped, kept,
                )
            if kept != must_touch:
                plan_payload = plan_payload.model_copy(update={"must_touch_files": kept})
            # If the planner DID emit paths but all of them were dropped
            # AND no expected_new_files compensate → raise so the
            # operator sees the real cause (path-format mismatch /
            # wrong source / hallucinated paths) instead of a generic
            # "codegen had nothing to do" further down the pipeline.
            if dropped and not kept and not expected_new:
                raise PlanTargetsDroppedToEmpty(
                    f"All planner must_touch_files were dropped during KB "
                    f"validation. Raw: {dropped[:10]}. This usually means "
                    f"the planner is using paths that don't match the active "
                    f"source layout (e.g. emitting React paths on an Android "
                    f"repo) or the KB is not synced. Check active source: "
                    f"{getattr(self.settings, 'knowledge_source_name', '?')}."
                )

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
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> GeneratedPlanPayload:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPS_AGENT_OPENAI_API_KEY is not configured")

        issue_block = self._format_issue_context_for_prompt(issue_context)
        candidates_block = self._format_candidate_files_for_prompt(candidate_files)
        extras = "".join(
            [
                f"{issue_block}\n\n" if issue_block else "",
                f"{candidates_block}\n\n" if candidates_block else "",
                f"{prior_failures_block}\n\n" if prior_failures_block else "",
            ]
        )
        user_text = (
            f"Actor: {actor_name}\n"
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            f"{extras}"
            "Return only the structured execution plan."
        )
        payload = {
            "model": model_name,
            "instructions": self._build_planning_instructions(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": user_text,
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
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> GeneratedPlanPayload:
        if not self.settings.anthropic_api_key:
            raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY is not configured")

        issue_block = self._format_issue_context_for_prompt(issue_context)
        candidates_block = self._format_candidate_files_for_prompt(candidate_files)
        extras = "".join(
            [
                f"{issue_block}\n\n" if issue_block else "",
                f"{candidates_block}\n\n" if candidates_block else "",
                f"{prior_failures_block}\n\n" if prior_failures_block else "",
            ]
        )
        user_text = (
            f"Actor: {actor_name}\n"
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            f"{extras}"
            "Return only valid JSON that matches the requested plan schema."
        )
        payload = {
            "model": model_name,
            "max_tokens": 8192,
            "system": self._build_planning_instructions(),
            "messages": [
                {
                    "role": "user",
                    "content": user_text,
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
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> GeneratedPlanPayload:
        """Invoke Claude Code CLI (``claude --print``) as the planner."""
        claude_cmd = shutil.which(self.settings.claude_code_command)
        claude_args = self.settings.claude_code_args.split()
        if not claude_cmd:
            raise RuntimeError(f"Claude Code CLI not found: {self.settings.claude_code_command}")

        issue_block = self._format_issue_context_for_prompt(issue_context)
        candidates_block = self._format_candidate_files_for_prompt(candidate_files)
        user_prompt = (
            f"Actor: {actor_name}\n"
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            + (f"{issue_block}\n\n" if issue_block else "")
            + (f"{candidates_block}\n\n" if candidates_block else "")
            + (f"{prior_failures_block}\n\n" if prior_failures_block else "")
            + "Return ONLY valid JSON that matches the requested plan schema. "
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
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> GeneratedPlanPayload:
        if not self.settings.minimax_api_key:
            raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY is not configured")

        issue_block = self._format_issue_context_for_prompt(issue_context)
        candidates_block = self._format_candidate_files_for_prompt(candidate_files)
        user_content = (
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            + (f"{issue_block}\n\n" if issue_block else "")
            + (f"{candidates_block}\n\n" if candidates_block else "")
            + (f"{prior_failures_block}\n\n" if prior_failures_block else "")
            + "Return only valid JSON that matches the requested plan schema."
        )
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
                    "content": user_content,
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

    def _generate_plan_with_deepseek(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        model_name: str,
        default_payload: GeneratedPlanPayload | None = None,
        issue_context: dict[str, Any] | None = None,
        candidate_files: list[dict] | None = None,
        prior_failures_block: str = "",
    ) -> GeneratedPlanPayload:
        """Plan generation via DeepSeek's OpenAI-compatible chat-
        completions endpoint. 2026-05-11: added so the harness can run
        100% DeepSeek (planner + codegen + reviewer) for the
        DeepSeek-special baseline.
        """
        if not self.settings.deepseek_api_key:
            raise RuntimeError("OPS_AGENT_DEEPSEEK_API_KEY is not configured")

        issue_block = self._format_issue_context_for_prompt(issue_context)
        candidates_block = self._format_candidate_files_for_prompt(candidate_files)
        user_content = (
            f"Actor: {actor_name}\n"
            f"Scenario: {scenario}\n"
            f"Request:\n{request_text}\n\n"
            + (f"{issue_block}\n\n" if issue_block else "")
            + (f"{candidates_block}\n\n" if candidates_block else "")
            + (f"{prior_failures_block}\n\n" if prior_failures_block else "")
            + "Return ONLY valid JSON that matches the requested plan "
            "schema. No markdown fences, no explanations, no "
            "commentary — pure JSON only."
        )
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._build_planning_instructions()},
                {"role": "user", "content": user_content},
            ],
            # Temperature is ignored under DeepSeek thinking mode but
            # left here for non-thinking variants and audit clarity.
            "temperature": 0.2,
            "max_tokens": int(
                getattr(self.settings, "deepseek_max_tokens_planner", 16384)
            ),
            # Phase A.2 (2026-05-11): planner is the hardest reasoning
            # stage in the harness — cross-file plan generation needs
            # `max` effort, not the implicit `high` default.
            "reasoning_effort": str(
                getattr(self.settings, "deepseek_reasoning_effort_planner", "max")
            ),
        }

        # DeepSeek uses an OpenAI-compatible /v1/chat/completions endpoint;
        # base_url is configured per .env (defaults to api.deepseek.com).
        base_url = (
            self.settings.deepseek_base_url.rstrip("/")
            .replace("/anthropic", "")  # Anthropic-compat alias used by
            # codegen path; planner stays on the OpenAI-compatible route.
        )
        url = f"{base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            url,
            headers=headers,
            json=body,
            timeout=external_http_timeout(
                max(self.settings.primary_agent_timeout_seconds, 120)
            ),
        )
        response.raise_for_status()
        response_payload = response.json()

        choices = response_payload.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek response had no choices")
        content = (
            choices[0].get("message", {}).get("content", "") or ""
        ).strip()
        if not content:
            raise RuntimeError("DeepSeek returned empty content for planner")

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

        for field_name in ("affected_code_locations", "must_touch_files", "expected_new_files", "must_inspect_files", "likely_touch_files", "tools", "steps", "acceptance_tests"):
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

        for field_name, limit in (
            ("must_touch_files", 12),
            ("expected_new_files", 8),
            ("must_inspect_files", 12),
            ("likely_touch_files", 12),
        ):
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
            _demote_unsourced_android_map_new_files(sanitized)

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
        acceptance = _normalize_library_acceptance_tests(
            acceptance,
            plan_payload=sanitized,
        )
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
            "Phase 2 scope levels (2026-05-11): in addition to must_touch_files and expected_new_files, populate two new read-only/uncertain scope levels for code-change scenarios: "
            "must_inspect_files — READ-ONLY context files you depend on to ground the patch (you will NOT modify them). Typical examples: build.gradle for SDK/dependency lookup, AndroidManifest.xml for permission/feature tags, nav_graph.xml for route registration, package.json for module version. List these when the patch's correctness depends on inspecting their content (e.g. confirming a SDK version, or a registered route name). The codegen step receives them as read-only context and the harness does NOT require them to appear in the diff. "
            "likely_touch_files — files that probably need modification but evidence is currently inconclusive (filename matches keywords but content not yet read; one of several plausible Composables; etc.). The harness MAY upgrade these to must_touch after deeper inspection; do not promote a file here to must_touch unless you are confident it will be edited. "
            "Discipline: pick must_inspect_files / likely_touch_files / must_touch_files ONLY from the Candidate files block when one is provided in the prompt. Do not invent paths. If the candidate menu is insufficient, leave the lists empty and explain in change_summary what additional context is needed. "
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
            "Library contract discipline: when repository context or a domain playbook says OSMDroid/org.osmdroid is the map library, do NOT emit Google Maps-only APIs such as setOnMapClickListener in acceptance_tests, plans, or expected outputs. Use OSMDroid patterns instead: MapEventsOverlay, MapEventsReceiver, and singleTapConfirmedHelper. "
            "Scope discipline for existing-feature integration: Do not propose a new reusable component file unless the Jira issue explicitly names that new file or repository evidence shows no existing target can host the change. Prefer minimal edits to evidence-backed existing files over broad multi-file rewrites. "
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

        # P0-2 gate (2026-05-11): codegen-style scenarios must have at
        # least one file target (must_touch or expected_new). Planner
        # returning empty arrays is a generic-stub signal — proceeding
        # to action wastes a codegen call and downstream gates produce
        # confusing "file not in plan" errors. Reject at review.
        _scenario = (getattr(plan, "scenario", "") or "").lower()
        _codegen_scenarios = {
            "jira_issue_develop",
            "bug_fix",
            "feature_implementation",
            "refactor",
            "code_change",
        }
        _must_touch = list(getattr(plan, "must_touch_files", []) or [])
        _expected_new = list(getattr(plan, "expected_new_files", []) or [])
        if (
            _scenario in _codegen_scenarios
            and not _must_touch
            and not _expected_new
        ):
            findings.append(
                ReviewFinding(
                    code="plan_empty_targets",
                    severity="error",
                    message=(
                        f"Plan has empty must_touch/expected_new for "
                        f"{_scenario}; codegen has no targets."
                    ),
                    field="must_touch_files",
                )
            )
            policy_checks.append(
                PolicyCheck(
                    name="plan_targets_non_empty",
                    status="failed",
                    detail="Plan has zero file targets for a codegen scenario.",
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
