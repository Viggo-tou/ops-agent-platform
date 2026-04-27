from __future__ import annotations

import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schemas import GeneratedPlan, GeneratedSemanticTranslation
from app.agents.service import ActionAgent, PrimaryAgentPlanner, ReviewerAgent
from app.agents.translation import SemanticTranslator
from app.core.enums import ActorRole, ApprovalStatus, EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.core.jira import extract_jira_issue_reference, looks_like_jira_issue_url
from app.core.telemetry import get_current_trace_id, get_tracer
from app.models.approval import Approval
from app.models.event import Event
from app.models.task import Task
from app.models.tool_execution import ToolExecution
from app.schemas.evidence import EvidenceItem
from app.services.events import commit_checkpoint, record_event, set_task_status
from app.services.sandbox import ExecutionSandbox, SandboxError
from app.services.spec_conformance import (
    ConformanceReport,
    build_goal_attestation,
    check_spec_conformance,
)
from app.services.task_workspace import TaskWorkspace
from app.tools.gateway import ToolApprovalRequired, ToolGateway, ToolInvocationError


def _contains_word(text: str, *keywords: str) -> bool:
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords)


def _truncate_text(value: object, *, limit: int) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 1)]}..."


def _set_span_attribute(span: object, key: str, value: object | None) -> None:
    if value is None:
        return
    if hasattr(value, "value"):
        value = getattr(value, "value")
    span.set_attribute(key, value)


def _set_task_span_attributes(span: object, *, task: Task, actor_name: str | None = None) -> None:
    _set_span_attribute(span, "task.id", task.id)
    _set_span_attribute(span, "task.scenario", task.scenario)
    _set_span_attribute(span, "task.status", task.status)
    _set_span_attribute(span, "task.workflow_stage", task.workflow_stage)
    _set_span_attribute(span, "actor.name", actor_name or task.actor_name)


# Heuristic: phrases that mean "user is asking ABOUT something" rather
# than "user is asking the system to DO something". When no Jira issue
# key is present and the prompt is clearly question-form, the request
# should route to process_question regardless of which content nouns
# (ticket, issue, access, etc.) appear later in the sentence.
_QUESTION_LEAD_EN = (
    "what ", "where ", "when ", "why ", "who ", "which ", "how ",
    "explain ", "explain:", "describe ", "describe:", "tell me ",
    "show me ", "trace ", "summarize ", "summarise ",
    "list the", "list all", "list every",
    "walk me through", "walk through",
    "is there", "is the", "are there", "are the",
    "does the", "do the", "did the",
    "can you explain", "can you describe", "can you show",
)
_QUESTION_LEAD_ZH = (
    "什么", "哪里", "哪个", "哪些", "怎么", "如何", "为什么", "为何",
    "解释", "说明", "描述", "介绍",
    "在哪", "请问",
)


def _looks_like_question(request_text: str) -> bool:
    """Heuristic check: does this prompt read like a question rather than
    an action request? Returns True for natural-language QA phrasing
    (e.g. "Where is the support page?", "How does the X work?", "Trace
    the Y pipeline.") even when later words happen to overlap with
    action-routing keywords (ticket, access, etc.).
    """
    text = request_text.strip()
    if not text:
        return False
    if "?" in text or "?" in text:
        return True
    lowered = text.lower()
    if any(lowered.startswith(p) for p in _QUESTION_LEAD_EN):
        return True
    if any(p in text for p in _QUESTION_LEAD_ZH):
        return True
    return False


def classify_request(request_text: str) -> str:
    lowered = request_text.lower()
    jira_reference = extract_jira_issue_reference(request_text)
    if jira_reference and any(
        keyword in lowered
        for keyword in (
            "transition",
            "move to",
            "status",
            "标记为",
            "推进",
            "移到",
            "in progress",
            "done",
            "complete",
            "close",
            "reopen",
            "comment",
            "评论",
            "备注",
            "note",
        )
    ):
        return "jira_issue_writeback"
    if jira_reference and (
        looks_like_jira_issue_url(request_text)
        or _contains_word(lowered, "plan", "breakdown", "implementation", "rollout", "scope")
    ):
        return "jira_issue_plan"
    if jira_reference and not _contains_word(lowered, "plan", "breakdown", "rollout", "scope"):
        # Bare Jira reference or Jira + any action keyword → develop pipeline.
        # This is the most common intent when a user pastes a Jira key.
        return "jira_issue_develop"
    # Question-form short-circuit: when no Jira issue key is present and
    # the prompt clearly reads as a question ("Where is X?", "How does
    # Y work?", "Trace Z..."), route to process_question regardless of
    # incidental content keywords (ticket, access, change). Closes the
    # gap where QA prompts about support/admin/feedback were misrouted
    # to jira_issue_create / action_with_approval and got rejected.
    if _looks_like_question(request_text):
        return "process_question"
    if "#" in lowered or _contains_word(lowered, "slack", "channel"):
        return "slack_message"
    if _contains_word(lowered, "jira", "ticket", "issue", "bug", "story"):
        return "jira_issue_create"
    if _contains_word(lowered, "sql", "database", "table", "select") or " from " in lowered:
        return "internal_db_query"
    if any(keyword in lowered for keyword in ("internal api", "endpoint", "service call", "/api/", "http://", "https://")):
        return "internal_api_request"
    if _contains_word(lowered, "approve", "approval", "notify", "access", "delete", "change"):
        return "action_with_approval"
    if _contains_word(lowered, "debug", "fix", "error", "exception", "traceback", "stacktrace", "crash", "logcat"):
        return "process_question"
    return "process_question"


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def detect_user_language(text: str) -> str:
    """Return 'zh' if *text* is predominantly CJK, otherwise 'en'."""
    if not text:
        return "en"
    non_space = text.replace(" ", "")
    if not non_space:
        return "en"
    cjk_count = len(_CJK_RE.findall(non_space))
    return "zh" if cjk_count / len(non_space) > 0.1 else "en"


class PrimaryOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.primary_agent = PrimaryAgentPlanner()
        self.semantic_translator = SemanticTranslator()
        self.action_agent = ActionAgent()
        self.reviewer_agent = ReviewerAgent()
        self.tool_gateway = ToolGateway(db)

    def _task_workspace(self, task: Task) -> TaskWorkspace:
        return TaskWorkspace.for_task(task.id, settings=self.tool_gateway.settings)

    def _workspace_call(self, task: Task, fn):
        try:
            return fn(self._task_workspace(task))
        except Exception:  # noqa: BLE001
            return None

    def _workspace_write_intent(self, task: Task) -> None:
        language = detect_user_language(task.request_text or "")

        def _write(workspace: TaskWorkspace) -> None:
            workspace.write_intent(
                intent_text=task.request_text or "",
                language=language,
                must_touch_files=[],
                scenario=task.scenario,
            )
            workspace.append_audit(
                "intake",
                {
                    "task_id": task.id,
                    "scenario": task.scenario,
                    "language": language,
                    "request_text": task.request_text,
                },
            )
            workspace.write_checkpoint(
                stage_completed="intake",
                next_stage="semantic_translation",
                resume_args={"task_id": task.id},
            )

        self._workspace_call(task, _write)

    def _workspace_append_audit(self, task: Task, event_name: str, payload: dict[str, object]) -> None:
        self._workspace_call(task, lambda workspace: workspace.append_audit(event_name, payload))

    def _workspace_write_checkpoint(
        self,
        task: Task,
        *,
        stage_completed: str,
        next_stage: str | None,
        resume_args: dict[str, object],
    ) -> None:
        self._workspace_call(
            task,
            lambda workspace: workspace.write_checkpoint(
                stage_completed=stage_completed,
                next_stage=next_stage,
                resume_args=resume_args,
            ),
        )

    def _workspace_write_plan(self, task: Task, plan: GeneratedPlan, *, reason: str) -> None:
        def _write(workspace: TaskWorkspace) -> None:
            workspace.write_plan(plan_payload=plan, reason=reason)
            workspace.append_audit(
                "plan",
                {
                    "plan_id": plan.plan_id,
                    "scenario": plan.scenario,
                    "must_touch_files": list(getattr(plan, "must_touch_files", []) or []),
                },
            )
            workspace.write_checkpoint(
                stage_completed="plan",
                next_stage="codegen" if task.scenario == "jira_issue_develop" else "execution",
                resume_args={"plan_id": plan.plan_id},
            )

        self._workspace_call(task, _write)

    def _workspace_add_evidence_from_result(
        self,
        task: Task,
        result: dict[str, object],
        *,
        event_name: str,
    ) -> None:
        raw_items = result.get("evidence_items")
        if not isinstance(raw_items, list) or not raw_items:
            return

        def _write(workspace: TaskWorkspace) -> None:
            items = [
                EvidenceItem.model_validate(item)
                for item in raw_items
                if isinstance(item, dict)
            ]
            if not items:
                return
            workspace.add_evidence(items)
            workspace.append_audit(
                event_name,
                {
                    "evidence_count": len(items),
                    "sources": sorted({item.source for item in items}),
                    "paths": [item.file_path for item in items[:10]],
                },
            )

        self._workspace_call(task, _write)

    def _workspace_attempt_index(self, task: Task, pipeline_state: dict[str, object]) -> int:
        existing = pipeline_state.get("workspace_attempt_index")
        if isinstance(existing, int) and existing >= 1:
            return existing
        index = self._workspace_call(task, lambda workspace: workspace.next_attempt_index())
        if not isinstance(index, int) or index < 1:
            index = 1
        pipeline_state["workspace_attempt_index"] = index
        return index

    def _workspace_write_attempt_diff(
        self,
        task: Task,
        pipeline_state: dict[str, object],
        *,
        diff: str,
        next_stage: str = "review",
    ) -> None:
        attempt_index = self._workspace_attempt_index(task, pipeline_state)

        def _write(workspace: TaskWorkspace) -> None:
            workspace.write_attempt_diff(attempt_index, diff)
            workspace.append_audit(
                "attempt.diff",
                {"attempt": attempt_index, "diff_chars": len(diff)},
            )
            workspace.write_checkpoint(
                stage_completed=f"attempt_{attempt_index:03d}",
                next_stage=next_stage,
                resume_args={"attempt": attempt_index},
            )

        self._workspace_call(task, _write)

    def _workspace_write_attempt_compile(
        self,
        task: Task,
        pipeline_state: dict[str, object],
        *,
        result_dict: dict[str, object],
    ) -> None:
        attempt_index = self._workspace_attempt_index(task, pipeline_state)
        self._workspace_call(
            task,
            lambda workspace: workspace.write_attempt_compile(attempt_index, result_dict),
        )

    def _workspace_write_attempt_review(
        self,
        task: Task,
        pipeline_state: dict[str, object],
        *,
        report_dict: dict[str, object],
        narrative: str,
    ) -> None:
        attempt_index = self._workspace_attempt_index(task, pipeline_state)

        def _write(workspace: TaskWorkspace) -> None:
            workspace.write_attempt_review(
                attempt_index,
                report_dict=report_dict,
                narrative=narrative,
            )
            workspace.append_audit(
                "attempt.review",
                {"attempt": attempt_index, "blocked": bool(report_dict.get("blocked"))},
            )

        self._workspace_call(task, _write)

    def bootstrap_task(self, task: Task, *, actor_name: str) -> None:
        with get_tracer().start_as_current_span("task.bootstrap") as span:
            _set_task_span_attributes(span, task=task, actor_name=actor_name)
            task.trace_id = get_current_trace_id()
            _set_span_attribute(span, "task.trace_id", task.trace_id)
            return self._bootstrap_task_impl(task=task, actor_name=actor_name)

    def _bootstrap_task_impl(self, task: Task, *, actor_name: str) -> None:
        self._workspace_write_intent(task)
        planning_request_text = task.request_text
        semantic_translation = self._translate_request(task=task, actor_name=actor_name, issue_context=None)
        self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
        task.translation_json = semantic_translation.model_dump(mode="json")

        issue_context: dict[str, object] | None = None
        planning_knowledge_context: dict[str, object] | None = None

        if task.scenario in {"jira_issue_plan", "jira_issue_develop"}:
            issue_context = self._prefetch_jira_issue_context(
                task=task,
                actor_name=actor_name,
                issue_key=semantic_translation.issue_key,
            )
            if issue_context is None and task.scenario == "jira_issue_develop":
                # Graceful fallback: proceed with translation-only context
                # when the Jira issue can't be loaded (e.g. deleted project).
                # _prefetch_jira_issue_context already marked the task as FAILED,
                # so reset it back to CREATED to allow the pipeline to continue.
                issue_context = {
                    "key": semantic_translation.issue_key or "UNKNOWN",
                    "summary": semantic_translation.objective or task.request_text or "",
                    "description": semantic_translation.normalized_request or task.request_text or "",
                    "status": "Unknown",
                    "_synthetic": True,
                }
                set_task_status(
                    self.db,
                    task=task,
                    new_status=TaskStatus.CREATED,
                    new_stage=WorkflowStage.INTAKE,
                    role=RoleName.PRIMARY,
                    source=EventSource.ORCHESTRATOR,
                    message="Jira issue unavailable — proceeding with translation-only context.",
                )
            elif issue_context is None:
                return

            # Skip 2nd translation pass when using synthetic issue context —
            # the 1st pass already has concrete grounding terms; re-translating
            # with the empty synthetic context produces generic/unusable terms.
            if not issue_context.get("_synthetic"):
                semantic_translation = self._translate_request(
                    task=task,
                    actor_name=actor_name,
                    issue_context=issue_context,
                )
                self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
                task.translation_json = semantic_translation.model_dump(mode="json")
            planning_knowledge_context = self._prefetch_planning_repository_context(
                task=task,
                actor_name=actor_name,
                semantic_translation=semantic_translation,
            )

            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=planning_knowledge_context,
            )
        elif task.scenario == "jira_issue_writeback":
            issue_context = self._prefetch_jira_issue_context(
                task=task,
                actor_name=actor_name,
                issue_key=semantic_translation.issue_key,
            )
            if issue_context is None:
                return

            semantic_translation = self._translate_request(
                task=task,
                actor_name=actor_name,
                issue_context=issue_context,
            )
            self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
            task.translation_json = semantic_translation.model_dump(mode="json")

            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=None,
            )
        elif task.translation_json:
            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=None,
                planning_knowledge_context=None,
            )

        # --- Defense line 2: anchor pre-check ---
        # If translation extracted grounding_terms/anchors, verify at least one
        # exists in the knowledge source tree. If ALL are missing, the task is
        # likely targeting the wrong repository — fail fast before planning.
        # Skip when using synthetic Jira context — the grounding terms are just
        # the Jira issue key, which will never appear in the codebase.
        _skip_anchor = (
            issue_context is not None and issue_context.get("_synthetic")
        )
        if not _skip_anchor and self._anchor_precheck_fails(task):
            return

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.PLANNING,
            new_stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            source=EventSource.ORCHESTRATOR,
            message="Primary runtime started planner execution.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.PLANNING_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            message="Planner role started structured plan generation.",
            payload={"actor_name": actor_name},
        )

        with get_tracer().start_as_current_span("task.plan") as span:
            _set_task_span_attributes(span, task=task, actor_name=actor_name)
            planning_result = self.primary_agent.generate_plan(
                task_id=task.id,
                request_text=planning_request_text,
                scenario=task.scenario,
                actor_name=actor_name,
                semantic_translation=semantic_translation,
                planning_knowledge=planning_knowledge_context,
                issue_context=issue_context,
            )
        plan_document = planning_result.plan
        task.plan_json = plan_document.model_dump(mode="json")

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.PLAN_GENERATED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            message="Execution plan generated.",
            payload={
                "actor_name": actor_name,
                "plan": task.plan_json,
                "provider_name": planning_result.provider_name,
                "model_name": planning_result.model_name,
                "used_fallback": planning_result.used_fallback,
                "fallback_reason": planning_result.fallback_reason,
            },
        )
        self._workspace_write_plan(task, plan_document, reason="planner_generated")
        commit_checkpoint(self.db, label="plan_generated")

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.REVIEWING,
            new_stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Reviewer started plan validation.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.REVIEW_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Reviewer role started pre-execution validation.",
            payload={"plan_id": plan_document.plan_id},
        )

        with get_tracer().start_as_current_span("task.review") as span:
            _set_task_span_attributes(span, task=task, actor_name=actor_name)
            _set_span_attribute(span, "plan.id", plan_document.plan_id)
            review_result = self.reviewer_agent.review_plan(
                task_id=task.id,
                actor_name=actor_name,
                plan=plan_document,
            )
        task.review_json = review_result.review.model_dump(mode="json")

        if review_result.review.verdict == "approved":
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.REVIEW_PASSED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message="Reviewer approved the execution plan.",
                payload={"review": task.review_json},
            )
            commit_checkpoint(self.db, label="review_passed_pre_execution")
            self._execute_plan(task=task, actor_name=actor_name, plan=plan_document)
            return

        if review_result.review.verdict == "requires_approval":
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.REVIEW_PASSED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message="Reviewer approved the plan with an approval gate.",
                payload={"review": task.review_json},
            )

            required_approver_role = (
                review_result.review.approval_requirements[0].approver_role
                if review_result.review.approval_requirements
                else ActorRole.TEAM_LEAD.value
            )

            approval = Approval(
                task_id=task.id,
                action_name=self._resolve_tool_name(plan_document),
                status=ApprovalStatus.PENDING,
                requested_by_role=RoleName.REVIEWER,
                approver_role=required_approver_role,
                requested_by_actor_name=task.actor_name,
                risk_level=task.risk_level,
                risk_category=task.risk_category,
                reason="Reviewer marked the plan as approval-required before execution.",
                request_payload_json={
                    "request_text": task.request_text,
                    "scenario": task.scenario,
                    "proposed_plan": task.plan_json,
                    "review": task.review_json,
                },
                policy_snapshot_json={
                    "decision": "require_approval",
                    "source": "reviewer_pre_execution_gate",
                    "tool_name": self._resolve_tool_name(plan_document),
                    "actor_name": task.actor_name,
                    "actor_role": task.actor_role.value,
                    "risk_level": task.risk_level.value,
                    "risk_category": task.risk_category.value,
                    "required_approver_role": required_approver_role,
                },
            )
            self.db.add(approval)
            self.db.flush()

            task.pending_approval = True
            task.latest_result_json = {
                "status": TaskStatus.AWAITING_APPROVAL.value,
                "message": "Reviewer requires manual approval before execution can continue.",
                "approval_id": approval.id,
                "review": task.review_json,
            }

            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.AWAITING_APPROVAL,
                new_stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                source=EventSource.ORCHESTRATOR,
                message="Task is awaiting manual approval after review.",
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.APPROVAL_REQUESTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message="Approval requested for planned action.",
                payload={
                    "approval_id": approval.id,
                    "action_name": approval.action_name,
                    "approver_role": approval.approver_role,
                    "review_summary": review_result.review.summary,
                },
            )
            self._workspace_write_checkpoint(
                task,
                stage_completed="plan",
                next_stage="approval",
                resume_args={"approval_id": approval.id, "plan_id": plan_document.plan_id},
            )
            commit_checkpoint(self.db, label="awaiting_approval_pre_execution")
            return

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.REVIEW_FAILED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Reviewer rejected the plan before execution.",
            payload={"review": task.review_json},
        )
        task.latest_result_json = {
            "status": TaskStatus.FAILED.value,
            "message": review_result.review.summary,
            "review": task.review_json,
        }
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Task failed during plan review.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after plan review failure.",
            payload={"review_id": review_result.review.review_id},
        )

    def resume_after_approval(self, *, task: Task, actor_name: str, approval_id: str) -> None:
        plan_document = GeneratedPlan.model_validate(task.plan_json or {})
        # T-039: for develop tasks paused at the post-conformance Jira
        # transition gate, set the granted flag on pipeline_state and
        # re-enter the develop pipeline. Cached pipeline_state entries
        # (codegen, sandbox, review, conformance, attestation) short-
        # circuit their stages, so the recursion only runs the Jira
        # writeback + completion tail.
        if (task.scenario or "") == "jira_issue_develop":
            pipeline_state = self._load_develop_pipeline_state(task)
            pending_id = pipeline_state.get("pending_jira_approval_id")
            if pending_id == approval_id or pending_id is None:
                pipeline_state["jira_approval_granted"] = True
                pipeline_state.pop("pending_jira_approval_id", None)
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                self._workspace_write_checkpoint(
                    task,
                    stage_completed="approval",
                    next_stage="writeback",
                    resume_args={"approval_id": approval_id},
                )
                self._execute_develop_pipeline(
                    task=task, actor_name=actor_name, plan=plan_document, approval_id=approval_id
                )
                return
        self._execute_plan(task=task, actor_name=actor_name, plan=plan_document, approval_id=approval_id)

    def _translate_request(
        self,
        *,
        task: Task,
        actor_name: str,
        issue_context: dict[str, object] | None,
    ):
        with get_tracer().start_as_current_span("task.translate") as span:
            _set_task_span_attributes(span, task=task, actor_name=actor_name)
            _set_span_attribute(span, "task.has_issue_context", issue_context is not None)
            return self._translate_request_impl(task=task, actor_name=actor_name, issue_context=issue_context)

    def _translate_request_impl(
        self,
        *,
        task: Task,
        actor_name: str,
        issue_context: dict[str, object] | None,
    ):
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.SEMANTIC_TRANSLATION_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PRIMARY,
            message="Primary runtime started semantic translation for the request.",
            payload={"scenario": task.scenario, "actor_name": actor_name},
        )

        translation_result = self.semantic_translator.translate(
            task_id=task.id,
            request_text=task.request_text,
            scenario=task.scenario,
            actor_name=actor_name,
            issue_context=issue_context,
        )
        translation_document = translation_result.translation

        if translation_result.used_fallback and translation_result.fallback_reason:
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.SEMANTIC_TRANSLATION_FAILED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PRIMARY,
                message="Configured semantic translation provider failed and the runtime switched to fallback.",
                payload={
                    "provider_name": translation_result.provider_name,
                    "model_name": translation_result.model_name,
                    "fallback_reason": translation_result.fallback_reason,
                },
            )

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.SEMANTIC_TRANSLATION_COMPLETED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PRIMARY,
            message="Semantic translation document generated for the request.",
            payload={
                "translation": translation_document.model_dump(mode="json"),
                "provider_name": translation_result.provider_name,
                "model_name": translation_result.model_name,
                "used_fallback": translation_result.used_fallback,
                "fallback_reason": translation_result.fallback_reason,
            },
        )
        self._workspace_append_audit(
            task,
            "semantic_translation",
            {
                "provider_name": translation_result.provider_name,
                "model_name": translation_result.model_name,
                "used_fallback": translation_result.used_fallback,
            },
        )
        return translation_document

    @staticmethod
    def _apply_jira_issue_key_fallback(
        *,
        task: Task,
        semantic_translation: GeneratedSemanticTranslation,
    ) -> None:
        if semantic_translation.issue_key:
            return

        jira_reference = extract_jira_issue_reference(task.request_text)
        if jira_reference:
            semantic_translation.issue_key = jira_reference.issue_key

    def _prefetch_jira_issue_context(
        self,
        *,
        task: Task,
        actor_name: str,
        issue_key: str | None,
    ) -> dict[str, object] | None:
        if not issue_key:
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": "No Jira issue key was found in the planning request.",
                "semantic_translation": task.translation_json,
            }
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                source=EventSource.ORCHESTRATOR,
                message="Task failed before planning because no Jira issue key was present.",
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.FINAL_RESPONSE_EMITTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                message="Final response emitted after Jira planning precheck failure.",
                payload={"reason": "missing_jira_issue_key"},
            )
            return None

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            tool_name="jira.get_issue",
            message="Planner requested Jira issue context before plan generation.",
            payload={"issue_key": issue_key},
        )

        try:
            result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name="jira.get_issue",
                payload={"issue_key": issue_key},
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PLANNER,
            )
            self._sync_retry_count(task)
        except Exception as exc:
            self._sync_retry_count(task)
            event_type = EventType.TOOL_TIMED_OUT if isinstance(exc, ToolInvocationError) and exc.timed_out else EventType.TOOL_FAILED
            record_event(
                self.db,
                task_id=task.id,
                event_type=event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PLANNER,
                tool_name="jira.get_issue",
                message="Planner failed to load Jira issue context before plan generation.",
                payload={"issue_key": issue_key, "error": str(exc)},
            )
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": f"Failed to load Jira issue {issue_key} before planning.",
                "error": str(exc),
                "semantic_translation": task.translation_json,
            }
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                source=EventSource.ORCHESTRATOR,
                message="Task failed before planning because the Jira issue context could not be loaded.",
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.FINAL_RESPONSE_EMITTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                message="Final response emitted after Jira context preload failure.",
                payload={"issue_key": issue_key},
            )
            return None

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_SUCCEEDED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            tool_name="jira.get_issue",
            message="Planner loaded Jira issue context before plan generation.",
            payload=result,
        )
        return result

    def _prefetch_planning_repository_context(
        self,
        *,
        task: Task,
        actor_name: str,
        semantic_translation: GeneratedSemanticTranslation,
    ) -> dict[str, object] | None:
        search_queries = [query for query in semantic_translation.search_queries if query.strip()]
        if not search_queries:
            return None

        query = search_queries[0]
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.KNOWLEDGE,
            tool_name="knowledge.search",
            message="Planner requested repository context before plan generation.",
            payload={"query": query, "top_k": 4},
        )

        try:
            result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name="knowledge.search",
                payload={"query": query, "top_k": 4},
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=WorkflowStage.PLANNING,
                role=RoleName.KNOWLEDGE,
            )
            self._sync_retry_count(task)
        except Exception as exc:
            self._sync_retry_count(task)
            event_type = EventType.TOOL_TIMED_OUT if isinstance(exc, ToolInvocationError) and exc.timed_out else EventType.TOOL_FAILED
            record_event(
                self.db,
                task_id=task.id,
                event_type=event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.PLANNING,
                role=RoleName.KNOWLEDGE,
                tool_name="knowledge.search",
                message="Repository context retrieval failed before plan generation.",
                payload={"query": query, "error": str(exc)},
            )
            return None

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.KNOWLEDGE_RETRIEVED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.KNOWLEDGE,
            tool_name="knowledge.search",
            message="Repository context retrieved before plan generation.",
            payload=result,
        )
        if isinstance(result, dict):
            self._workspace_add_evidence_from_result(
                task,
                result,
                event_name="knowledge.prefetch",
            )
        return result

    @staticmethod
    def _summarize_translation_document(translation_document: dict[str, object]) -> dict[str, object]:
        summary: dict[str, object] = {}
        for key in (
            "normalized_request",
            "intent",
            "work_type",
            "objective",
            "issue_key",
            "issue_url",
        ):
            value = translation_document.get(key)
            if isinstance(value, str) and value.strip():
                summary[key] = _truncate_text(value, limit=260)

        for key, limit in (
            ("candidate_modules", 6),
            ("search_queries", 4),
            ("constraints", 4),
            ("requested_outputs", 4),
            ("missing_information", 4),
        ):
            values = translation_document.get(key)
            if isinstance(values, list):
                cleaned = [
                    _truncate_text(value, limit=160)
                    for value in values
                    if isinstance(value, str) and value.strip()
                ][:limit]
                if cleaned:
                    summary[key] = cleaned
        return summary

    @staticmethod
    def _summarize_planning_knowledge_context(planning_knowledge_context: dict[str, object]) -> dict[str, object]:
        summary: dict[str, object] = {}

        answer = planning_knowledge_context.get("answer")
        if isinstance(answer, str) and answer.strip():
            summary["answer"] = _truncate_text(answer, limit=500)

        answer_trace = planning_knowledge_context.get("answer_trace")
        if isinstance(answer_trace, dict):
            trace_summary: dict[str, object] = {}
            for key in ("route_kind", "route_reason", "hallucination_risk", "token_coverage", "top_score"):
                value = answer_trace.get(key)
                if isinstance(value, str) and value.strip():
                    trace_summary[key] = _truncate_text(value, limit=200)
                elif isinstance(value, (int, float)):
                    trace_summary[key] = value
            selected_sources = answer_trace.get("selected_sources")
            if isinstance(selected_sources, list):
                cleaned_sources = [
                    _truncate_text(value, limit=80)
                    for value in selected_sources
                    if isinstance(value, str) and value.strip()
                ][:4]
                if cleaned_sources:
                    trace_summary["selected_sources"] = cleaned_sources
            if trace_summary:
                summary["answer_trace"] = trace_summary

        citations = planning_knowledge_context.get("citations")
        if isinstance(citations, list):
            compact_citations: list[dict[str, object]] = []
            for citation in citations[:4]:
                if not isinstance(citation, dict):
                    continue
                relative_path = citation.get("relative_path")
                source_name = citation.get("source_name")
                if not isinstance(relative_path, str) or not relative_path.strip():
                    continue
                compact_citation: dict[str, object] = {
                    "relative_path": _truncate_text(relative_path, limit=220),
                }
                if isinstance(source_name, str) and source_name.strip():
                    compact_citation["source_name"] = _truncate_text(source_name, limit=80)
                for key in ("line_start", "line_end", "score"):
                    value = citation.get(key)
                    if isinstance(value, (int, float)):
                        compact_citation[key] = value
                snippet = citation.get("snippet")
                if isinstance(snippet, str) and snippet.strip():
                    compact_citation["snippet"] = _truncate_text(snippet, limit=240)
                compact_citations.append(compact_citation)
            if compact_citations:
                summary["citations"] = compact_citations

        return summary

    @staticmethod
    def _augment_request_with_context(
        *,
        original_request: str,
        translation_document: dict[str, object] | None,
        issue_context: dict[str, object] | None,
        planning_knowledge_context: dict[str, object] | None,
    ) -> str:
        lines = [original_request.strip()]

        if translation_document:
            lines.extend(
                [
                    "",
                    "Semantic Translation:",
                    json.dumps(
                        PrimaryOrchestrator._summarize_translation_document(translation_document),
                        indent=2,
                        ensure_ascii=False,
                    ),
                ]
            )

        if issue_context:
            lines.extend(
                [
                    "",
                    "Jira Issue Context:",
                    f"Issue Key: {issue_context.get('issue_key', '')}",
                    f"Summary: {_truncate_text(issue_context.get('summary', ''), limit=240)}",
                    f"Status: {issue_context.get('issue_status', '')}",
                    f"Issue Type: {issue_context.get('issue_type', '')}",
                    f"Priority: {issue_context.get('priority', '')}",
                    f"Description: {_truncate_text(issue_context.get('description', ''), limit=1200)}",
                ]
            )

        if planning_knowledge_context:
            lines.extend(
                [
                    "",
                    "Planning Repository Context:",
                    json.dumps(
                        PrimaryOrchestrator._summarize_planning_knowledge_context(planning_knowledge_context),
                        indent=2,
                        ensure_ascii=False,
                    ),
                ]
            )

        return "\n".join(lines).strip()

    def _execute_plan(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        approval_id: str | None = None,
    ) -> None:
        with get_tracer().start_as_current_span("task.execute") as span:
            _set_task_span_attributes(span, task=task, actor_name=actor_name)
            _set_span_attribute(span, "plan.id", plan.plan_id)
            _set_span_attribute(span, "approval.id", approval_id)
            return self._execute_plan_impl(
                task=task,
                actor_name=actor_name,
                plan=plan,
                approval_id=approval_id,
            )

    def _execute_plan_impl(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        approval_id: str | None = None,
    ) -> None:
        if task.scenario == "jira_issue_develop":
            return self._execute_develop_pipeline(
                task=task,
                actor_name=actor_name,
                plan=plan,
                approval_id=approval_id,
            )
        if task.scenario == "jira_issue_writeback":
            return self._execute_writeback_plan(
                task=task,
                actor_name=actor_name,
                plan=plan,
                approval_id=approval_id,
            )

        tool_name = self._resolve_tool_name(plan)
        execution_stage = WorkflowStage.KNOWLEDGE if tool_name == "knowledge.search" else WorkflowStage.ACTION
        execution_role = RoleName.KNOWLEDGE if tool_name == "knowledge.search" else RoleName.ACTION
        semantic_translation = (
            GeneratedSemanticTranslation.model_validate(task.translation_json or {})
            if task.translation_json
            else self.semantic_translator.translate(
                task_id=task.id,
                request_text=task.request_text,
                scenario=task.scenario,
                actor_name=actor_name,
            ).translation
        )
        if not task.translation_json:
            task.translation_json = semantic_translation.model_dump(mode="json")

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.EXECUTING,
            new_stage=execution_stage,
            role=execution_role,
            source=EventSource.ORCHESTRATOR,
            message="Task entered execution after planner and reviewer stages.",
            payload={"approval_id": approval_id},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=execution_stage,
            role=execution_role,
            tool_name=tool_name,
            message="Execution started from the approved plan.",
            payload={"plan_id": plan.plan_id, "approval_id": approval_id},
        )

        category = self.tool_gateway.get_category(tool_name)
        tool_payload = self.action_agent.build_payload(
            task_id=task.id,
            request_text=task.request_text,
            scenario=task.scenario,
            semantic_translation=semantic_translation,
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=execution_stage,
            role=execution_role,
            tool_name=tool_name,
            message="Tool execution requested from the unified runtime.",
            payload={
                "permission_category": category.value,
                "approval_id": approval_id,
                "payload_preview": tool_payload,
            },
        )

        try:
            result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name=tool_name,
                payload=tool_payload,
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=execution_stage,
                role=execution_role,
                approval_id=approval_id,
            )
            self._sync_retry_count(task)
            if tool_name == "knowledge.search" and isinstance(result, dict):
                self._workspace_add_evidence_from_result(
                    task,
                    result,
                    event_name="knowledge.search",
                )
        except ToolApprovalRequired as exc:
            self._sync_retry_count(task)
            self._pause_for_tool_approval(
                task=task,
                tool_name=exc.tool_name,
                execution_id=exc.execution_id,
                approval_id=exc.approval_id,
                stage=execution_stage,
                role=execution_role,
            )
            return
        except Exception as exc:
            self._sync_retry_count(task)
            failed_event_type = EventType.TOOL_TIMED_OUT if isinstance(exc, ToolInvocationError) and exc.timed_out else EventType.TOOL_FAILED
            record_event(
                self.db,
                task_id=task.id,
                event_type=failed_event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=execution_stage,
                role=execution_role,
                tool_name=tool_name,
                message="Tool execution failed.",
                payload={"error": str(exc), "approval_id": approval_id},
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.EXECUTION_FAILED,
                source=EventSource.ORCHESTRATOR,
                stage=execution_stage,
                role=execution_role,
                tool_name=tool_name,
                message="Execution failed during tool execution.",
                payload={"error": str(exc)},
            )
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": str(exc),
            }
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=execution_role,
                source=EventSource.ORCHESTRATOR,
                message="Task failed during execution.",
            )
            return

        succeeded_event_type = EventType.KNOWLEDGE_RETRIEVED if tool_name == "knowledge.search" else EventType.TOOL_SUCCEEDED
        succeeded_message = (
            "Knowledge context packaged for the task."
            if tool_name == "knowledge.search"
            else "Tool execution completed."
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=succeeded_event_type,
            source=EventSource.TOOL_GATEWAY,
            stage=execution_stage,
            role=execution_role,
            tool_name=tool_name,
            message=succeeded_message,
            payload=result,
        )

        if task.scenario == "jira_issue_plan":
            result = {
                **result,
                "agent_plan": {
                    "objective": plan.objective,
                    "change_summary": plan.change_summary,
                    "change_explanation": plan.change_explanation,
                    "request_summary": plan.request_summary,
                    "affected_code_locations": [
                        {
                            "source_name": location.source_name,
                            "relative_path": location.relative_path,
                            "reason": location.reason,
                            "line_start": location.line_start,
                            "line_end": location.line_end,
                        }
                        for location in plan.affected_code_locations
                    ],
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "title": step.title,
                            "owner_role": step.owner_role.value,
                            "kind": step.kind,
                            "expected_output": step.expected_output,
                        }
                        for step in plan.steps
                    ],
                },
            }

        output_review = self.reviewer_agent.review_output(
            task_id=task.id,
            plan=plan,
            result=result,
        )
        task.review_json = output_review.review.model_dump(mode="json")
        task.pending_approval = False

        if output_review.review.verdict == "approved":
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.REVIEW_PASSED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message="Reviewer approved the execution output.",
                payload={"review": task.review_json},
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.EXECUTION_COMPLETED,
                source=EventSource.ORCHESTRATOR,
                stage=execution_stage,
                role=execution_role,
                tool_name=tool_name,
                message="Execution completed successfully.",
                payload={"approval_id": approval_id},
            )
            task.latest_result_json = {
                "status": TaskStatus.COMPLETED.value,
                "message": "Task completed after planner, reviewer, and execution stages.",
                "result": result,
                "review": task.review_json,
            }
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.COMPLETED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                source=EventSource.ORCHESTRATOR,
                message="Task completed after execution output review.",
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.FINAL_RESPONSE_EMITTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                message="Final response emitted for task.",
                payload={"tool_name": tool_name, "approval_id": approval_id},
            )
            return

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.REVIEW_FAILED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Reviewer rejected the execution output.",
            payload={"review": task.review_json},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_FAILED,
            source=EventSource.ORCHESTRATOR,
            stage=execution_stage,
            role=execution_role,
            tool_name=tool_name,
            message="Execution failed during output review.",
            payload={"approval_id": approval_id},
        )
        task.latest_result_json = {
            "status": TaskStatus.FAILED.value,
            "message": self._build_failed_output_message(
                plan=plan,
                result=result,
                review_summary=output_review.review.summary,
            ),
            "result": result,
            "review": task.review_json,
        }
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Task failed because the execution output did not pass review.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after execution review failure.",
            payload={"tool_name": tool_name, "approval_id": approval_id},
        )

    def _execute_develop_pipeline(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        approval_id: str | None = None,
    ) -> None:
        """Full pipeline: codegen -> sandbox -> test -> review -> approve -> writeback."""
        pipeline_state = self._load_develop_pipeline_state(task)

        user_lang = detect_user_language(task.request_text or "")
        pipeline_state.setdefault("user_lang", user_lang)

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.EXECUTING,
            new_stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            source=EventSource.ORCHESTRATOR,
            message="Jira 开发流水线已启动。" if user_lang == "zh" else "Task entered Jira issue development pipeline.",
            payload={"approval_id": approval_id},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            message="Jira issue development pipeline started.",
            payload={"plan_id": plan.plan_id, "approval_id": approval_id},
        )

        # Resolve once — reused throughout the pipeline to avoid repeated
        # disk/config lookups (previously called ~8 times per pipeline run).
        _pipeline_source_path = self._resolve_knowledge_source_path()

        context_files = self._gather_codegen_context(task=task, plan=plan)
        if not context_files:
            # Check if the plan expects new files to be created — if so, proceed with empty context
            has_planned_files = bool(plan.affected_code_locations)
            if not has_planned_files:
                self._fail_develop_pipeline(
                    task=task,
                    message="\u4ee3\u7801\u751f\u6210\u5931\u8d25\uff1a\u6ca1\u6709\u627e\u5230\u8ba1\u5212\u4e2d\u53d7\u5f71\u54cd\u6587\u4ef6\u7684\u4e0a\u4e0b\u6587\u3002",
                    payload={"plan_id": plan.plan_id},
                )
                return
            # For new-file-creation tasks, use a placeholder context so batch codegen proceeds
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_SKIPPED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                message=(
                    f"No existing files found in source tree. Plan has {len(plan.affected_code_locations)} "
                    f"target locations — proceeding as new-file-creation task."
                ),
                payload={
                    "planned_paths": [loc.relative_path for loc in plan.affected_code_locations],
                },
            )
            # Add planned file paths as empty stubs so the codegen prompt lists them
            for loc in plan.affected_code_locations:
                rel = self._normalize_codegen_path(loc.relative_path)
                if rel:
                    context_files[rel] = ""
            pipeline_state["_new_file_task"] = True

        # Also detect new-file-creation when context_files is non-empty but
        # the plan references paths that don't exist in the gathered context.
        source_path = _pipeline_source_path
        sandbox_dir = self._develop_sandbox_dir(task)
        if not pipeline_state.get("_new_file_task"):
            new_file_stubs_added = False
            for loc in plan.affected_code_locations:
                rel = self._normalize_codegen_path(loc.relative_path)
                if not rel or rel in context_files:
                    continue
                # Check if the file exists on disk
                exists = False
                if source_path and (source_path / rel).exists():
                    exists = True
                if sandbox_dir.exists() and (sandbox_dir / rel).exists():
                    exists = True
                if not exists:
                    context_files[rel] = ""
                    new_file_stubs_added = True
            if new_file_stubs_added:
                pipeline_state["_new_file_task"] = True

        # Tertiary detection: when the planner picked grounding files as
        # affected_code_locations instead of the intended targets, extract
        # filenames explicitly mentioned in the request text. If those files
        # don't exist on disk, treat them as new-file targets. This recovers
        # from planner mislabeling (common with weak LLMs).
        if not pipeline_state.get("_new_file_task"):
            request_files = self._extract_filenames_from_request(task.request_text or "")
            request_stubs_added = False
            for rel in request_files:
                rel_norm = self._normalize_codegen_path(rel)
                if not rel_norm or rel_norm in context_files:
                    continue
                exists = False
                if source_path and (source_path / rel_norm).exists():
                    exists = True
                if sandbox_dir.exists() and (sandbox_dir / rel_norm).exists():
                    exists = True
                if not exists:
                    context_files[rel_norm] = ""
                    request_stubs_added = True
            if request_stubs_added:
                pipeline_state["_new_file_task"] = True
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SKIPPED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    message=(
                        "Request text names files that don't exist on disk — "
                        "treating as new-file creation targets."
                    ),
                    payload={"request_new_files": list(request_files)},
                )

        pipeline_state["context_file_paths"] = list(context_files)
        pipeline_state["context_files"] = context_files

        # --- Evidence bundle gate (T-041-01) ---
        if not pipeline_state.get("evidence_bundle_done"):
            from app.services.evidence_bundle import build_evidence_bundle
            from app.services.spec_conformance import _has_destructive_verb

            translation = task.translation_json if isinstance(task.translation_json, dict) else {}
            try:
                evidence = build_evidence_bundle(
                    request_text=task.request_text,
                    normalized_request=translation.get("normalized_request"),
                    source_tree=_pipeline_source_path,
                    grounding_terms=translation.get("grounding_terms"),
                    planner_must_touch=getattr(plan, "must_touch_files", None) or [],
                    has_destructive_verb=_has_destructive_verb(task.request_text or ""),
                )
            except Exception as exc:
                evidence = None
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.KNOWLEDGE,
                    role=RoleName.KNOWLEDGE,
                    tool_name="evidence_bundle.build",
                    message=f"Evidence bundle errored: {exc}",
                    payload={"error": str(exc)},
                )
            if evidence is not None:
                pipeline_state["evidence_bundle"] = evidence.to_payload()
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=(
                        EventType.TOOL_SUCCEEDED
                        if evidence.verdict != "insufficient"
                        else EventType.EXECUTION_FAILED
                    ),
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.KNOWLEDGE,
                    role=RoleName.KNOWLEDGE,
                    tool_name="evidence_bundle.build",
                    message=evidence.reason,
                    payload=evidence.to_payload(),
                )
                if evidence.verdict == "insufficient":
                    self._fail_develop_pipeline(
                        task=task,
                        message=f"Evidence bundle insufficient: {evidence.reason}",
                        event_type=EventType.EXECUTION_FAILED,
                        stage=WorkflowStage.KNOWLEDGE,
                        role=RoleName.KNOWLEDGE,
                        payload=evidence.to_payload(),
                    )
                    return
                if evidence.must_touch_files and not getattr(plan, "must_touch_files", None):
                    plan.must_touch_files = evidence.must_touch_files
            pipeline_state["evidence_bundle_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        codegen_result = pipeline_state.get("codegen_result")
        if not isinstance(codegen_result, dict):
            # --- Fast path: deterministic rename if applicable ---
            rename_pair = self._detect_rename_pair(task)
            if rename_pair:
                pipeline_state["_rename_pair"] = rename_pair
                codegen_result = self._deterministic_rename(
                    context_files=context_files,
                    old_name=rename_pair[0],
                    new_name=rename_pair[1],
                )
                if codegen_result and codegen_result.get("diff"):
                    pipeline_state["codegen_result"] = codegen_result
                    pipeline_state["diff"] = codegen_result["diff"]
                    pipeline_state["files_changed"] = codegen_result.get("files_changed", [])
                    pipeline_state["codegen_provider"] = "deterministic_rename"
                    pipeline_state["file_summaries"] = codegen_result.get("file_summaries", [])
                    self._workspace_write_attempt_diff(
                        task,
                        pipeline_state,
                        diff=str(codegen_result.get("diff") or ""),
                    )
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        tool_name="codegen.deterministic_rename",
                        message=(
                            f"确定性重命名完成: {rename_pair[0]} → {rename_pair[1]}, "
                            f"修改了 {len(codegen_result.get('files_changed', []))} 个文件"
                        ),
                        payload=codegen_result.get("files_changed", []),
                    )
                else:
                    # Deterministic rename found no matches — log for debugging
                    # and fall through to LLM codegen.
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SKIPPED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        tool_name="codegen.deterministic_rename",
                        message=(
                            f"确定性重命名跳过: '{rename_pair[0]}' 在 {len(context_files)} 个上下文文件中未找到, "
                            f"回退到 LLM codegen"
                        ),
                        payload={
                            "old_name": rename_pair[0],
                            "new_name": rename_pair[1],
                            "context_file_count": len(context_files),
                            "context_file_paths": list(context_files.keys())[:10],
                        },
                    )

        if not isinstance(codegen_result, dict):
            # --- Batch codegen: split files into chunks of BATCH_SIZE ---
            # Separate new-file stubs (empty content) from existing files.
            # New-file stubs go into a single dedicated batch so they are
            # only generated once instead of duplicated across every batch.
            batch_size = 5
            # Filter out non-modifiable / excessively large files that waste
            # tokens and confuse the model (e.g. package-lock.json).
            _CODEGEN_EXCLUDE_PATTERNS = frozenset({
                "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                "composer.lock", "Gemfile.lock", "poetry.lock",
            })
            _CODEGEN_MAX_FILE_CHARS = 50_000  # ~50KB — skip enormous files
            context_files = {
                p: c for p, c in context_files.items()
                if p.split("/")[-1] not in _CODEGEN_EXCLUDE_PATTERNS
                and len(c) <= _CODEGEN_MAX_FILE_CHARS
            }
            existing_files = [(p, c) for p, c in context_files.items() if c.strip()]
            new_file_stubs = [(p, c) for p, c in context_files.items() if not c.strip()]

            # Use pipeline-level source path for batch construction + codegen calls
            _source_path = _pipeline_source_path

            # Per-file batching: each file gets its own codegen call. Cuts
            # single-call output size from 10K+ tokens (all files in one diff)
            # to ~500 tokens (one file's diff), eliminating LLM token-limit
            # truncation. Batches run in parallel via ThreadPoolExecutor.
            is_new_file_task = bool(pipeline_state.get("_new_file_task"))

            # Targets come from planner's must_touch_files (the files the
            # planner explicitly committed to modifying). existing_files may
            # include broader grounding from knowledge.search (e.g. package.json,
            # cors.json) that the model needs to see but MUST NOT rewrite.
            _must_touch = set(getattr(plan, "must_touch_files", None) or [])
            _must_touch_existing = [
                (p, c) for p, c in existing_files if p in _must_touch
            ]
            # expected_new_files from planner → stubs that become their own
            # batches so each new file gets an explicit codegen call scoped
            # to creating it (rather than relying on CLI agents to "remember"
            # to create them as a side-effect of another batch).
            _expected_new = list(getattr(plan, "expected_new_files", None) or [])
            _planner_new_stubs = [
                (p, "") for p in _expected_new
                if not any(p == np for np, _ in new_file_stubs)
            ]
            # Fallback: if planner didn't specify must_touch_files (empty set),
            # keep the legacy batched-single-call behavior to avoid dispatching
            # per-context-file, which blew up to 19 calls in testing.
            _has_targets = (
                bool(_must_touch_existing)
                or bool(new_file_stubs)
                or bool(_planner_new_stubs)
            )

            batches: list[dict[str, str]] = []
            if is_new_file_task and new_file_stubs:
                new_batch = dict(new_file_stubs)
                for p, c in existing_files[:3]:
                    new_batch.setdefault(p, c)
                batches.append(new_batch)
            elif _has_targets:
                if new_file_stubs:
                    new_batch = dict(new_file_stubs)
                    for p, c in existing_files[:3]:
                        new_batch.setdefault(p, c)
                    batches.append(new_batch)
                # Per-file: only planner-declared must_touch targets get batches.
                for path, content in _must_touch_existing:
                    batches.append({path: content})
                # Planner-declared new files: each gets its own stub batch with
                # a small grounding slice so the model understands context.
                for path, _ in _planner_new_stubs:
                    stub_batch: dict[str, str] = {path: ""}
                    for gp, gc in existing_files[:2]:
                        stub_batch.setdefault(gp, gc)
                    batches.append(stub_batch)
            else:
                # No must_touch targets at all — fall back to single batch
                # with everything, so codegen can still run (legacy path).
                batches.append(dict(context_files))

            merged_diff_parts: list[str] = []
            merged_files_changed: list[str] = []
            merged_file_summaries: list[dict[str, str]] = []
            seen_files: set[str] = set()
            codegen_provider = "unknown"

            # Pipe translation constraints into plan_json so codegen sees them
            _plan_json_for_codegen = dict(task.plan_json or plan.model_dump(mode="json"))
            _translation = task.translation_json or {}
            if _translation.get("constraints"):
                _plan_json_for_codegen["constraints"] = _translation["constraints"]

            parallel_max = getattr(
                self.tool_gateway.settings, "codegen_parallel_max", 2
            )
            parallel_max = max(1, min(parallel_max, len(batches)))

            # Main-thread: record TOOL_CALL_REQUESTED up-front so UI shows N
            # pending batches before any worker returns.
            for batch_idx, batch_files in enumerate(batches):
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_CALL_REQUESTED,
                    source=EventSource.TOOL_GATEWAY,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name="codegen.generate_patch",
                    message=(
                        f"Codegen batch {batch_idx + 1}/{len(batches)} dispatched "
                        f"(parallel_max={parallel_max}, files={list(batch_files.keys())[:2]})"
                    ),
                    payload={
                        "batch": batch_idx,
                        "files": list(batch_files.keys()),
                        "parallel_max": parallel_max,
                    },
                )
            commit_checkpoint(
                self.db, label=f"codegen_parallel_start_{len(batches)}_batches"
            )

            # Worker runs in pool thread. For multi-batch parallel, workers
            # bypass tool_gateway entirely and call CodeGenerator directly: no
            # DB writes in worker threads, so no SQLite write-lock contention.
            # ToolExecution + CostTracker rows are skipped on this path (TODO:
            # record them from main thread post-join). Single-batch path keeps
            # self.tool_gateway so test mocks still apply.
            _use_direct_codegen = len(batches) > 1

            def _worker_codegen(
                b_idx: int, b_files: dict[str, str]
            ) -> tuple[int, dict | None, Exception | None]:
                task_description = self._build_codegen_task_description(
                    task=task,
                    plan=plan,
                    pipeline_state=pipeline_state,
                    batch_files=b_files,
                )
                if _use_direct_codegen:
                    from app.services.codegen import CodeGenerator, CodegenError

                    try:
                        result = CodeGenerator(self.tool_gateway.settings).generate_patch(
                            task_id=task.id,
                            plan_json=_plan_json_for_codegen,
                            context_files=b_files,
                            task_description=task_description,
                            source_repo_path=str(_source_path) if _source_path else None,
                        )
                        dump = result.model_dump(mode="json")
                        # Defensive scope-lock: single-file batches must only
                        # MODIFY their target existing file. New-file creation
                        # hunks (detected by 'new file mode' header) are kept
                        # across all batches — multi-batch duplicate creation
                        # is deduplicated later by the merge step's seen_files
                        # set, so first batch wins.
                        if len(b_files) == 1:
                            target = next(iter(b_files.keys()))
                            diff_text = str(dump.get("diff") or "")
                            kept_sections: list[str] = []
                            kept_file_paths: list[str] = []
                            for section in re.split(
                                r"(?=^diff --git )", diff_text, flags=re.MULTILINE
                            ):
                                section = section.strip()
                                if not section:
                                    continue
                                m = re.match(r"diff --git a/(.+?) b/", section)
                                if m is None:
                                    continue
                                hunk_file = m.group(1).strip()
                                is_target = (
                                    hunk_file == target
                                    or hunk_file.endswith("/" + target)
                                )
                                # Detect new-file creation from diff header:
                                # git formats these as 'new file mode 100644'
                                # plus 'index 0000000..xxxxxxx'.
                                is_new_file = (
                                    "new file mode " in section
                                    or bool(re.search(r"^index 0{7,}\.\.", section, re.MULTILINE))
                                )
                                if is_target or is_new_file:
                                    kept_sections.append(section)
                                    kept_file_paths.append(hunk_file)
                            dump["diff"] = "\n".join(kept_sections)
                            fc = dump.get("files_changed") or []
                            # Keep the target + any new files that survived
                            dump["files_changed"] = [
                                f for f in fc
                                if f == target
                                or f.endswith("/" + target)
                                or f in kept_file_paths
                                or any(k.endswith("/" + f) for k in kept_file_paths)
                            ]
                        return b_idx, dump, None
                    except CodegenError as exc:
                        return b_idx, None, exc
                    except Exception as exc:  # noqa: BLE001
                        return b_idx, None, exc
                else:
                    try:
                        result = self.tool_gateway.execute(
                            task_id=task.id,
                            tool_name="codegen.generate_patch",
                            payload={
                                "plan_json": _plan_json_for_codegen,
                                "context_files": b_files,
                                "task_description": task_description,
                                "source_repo_path": str(_source_path) if _source_path else None,
                            },
                            actor_context={"actor_name": actor_name, "task_id": task.id},
                            session_id=task.session_id,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            approval_id=approval_id,
                        )
                        return b_idx, result, None
                    except Exception as exc:  # noqa: BLE001
                        return b_idx, None, exc

            results_by_idx: dict[int, dict] = {}
            with ThreadPoolExecutor(
                max_workers=parallel_max, thread_name_prefix="codegen"
            ) as pool:
                futures = [
                    pool.submit(_worker_codegen, i, batches[i])
                    for i in range(len(batches))
                ]
                for fut in as_completed(futures):
                    batch_idx, batch_result, err = fut.result()
                    batch_label = f"batch {batch_idx + 1}/{len(batches)}"
                    if err is not None:
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.TOOL_GATEWAY,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="codegen.generate_patch",
                            message=f"Codegen {batch_label} failed: {err}",
                            payload={
                                "batch": batch_idx,
                                "files": list(batches[batch_idx].keys()),
                                "error": str(err)[:500],
                            },
                        )
                    elif batch_result is not None:
                        results_by_idx[batch_idx] = batch_result
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.TOOL_GATEWAY,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="codegen.generate_patch",
                            message=(
                                f"Codegen {batch_label} done "
                                f"({len(batch_result.get('files_changed') or [])} files)"
                            ),
                            payload=batch_result,
                        )
                    commit_checkpoint(
                        self.db, label=f"codegen_batch_{batch_idx}_done"
                    )

            # Merge by batch index so downstream file ordering is stable.
            for batch_idx in sorted(results_by_idx):
                batch_result = results_by_idx[batch_idx]
                batch_diff = str(batch_result.get("diff") or "").strip()
                batch_changed = batch_result.get("files_changed")
                if isinstance(batch_changed, list):
                    novel_files = [f for f in batch_changed if f not in seen_files]
                    if novel_files and batch_diff:
                        if seen_files:
                            batch_diff = self._strip_duplicate_diff_hunks(
                                batch_diff, seen_files,
                            )
                        if batch_diff.strip():
                            merged_diff_parts.append(batch_diff)
                    for f in novel_files:
                        seen_files.add(f)
                    merged_files_changed.extend(novel_files)
                elif batch_diff:
                    merged_diff_parts.append(batch_diff)
                batch_summaries = batch_result.get("file_summaries")
                if isinstance(batch_summaries, list):
                    merged_file_summaries.extend(
                        s for s in batch_summaries
                        if isinstance(s, dict) and s.get("path") not in seen_files
                    )
                codegen_provider = str(
                    batch_result.get("provider_name") or codegen_provider
                )

            if not merged_diff_parts:
                self._fail_develop_pipeline(
                    task=task,
                    message="\u4ee3\u7801\u751f\u6210\u5931\u8d25\uff1a\u6240\u6709\u6279\u6b21\u5747\u672a\u751f\u6210\u6709\u6548\u7684 diff\u3002",
                    payload={"plan_id": plan.plan_id, "batches": len(batches)},
                )
                return

            codegen_result = {
                "diff": "\n".join(merged_diff_parts),
                "files_changed": merged_files_changed,
                "file_summaries": merged_file_summaries,
                "provider_name": codegen_provider,
            }
            pipeline_state["codegen_result"] = codegen_result
            pipeline_state["diff"] = codegen_result["diff"]
            pipeline_state["files_changed"] = merged_files_changed
            pipeline_state["codegen_provider"] = codegen_provider
            pipeline_state["file_summaries"] = merged_file_summaries
            self._workspace_write_attempt_diff(
                task,
                pipeline_state,
                diff=str(codegen_result.get("diff") or ""),
            )
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_SUCCEEDED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name="codegen.generate_patch",
                message=f"\u4ee3\u7801\u751f\u6210\u5b8c\u6210\uff0c\u4fee\u6539\u4e86 {len(merged_files_changed)} \u4e2a\u6587\u4ef6\uff08{len(batches)} \u6279\uff09",
                payload={"files_changed": merged_files_changed, "batches": len(batches)},
            )

        diff = str(codegen_result.get("diff") or "").strip()
        if not diff:
            self._fail_develop_pipeline(
                task=task,
                message="\u4ee3\u7801\u751f\u6210\u5931\u8d25\uff1a\u4ee3\u7801\u751f\u6210\u5de5\u5177\u6ca1\u6709\u8fd4\u56de\u53ef\u5e94\u7528\u7684 diff\u3002",
                payload={"codegen_result": codegen_result},
            )
            return
        pipeline_state.setdefault("diff", diff)
        files_changed = codegen_result.get("files_changed")
        pipeline_state.setdefault("files_changed", files_changed if isinstance(files_changed, list) else [])
        pipeline_state.setdefault("codegen_provider", str(codegen_result.get("provider_name") or "unknown"))

        sandbox_result = pipeline_state.get("sandbox_result")
        if not isinstance(sandbox_result, dict):
            try:
                sandbox_setup_result = self._ensure_develop_sandbox(task=task, plan=plan)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name="sandbox.clone",
                    message="Development sandbox is ready.",
                    payload=sandbox_setup_result,
                )
                sandbox_result = self._execute_develop_tool(
                    task=task,
                    actor_name=actor_name,
                    tool_name="sandbox.apply_patch",
                    payload={
                        "task_id": task.id,
                        "patch": diff,
                        "context_files": context_files,
                        "commit": True,
                        "commit_message": f"Apply generated patch for {task.id}",
                    },
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    approval_id=approval_id,
                    pipeline_state=pipeline_state,
                )
            except Exception as exc:
                self._fail_develop_pipeline(
                    task=task,
                    message=f"Sandbox patch application failed: {exc}",
                    payload={"error": str(exc), "plan_id": plan.plan_id},
                )
                return
            if sandbox_result is None:
                return
            pipeline_state["sandbox_result"] = sandbox_result
            pipeline_state["patch_method"] = str(sandbox_result.get("method") or "git_apply")
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Completeness check ---
        # Strategy varies by task type:
        # - Rename tasks: grep for OLD identifier (should be gone)
        # - New-file-creation tasks: check that target files exist and are non-empty
        # - Other tasks: grep for grounding_terms code symbols
        completeness = pipeline_state.get("completeness_check")
        if not isinstance(completeness, dict):
            sandbox_dir = self._develop_sandbox_dir(task)
            is_new_file_task = pipeline_state.get("_new_file_task", False)

            if is_new_file_task:
                # For new-file tasks, just verify the target files exist
                planned_paths = [
                    self._normalize_codegen_path(loc.relative_path)
                    for loc in plan.affected_code_locations
                ]
                missing = [
                    p for p in planned_paths
                    if p and not (sandbox_dir / p).exists()
                ]
                if missing:
                    completeness = {
                        "complete": False,
                        "remaining_files": len(missing),
                        "remaining_hits": len(missing),
                        "details": {p: 0 for p in missing},
                    }
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        tool_name="completeness_check",
                        message=f"Completeness check: {len(missing)} target file(s) not created: {', '.join(missing)}",
                        payload={"missing_files": missing},
                    )
                else:
                    completeness = {"complete": True, "remaining_files": 0, "remaining_hits": 0}
            else:
                rename_pair = pipeline_state.get("_rename_pair") or (
                    self._detect_rename_pair(task)
                )
                if rename_pair:
                    # Rename task: grep for OLD identifier (should be gone)
                    pipeline_state["_rename_pair"] = rename_pair
                    completeness_keywords = [rename_pair[0]]
                else:
                    # Non-rename tasks: keyword completeness check only makes
                    # sense for destructive operations (remove/delete/replace)
                    # where grounding_terms should disappear after the patch.
                    # For additive tasks (add JSDoc, add feature) the terms
                    # will still be present — grepping for them produces false
                    # negatives that trigger wasteful auto-retries.
                    from app.services.spec_conformance import _has_destructive_verb
                    if _has_destructive_verb(task.request_text or ""):
                        translation = task.translation_json or {}
                        completeness_keywords = [
                            t for t in translation.get("grounding_terms", [])
                            if isinstance(t, str)
                            and len(t) >= 3
                            and " " not in t  # single-word identifiers only
                        ]
                    else:
                        completeness_keywords = []
                already_changed: set[str] = set()
                for p in pipeline_state.get("files_changed", []):
                    already_changed.add(self._normalize_codegen_path(str(p)) or str(p))
                if completeness_keywords and sandbox_dir.exists():
                    remaining = self._grep_source_tree(sandbox_dir, completeness_keywords)
                    remaining = {
                        path: lines for path, lines in remaining.items()
                        if (self._normalize_codegen_path(path) or path) not in already_changed
                    }
                    if remaining:
                        remaining_summary = {
                            path: len(lines) for path, lines in remaining.items()
                        }
                        completeness = {
                            "complete": False,
                            "remaining_files": len(remaining),
                            "remaining_hits": sum(remaining_summary.values()),
                            "details": remaining_summary,
                        }
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="completeness_check",
                            message=(
                                f"Completeness check: {len(remaining)} file(s) still "
                                f"contain target keywords after patch."
                            ),
                            payload=remaining_summary,
                        )
                    else:
                        completeness = {"complete": True, "remaining_files": 0, "remaining_hits": 0}
                else:
                    completeness = {"complete": True, "remaining_files": 0, "remaining_hits": 0}

            pipeline_state["completeness_check"] = completeness
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Auto-retry: re-codegen missed files from completeness check ---
        retry_done = pipeline_state.get("retry_done", False)
        if (
            not retry_done
            and isinstance(completeness, dict)
            and not completeness.get("complete")
            and completeness.get("remaining_files", 0) > 0
        ):
            retry_file_paths = list((completeness.get("details") or {}).keys())
            sandbox_dir = self._develop_sandbox_dir(task)
            source_path = _pipeline_source_path
            retry_context: dict[str, str] = {}
            for rpath in retry_file_paths:
                content = self._read_context_file(
                    source_path=source_path,
                    sandbox_dir=sandbox_dir,
                    relative_path=rpath,
                )
                if content is not None:
                    retry_context[rpath] = content

            if retry_context:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_CALL_REQUESTED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name="codegen.retry",
                    message=f"Auto-retry codegen for {len(retry_context)} missed file(s): {', '.join(retry_context.keys())}",
                    payload={"retry_files": list(retry_context.keys())},
                )

                retry_merged_diff_parts: list[str] = []
                retry_merged_files_changed: list[str] = []

                # Fast path: if this is a rename task, use deterministic
                # rename for retry too — no LLM call needed.
                retry_rename_pair = pipeline_state.get("_rename_pair")
                if retry_rename_pair:
                    retry_det = self._deterministic_rename(
                        context_files=retry_context,
                        old_name=retry_rename_pair[0],
                        new_name=retry_rename_pair[1],
                    )
                    if retry_det and retry_det.get("diff"):
                        retry_merged_diff_parts.append(str(retry_det["diff"]))
                        retry_merged_files_changed.extend(retry_det.get("files_changed", []))
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="codegen.deterministic_rename_retry",
                            message=(
                                f"Deterministic rename retry: {retry_rename_pair[0]} → {retry_rename_pair[1]}, "
                                f"{len(retry_merged_files_changed)} file(s)"
                            ),
                            payload={"files_changed": retry_merged_files_changed},
                        )
                else:
                    # LLM-based retry: batch retry files (batch_size=3)
                    retry_batch_size = 5
                    retry_items = list(retry_context.items())
                    retry_batches = [
                        dict(retry_items[i : i + retry_batch_size])
                        for i in range(0, len(retry_items), retry_batch_size)
                    ]

                    for rb_idx, rb_files in enumerate(retry_batches):
                        if rb_idx > 0:
                            time.sleep(15)
                        rb_label = f"retry batch {rb_idx + 1}/{len(retry_batches)}"
                        try:
                            rb_result = self._execute_develop_tool(
                                task=task,
                                actor_name=actor_name,
                                tool_name="codegen.generate_patch",
                                payload={
                                    "plan_json": task.plan_json or plan.model_dump(mode="json"),
                                    "context_files": rb_files,
                                    "task_description": self._build_codegen_task_description(
                                        task=task,
                                        plan=plan,
                                        pipeline_state=pipeline_state,
                                        batch_files=rb_files,
                                    ),
                                    "source_repo_path": str(source_path) if source_path else None,
                                },
                                stage=WorkflowStage.ACTION,
                                role=RoleName.ACTION,
                                approval_id=approval_id,
                                pipeline_state=pipeline_state,
                            )
                        except Exception as exc:
                            record_event(
                                self.db,
                                task_id=task.id,
                                event_type=EventType.TOOL_FAILED,
                                source=EventSource.ORCHESTRATOR,
                                stage=WorkflowStage.ACTION,
                                role=RoleName.ACTION,
                                tool_name="codegen.retry",
                                message=f"Retry codegen {rb_label} failed: {exc}",
                                payload={"batch": rb_idx, "files": list(rb_files.keys())},
                            )
                            continue

                        if rb_result is None:
                            continue
                        rb_diff = str(rb_result.get("diff") or "").strip()
                        if rb_diff:
                            retry_merged_diff_parts.append(rb_diff)
                        rb_changed = rb_result.get("files_changed")
                        if isinstance(rb_changed, list):
                            retry_merged_files_changed.extend(rb_changed)

                # Apply merged retry diff to sandbox
                if retry_merged_diff_parts:
                    retry_diff = "\n".join(retry_merged_diff_parts)
                    try:
                        self._execute_develop_tool(
                            task=task,
                            actor_name=actor_name,
                            tool_name="sandbox.apply_patch",
                            payload={
                                "task_id": task.id,
                                "patch": retry_diff,
                                "context_files": retry_context,
                                "commit": True,
                                "commit_message": f"Apply retry patch for {task.id}",
                            },
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            approval_id=approval_id,
                            pipeline_state=pipeline_state,
                        )
                        existing_changed = pipeline_state.get("files_changed", [])
                        if isinstance(existing_changed, list):
                            pipeline_state["files_changed"] = existing_changed + retry_merged_files_changed
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="codegen.retry",
                            message=f"Retry patch applied, {len(retry_merged_files_changed)} additional file(s) modified.",
                            payload={"retry_files_changed": retry_merged_files_changed},
                        )
                    except Exception as exc:
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.ACTION,
                            role=RoleName.ACTION,
                            tool_name="codegen.retry_patch",
                            message=f"Retry patch apply failed: {exc}",
                            payload={"error": str(exc)},
                        )

            pipeline_state["retry_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        test_result = pipeline_state.get("test_result")
        if not isinstance(test_result, dict):
            try:
                test_result = self._execute_develop_tool(
                    task=task,
                    actor_name=actor_name,
                    tool_name="test_pipeline.run",
                    payload={"task_id": task.id},
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    approval_id=approval_id,
                    pipeline_state=pipeline_state,
                )
            except Exception as exc:
                error_message = str(exc)
                if self._is_missing_test_pipeline_config_error(error_message):
                    test_result = {
                        "status": "skipped",
                        "overall_passed": True,
                        "skipped_count": 1,
                        "reason": error_message,
                    }
                    pipeline_state["test_skipped"] = True
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SKIPPED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        tool_name="test_pipeline.run",
                        message=f"Test pipeline skipped: {error_message}",
                        payload={"error": error_message, "plan_id": plan.plan_id},
                    )
                else:
                    self._fail_develop_pipeline(
                        task=task,
                        message=f"\u6d4b\u8bd5\u672a\u901a\u8fc7\uff1a{exc}",
                        payload={"error": error_message, "plan_id": plan.plan_id},
                    )
                    return
            if test_result is None:
                return
            pipeline_state["test_result"] = test_result
            pipeline_state["test_skipped"] = str(test_result.get("status") or "").casefold() == "skipped"
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        if not bool(test_result.get("overall_passed")):
            failed_count = self._safe_int(test_result.get("failed_count"), default=1)
            self._fail_develop_pipeline(
                task=task,
                message=f"\u6d4b\u8bd5\u672a\u901a\u8fc7\uff1a{failed_count} \u4e2a\u5931\u8d25",
                payload={"test_result": test_result, "plan_id": plan.plan_id},
            )
            return

        # --- Diff shape check (T-041-02 + T-041-03) ---
        if not pipeline_state.get("diff_shape_done"):
            from app.services.diff_shape_checker import check_diff_shape
            from app.services.spec_conformance import _classify_files_in_diff

            file_shapes = _classify_files_in_diff(diff)
            if diff.strip() and file_shapes:
                try:
                    shape_report = check_diff_shape(
                        request_text=task.request_text or "",
                        diff=diff,
                        file_shapes=file_shapes,
                    )
                except Exception as exc:
                    shape_report = None
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="diff_shape.check",
                        message=f"Diff shape check errored: {exc}",
                        payload={"error": str(exc)},
                    )
                if shape_report is not None:
                    pipeline_state["diff_shape"] = shape_report.to_payload()
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED if not shape_report.blocked else EventType.REVIEW_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="diff_shape.check",
                        message=(
                            "Diff shape check passed."
                            if not shape_report.blocked
                            else f"Diff shape check blocked: {'; '.join(f.message for f in shape_report.findings if f.severity == 'block')}"
                        ),
                        payload=shape_report.to_payload(),
                    )
                    if shape_report.blocked:
                        self._fail_develop_pipeline(
                            task=task,
                            event_type=EventType.REVIEW_FAILED,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            message=f"Diff shape: {'; '.join(f.message for f in shape_report.findings if f.severity == 'block')}",
                            payload=shape_report.to_payload(),
                        )
                        return
            pipeline_state["diff_shape_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Compile gate (T-040 defense line 5) with repair loop ---
        if not pipeline_state.get("compile_gate_done"):
            from app.services.compile_gate import run_compile_gate

            sandbox_dir = self._develop_sandbox_dir(task)
            changed = pipeline_state.get("files_changed") or []
            max_compile_passes = 2  # initial check + 1 repair attempt

            for compile_pass in range(max_compile_passes):
                if not (sandbox_dir.exists() and changed):
                    break

                try:
                    compile_result = run_compile_gate(
                        sandbox_dir=sandbox_dir,
                        changed_files=changed,
                    )
                except Exception as exc:
                    compile_result = None
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="compile_gate.check",
                        message=f"Compile gate errored: {exc}",
                        payload={"error": str(exc)},
                    )
                    break  # Can't recover from an internal error

                if compile_result is None:
                    break

                pipeline_state["compile_gate"] = {
                    "passed": compile_result.passed,
                    "errors": compile_result.errors,
                }
                self._workspace_write_attempt_compile(
                    task,
                    pipeline_state,
                    result_dict=pipeline_state["compile_gate"],
                )

                if compile_result.passed:
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="compile_gate.check",
                        message=compile_result.summary(),
                        payload=pipeline_state["compile_gate"],
                    )
                    break  # Gate passed

                # Gate failed — attempt repair on first pass only
                if compile_pass == 0:
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.REVIEW_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="compile_gate.check",
                        message=f"Compile gate failed (attempting repair): {compile_result.summary()}",
                        payload=pipeline_state["compile_gate"],
                    )
                    repaired, repair_touched = self._attempt_compile_repair(
                        task=task,
                        actor_name=actor_name,
                        compile_errors=compile_result.errors,
                        sandbox_dir=sandbox_dir,
                        pipeline_state=pipeline_state,
                        approval_id=approval_id,
                    )
                    if repaired:
                        # Merge repair-touched files into changed so
                        # the next compile gate pass also checks them.
                        changed = list(set(changed) | set(repair_touched))
                        continue  # Re-run compile gate after repair
                    # Repair failed or produced no changes — fall through to fail

                # Final failure (repair exhausted or skipped)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.REVIEW_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="compile_gate.check",
                    message=compile_result.summary(),
                    payload=pipeline_state["compile_gate"],
                )
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=f"Compile gate: {compile_result.summary()}",
                    payload=pipeline_state["compile_gate"],
                )
                return

            pipeline_state["compile_gate_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Runtime validation gate (with 1 repair cycle) ---
        if not pipeline_state.get("runtime_validation_done"):
            from app.services.runtime_validation import validate_diff_semantics, build_repair_prompt
            import logging as _rv_logging

            _rv_logger = _rv_logging.getLogger("orchestrator.runtime_validation")
            _rv_max_passes = 2  # initial check + 1 repair attempt

            for rv_pass in range(_rv_max_passes):
                try:
                    rv_report = validate_diff_semantics(
                        diff=diff,
                        context_files=pipeline_state.get("context_files", {}),
                        request_text=task.request_text or "",
                    )
                except Exception as exc:
                    rv_report = None
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="runtime_validation.check",
                        message=f"Runtime validation errored: {exc}",
                        payload={"error": str(exc)},
                    )
                    break

                pipeline_state["runtime_validation"] = rv_report.to_payload()
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=(
                        EventType.TOOL_SUCCEEDED
                        if rv_report.passed
                        else EventType.REVIEW_FAILED
                    ),
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="runtime_validation.check",
                    message=rv_report.summary(),
                    payload=rv_report.to_payload(),
                )

                for finding in rv_report.findings:
                    _rv_logger.warning(
                        "Runtime validation [%s] %s: %s",
                        finding.severity, finding.file, finding.message,
                    )

                if rv_report.passed:
                    break  # Gate passed

                # --- Attempt semantic repair (first pass only) ---
                if rv_pass == 0:
                    repair_prompt = build_repair_prompt(rv_report.findings)
                    if repair_prompt:
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_CALL_REQUESTED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="runtime_validation.repair",
                            message="Attempting semantic repair based on runtime validation findings",
                        )

                        time.sleep(15)  # Rate-limit cooldown

                        repair_payload = {
                            "plan_json": {"objective": "Fix runtime validation issues", "steps": []},
                            "context_files": pipeline_state.get("context_files", {}),
                            "task_description": repair_prompt,
                        }
                        try:
                            repair_result = self._execute_develop_tool(
                                task=task,
                                actor_name=actor_name,
                                tool_name="codegen.generate_patch",
                                payload=repair_payload,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                approval_id=approval_id,
                                pipeline_state=pipeline_state,
                            )
                            repair_diff = str((repair_result or {}).get("diff", "")).strip()
                            if repair_diff:
                                # Apply repair diff to sandbox and re-run validation
                                diff = repair_diff
                                pipeline_state["diff"] = diff
                                record_event(
                                    self.db,
                                    task_id=task.id,
                                    event_type=EventType.TOOL_SUCCEEDED,
                                    source=EventSource.ORCHESTRATOR,
                                    stage=WorkflowStage.REVIEW,
                                    role=RoleName.REVIEWER,
                                    tool_name="runtime_validation.repair",
                                    message="Semantic repair produced a patch; re-validating",
                                )
                                continue  # Re-run validation with repaired diff
                            else:
                                record_event(
                                    self.db,
                                    task_id=task.id,
                                    event_type=EventType.TOOL_FAILED,
                                    source=EventSource.ORCHESTRATOR,
                                    stage=WorkflowStage.REVIEW,
                                    role=RoleName.REVIEWER,
                                    tool_name="runtime_validation.repair",
                                    message="Semantic repair produced no diff",
                                )
                        except Exception as exc:
                            record_event(
                                self.db,
                                task_id=task.id,
                                event_type=EventType.TOOL_FAILED,
                                source=EventSource.ORCHESTRATOR,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                tool_name="runtime_validation.repair",
                                message=f"Semantic repair codegen failed: {exc}",
                            )

                # Final failure — repair exhausted or not attempted
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=f"Runtime validation: {rv_report.summary()}",
                    payload=rv_report.to_payload(),
                )
                return

            pipeline_state["runtime_validation_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        review_result = pipeline_state.get("review_result")
        if not isinstance(review_result, dict):
            try:
                review_result = self._execute_develop_tool(
                    task=task,
                    actor_name=actor_name,
                    tool_name="diff_reviewer.review",
                    payload={
                        "diff": diff,
                        "test_result": test_result,
                        "task_description": task.request_text,
                        "max_diff_size": 200_000,
                    },
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    approval_id=approval_id,
                    pipeline_state=pipeline_state,
                )
            except Exception as exc:
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=f"\u4ee3\u7801\u5ba1\u67e5\u672a\u901a\u8fc7\uff1a{exc}",
                    payload={"error": str(exc), "plan_id": plan.plan_id},
                )
                return
            if review_result is None:
                return
            pipeline_state["review_result"] = review_result
            pipeline_state["review_verdict"] = str(review_result.get("verdict") or "")
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        if str(review_result.get("verdict") or "").casefold() == "block":
            violations = self._format_review_violations(review_result)
            self._fail_develop_pipeline(
                task=task,
                event_type=EventType.REVIEW_FAILED,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message=f"\u4ee3\u7801\u5ba1\u67e5\u672a\u901a\u8fc7\uff1a{violations}",
                payload={"review_result": review_result, "plan_id": plan.plan_id},
            )
            return

        # --- Spec conformance gate (T-038) ---
        # Hard rules that catch "creative avoidance": shadow implementations,
        # unchanged hit counts on anchors the request asked to remove, and
        # patches that don't touch any file actually containing the anchors.
        # Runs after diff_reviewer so the LLM-graded review has already had
        # its say; conformance failures here mean the diff shape does not
        # match the task intent regardless of code quality.
        conformance_report = pipeline_state.get("conformance_report")
        if not isinstance(conformance_report, ConformanceReport):
            translation = task.translation_json if isinstance(task.translation_json, dict) else {}
            normalized_request = translation.get("normalized_request") if translation else None
            try:
                conformance_report = check_spec_conformance(
                    request_text=task.request_text,
                    normalized_request=normalized_request if isinstance(normalized_request, str) else None,
                    diff=diff,
                    source_tree=_pipeline_source_path,
                    must_touch_files=getattr(plan, "must_touch_files", []) or [],
                )
            except Exception as exc:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="spec_conformance.check",
                    message=f"Spec conformance check errored and was skipped: {exc}",
                    payload={"error": str(exc)},
                )
                conformance_report = None
            if isinstance(conformance_report, ConformanceReport):
                # store only the JSON-safe payload in pipeline_state so
                # persistence (both mid-pipeline flushes and the final
                # latest_result_json write) never sees the dataclass. The
                # ConformanceReport local is used for the block/retry
                # logic below but is not persisted.
                pipeline_state["conformance_report"] = conformance_report.to_payload()
                self._workspace_write_attempt_review(
                    task,
                    pipeline_state,
                    report_dict=pipeline_state["conformance_report"],
                    narrative=(
                        "Spec conformance passed."
                        if not conformance_report.blocked
                        else "\n".join(conformance_report.block_messages())
                    ),
                )
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=(
                        EventType.TOOL_SUCCEEDED
                        if not conformance_report.blocked
                        else EventType.REVIEW_FAILED
                    ),
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="spec_conformance.check",
                    message=(
                        "Spec conformance passed."
                        if not conformance_report.blocked
                        else "Spec conformance blocked the diff."
                    ),
                    payload=conformance_report.to_payload(),
                )
                if not conformance_report.blocked:
                    # T-038 goal-evidence attestation: positive proof that
                    # each destructive sub-goal actually landed. Runs only
                    # on the pass path so the final task result carries a
                    # machine-checkable summary of what the patch changed.
                    try:
                        attestation = build_goal_attestation(
                            request_text=task.request_text,
                            normalized_request=(
                                normalized_request
                                if isinstance(normalized_request, str)
                                else None
                            ),
                            diff=diff,
                            source_tree=_pipeline_source_path,
                        )
                    except Exception as exc:
                        attestation = {"error": str(exc)}
                    pipeline_state["goal_attestation"] = attestation
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="spec_conformance.attest",
                        message=(
                            "Goal attestation: "
                            + ("all goals met" if attestation.get("all_goals_met") else "partial")
                        ),
                        payload=attestation,
                    )

        if isinstance(conformance_report, ConformanceReport) and conformance_report.blocked:
            blocks = "; ".join(conformance_report.block_messages()) or "unspecified"
            attempts_used = int(pipeline_state.get("conformance_attempts", 0) or 0)
            if attempts_used + 1 < self.MAX_CONFORMANCE_ATTEMPTS:
                # T-038-A: clear downstream state, reset sandbox, push the
                # block reasons into pipeline_state["conformance_feedback"]
                # so the next codegen pass sees them, then recurse. The
                # recursion adds one duplicate EXECUTION_STARTED event but
                # otherwise re-runs only codegen→apply→review→conformance.
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SKIPPED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="spec_conformance.retry",
                    message=(
                        f"Spec conformance failed (attempt {attempts_used + 1}/"
                        f"{self.MAX_CONFORMANCE_ATTEMPTS}); resetting sandbox "
                        "and re-running codegen with feedback."
                    ),
                    payload={
                        "attempt": attempts_used + 1,
                        "feedback": conformance_report.block_messages(),
                    },
                )
                self._reset_for_conformance_retry(
                    task=task,
                    pipeline_state=pipeline_state,
                    feedback=conformance_report.block_messages(),
                )
                # Cooldown before retry — avoids rate-limiting from
                # back-to-back Claude Code CLI calls.
                time.sleep(30)
                return self._execute_develop_pipeline(
                    task=task,
                    actor_name=actor_name,
                    plan=plan,
                    approval_id=approval_id,
                )

            self._fail_develop_pipeline(
                task=task,
                event_type=EventType.REVIEW_FAILED,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message=(
                    "\u89c4\u8303\u4e00\u81f4\u6027\u68c0\u67e5\u672a\u901a\u8fc7\uff1a" + blocks
                ),
                payload={
                    "conformance_report": conformance_report.to_payload(),
                    "plan_id": plan.plan_id,
                    "attempts_used": attempts_used + 1,
                },
            )
            return

        # --- T-041-06: Failing test first gate ---
        if not pipeline_state.get("failing_test_gate_done"):
            from app.services.failing_test_gate import check_failing_test_gate
            from app.services.spec_conformance import _classify_files_in_diff as _clf_diff

            ft_shapes = _clf_diff(diff) if diff.strip() else {}
            try:
                ft_report = check_failing_test_gate(
                    request_text=task.request_text or "",
                    file_shapes=ft_shapes,
                    test_result=test_result if isinstance(test_result, dict) else None,
                )
            except Exception as exc:
                ft_report = None
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="failing_test_gate.check",
                    message=f"Failing test gate errored: {exc}",
                    payload={"error": str(exc)},
                )
            if ft_report is not None:
                pipeline_state["failing_test_gate"] = ft_report.to_payload()
                if ft_report.findings:
                    record_event(
                        self.db, task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED if ft_report.verdict != "block" else EventType.REVIEW_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                        tool_name="failing_test_gate.check",
                        message=f"Failing test gate: {ft_report.verdict} ({len(ft_report.findings)} findings)",
                        payload=ft_report.to_payload(),
                    )
                    if ft_report.verdict == "block":
                        self._fail_develop_pipeline(
                            task=task,
                            event_type=EventType.REVIEW_FAILED,
                            stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                            message=f"Failing test gate: {ft_report.findings[0].message}",
                            payload=ft_report.to_payload(),
                        )
                        return
            pipeline_state["failing_test_gate_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- T-041-08: Goal decomposition + per-file justification ---
        if not pipeline_state.get("goal_decomp_done"):
            from app.services.goal_decomposition import decompose_and_verify
            from app.services.spec_conformance import _classify_files_in_diff as _clf_diff2

            gd_shapes = _clf_diff2(diff) if diff.strip() else {}
            try:
                goal_report = decompose_and_verify(
                    request_text=task.request_text or "",
                    diff=diff,
                    file_shapes=gd_shapes,
                    source_tree=_pipeline_source_path,
                    attestation=pipeline_state.get("goal_attestation"),
                )
            except Exception as exc:
                goal_report = None
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="goal_decomposition.check",
                    message=f"Goal decomposition errored: {exc}",
                    payload={"error": str(exc)},
                )
            if goal_report is not None:
                pipeline_state["goal_decomposition"] = goal_report.to_payload()
                # Comment-only escalation: if any unjustified file's changes
                # are purely comments/whitespace, flag as block — that's the
                # "CLI agent added a self-documenting note to placate review"
                # pattern we saw in P69-8 task 5de6b5d3.
                from app.services.comment_only_detector import classify_diff
                comment_reports = classify_diff(diff)
                comment_only_unjustified: list[str] = []
                for unjf in goal_report.unjustified_files or []:
                    # match by suffix because diff paths may have different prefixes
                    for rep_path, rep in comment_reports.items():
                        if (
                            rep.is_comment_only
                            and (rep_path == unjf or rep_path.endswith("/" + unjf) or unjf.endswith("/" + rep_path))
                        ):
                            comment_only_unjustified.append(rep_path)
                            break
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="goal_decomposition.check",
                    message=(
                        f"Goals: {len(goal_report.sub_goals)}, "
                        f"all met: {goal_report.all_goals_met}, "
                        f"unjustified files: {goal_report.unjustified_files}"
                        + (
                            f", comment-only unjustified: {comment_only_unjustified}"
                            if comment_only_unjustified else ""
                        )
                    ),
                    payload={
                        **goal_report.to_payload(),
                        "comment_only_unjustified": comment_only_unjustified,
                    },
                )
                if comment_only_unjustified:
                    self._fail_develop_pipeline(
                        task=task,
                        message=(
                            "Goal decomposition escalated to block: "
                            f"{len(comment_only_unjustified)} file(s) had comment-only "
                            f"changes that don't advance any goal: "
                            + ", ".join(comment_only_unjustified[:3])
                            + (f" (+{len(comment_only_unjustified) - 3} more)"
                               if len(comment_only_unjustified) > 3 else "")
                        ),
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        payload={
                            "comment_only_unjustified": comment_only_unjustified,
                            "goal_report": goal_report.to_payload(),
                        },
                    )
                    return
            pipeline_state["goal_decomp_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- T-041-05: Symbol + reference gate ---
        if not pipeline_state.get("symbol_ref_done"):
            from app.services.symbol_reference_gate import check_symbol_references
            try:
                sym_report = check_symbol_references(
                    diff=diff,
                    source_tree=_pipeline_source_path,
                )
            except Exception as exc:
                sym_report = None
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="symbol_reference.check",
                    message=f"Symbol reference check errored: {exc}",
                    payload={"error": str(exc)},
                )
            if sym_report is not None and sym_report.findings:
                pipeline_state["symbol_ref"] = sym_report.to_payload()
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="symbol_reference.check",
                    message=f"Symbol reference warnings: {len(sym_report.findings)}",
                    payload=sym_report.to_payload(),
                )
            pipeline_state["symbol_ref_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Artifact existence gate ---
        # Verify planner-declared files actually exist in the sandbox after
        # the patch. Closes the gap where scope-lock or merge logic silently
        # drops a core deliverable (e.g. new rules file) and every other gate
        # passes on the remaining cosmetic changes.
        if not pipeline_state.get("artifact_existence_done"):
            from app.services.artifact_existence import check_artifact_existence
            import re as _re
            must_touch_plan = list(getattr(plan, "must_touch_files", []) or [])
            expected_new_plan = list(getattr(plan, "expected_new_files", []) or [])
            # Derive paths touched by the applied diff from its headers.
            diff_touched: set[str] = set()
            for _m in _re.finditer(r"^\+\+\+ b/(\S+)", diff, flags=_re.MULTILINE):
                diff_touched.add(_m.group(1).strip())
            art_report = None
            try:
                art_report = check_artifact_existence(
                    sandbox_dir=self._develop_sandbox_dir(task),
                    must_touch_files=must_touch_plan,
                    expected_new_files=expected_new_plan,
                    diff_touched_paths=diff_touched,
                )
            except Exception as exc:  # noqa: BLE001
                record_event(
                    self.db, task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="artifact_existence.check",
                    message=f"Artifact existence check errored (non-blocking): {exc}",
                    payload={"error": str(exc)[:500]},
                )
            if art_report is not None:
                pipeline_state["artifact_existence"] = art_report.to_payload()
                blockers = art_report.blocking_findings
                record_event(
                    self.db, task_id=task.id,
                    event_type=(
                        EventType.REVIEW_FAILED if blockers
                        else EventType.TOOL_SUCCEEDED
                    ),
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW, role=RoleName.REVIEWER,
                    tool_name="artifact_existence.check",
                    message=(
                        f"Artifact existence: {len(blockers)} blocking, "
                        f"{len(art_report.findings) - len(blockers)} warn "
                        f"(must_touch={len(must_touch_plan)}, "
                        f"expected_new={len(expected_new_plan)})"
                    ),
                    payload=art_report.to_payload(),
                )
                if blockers:
                    block_msgs = "; ".join(f.message for f in blockers[:3])
                    self._fail_develop_pipeline(
                        task=task,
                        message=(
                            "Artifact existence gate blocked the diff: "
                            + block_msgs
                            + (f" (+{len(blockers) - 3} more)" if len(blockers) > 3 else "")
                        ),
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        payload={"artifact_report": art_report.to_payload()},
                    )
                    return
            pipeline_state["artifact_existence_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- T-041-04: evidence chain validation before approval ---
        if not pipeline_state.get("evidence_chain_validated"):
            chain_gaps: list[str] = []
            attestation = pipeline_state.get("goal_attestation")
            if isinstance(attestation, dict) and attestation.get("all_goals_met") is False:
                unmet = [
                    a["anchor"] for a in attestation.get("anchors", [])
                    if a.get("status") == "not_achieved"
                ]
                if unmet:
                    chain_gaps.append(f"Unmet goals: {unmet!r}")
            conf = pipeline_state.get("conformance_report")
            if isinstance(conf, dict) and conf.get("verdict") == "block":
                chain_gaps.append("Conformance verdict is block")
            shape = pipeline_state.get("diff_shape")
            if isinstance(shape, dict) and shape.get("verdict") == "block":
                chain_gaps.append("Diff shape verdict is block")
            evidence = pipeline_state.get("evidence_bundle")
            if isinstance(evidence, dict) and evidence.get("verdict") == "insufficient":
                chain_gaps.append("Evidence bundle insufficient")
            ft_gate = pipeline_state.get("failing_test_gate")
            if isinstance(ft_gate, dict) and ft_gate.get("verdict") == "block":
                chain_gaps.append("Failing test gate blocked")

            pipeline_state["evidence_chain_validated"] = True
            pipeline_state["evidence_chain_gaps"] = chain_gaps
            if chain_gaps:
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=f"Evidence chain incomplete: {'; '.join(chain_gaps)}",
                    payload={"gaps": chain_gaps},
                )
                return
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- T-039: human approval gate before Jira transition ---
        # After conformance+attestation pass, pause here so a human can
        # review the diff/summary and either grant (→ Jira transitions)
        # or reject (→ task completes, Jira untouched). Gated by the
        # `develop_require_jira_approval` setting so tests/CI can disable it.
        require_approval = bool(
            getattr(self.tool_gateway.settings, "develop_require_jira_approval", True)
        )
        already_granted = bool(pipeline_state.get("jira_approval_granted"))
        writeback_done = isinstance(pipeline_state.get("jira_writeback"), dict) and bool(
            pipeline_state["jira_writeback"].get("transition")
        )
        if require_approval and not already_granted and not writeback_done:
            self._request_jira_transition_approval(
                task=task,
                plan=plan,
                pipeline_state=pipeline_state,
                codegen_result=codegen_result,
                review_result=review_result,
                attestation=pipeline_state.get("goal_attestation"),
            )
            return

        jira_writeback = pipeline_state.get("jira_writeback")
        if not isinstance(jira_writeback, dict):
            jira_writeback = {}
            issue_key = self._resolve_develop_issue_key(task)
            if issue_key:
                try:
                    # Auto comment on the Jira issue is intentionally disabled:
                    # it reads as mechanical and clutters the issue history.
                    # Status transition (to Done) is still useful and kept.
                    transition_result = self._execute_develop_tool(
                        task=task,
                        actor_name=actor_name,
                        tool_name="jira.transition_issue",
                        payload={
                            "issue_key": issue_key,
                            "transition_name": self._resolve_develop_done_transition(),
                        },
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        approval_id=approval_id,
                        pipeline_state=pipeline_state,
                    )
                    if transition_result is None:
                        return
                    jira_writeback["transition"] = transition_result
                except Exception as exc:
                    self._fail_develop_pipeline(
                        task=task,
                        message=f"Jira writeback failed: {exc}",
                        payload={"error": str(exc), "jira_writeback": jira_writeback, "plan_id": plan.plan_id},
                    )
                    return
            pipeline_state["jira_writeback"] = jira_writeback
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        task.pending_approval = False
        issue_key = self._resolve_develop_issue_key(task) or "unknown"
        pipeline_state["issue_key"] = issue_key
        develop_result = {
            "status": TaskStatus.COMPLETED.value,
            "message": self._build_develop_summary(pipeline_state),
            "result": {
                "scenario": "jira_issue_develop",
                "issue_key": issue_key,
                "summary": plan.change_summary,
                "files_changed": codegen_result.get("files_changed", []),
                "diff": codegen_result.get("diff", ""),
                "patch_method": pipeline_state.get("patch_method", ""),
                "test_skipped": pipeline_state.get("test_skipped", False),
                "review_verdict": review_result.get("verdict", ""),
                "jira_transitioned": bool(jira_writeback.get("transition")),
                "completeness_check": pipeline_state.get("completeness_check"),
                "goal_attestation": pipeline_state.get("goal_attestation"),
            },
            "codegen": codegen_result,
            "sandbox": sandbox_result,
            "test_result": test_result,
            "review_result": review_result,
            "jira_writeback": jira_writeback,
            "pipeline_state": pipeline_state,
        }
        task.latest_result_json = develop_result
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.COMPLETED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            source=EventSource.ORCHESTRATOR,
            message="Jira issue development pipeline completed.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_COMPLETED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            message="Jira issue development pipeline completed successfully.",
            payload={"plan_id": plan.plan_id, "approval_id": approval_id},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after Jira issue development pipeline.",
            payload={"jira_writeback": jira_writeback},
        )

    def _build_develop_summary(self, pipeline_state: dict[str, object]) -> str:
        """Build a human-readable summary of the develop pipeline execution."""
        zh = pipeline_state.get("user_lang") == "zh"
        parts: list[str] = []

        issue_key = str(pipeline_state.get("issue_key") or "unknown")
        if issue_key == "unknown":
            jira_writeback = pipeline_state.get("jira_writeback")
            if isinstance(jira_writeback, dict):
                for result in (jira_writeback.get("comment"), jira_writeback.get("transition")):
                    if isinstance(result, dict) and result.get("issue_key"):
                        issue_key = str(result["issue_key"])
                        break

        parts.append(f"## {issue_key} {'开发完成' if zh else 'Development Complete'}\n")

        # --- Change summary section ---
        raw_file_summaries = pipeline_state.get("file_summaries")
        file_summaries: list[str] = []
        if isinstance(raw_file_summaries, list):
            for f in raw_file_summaries:
                if isinstance(f, dict) and f.get("path") and f.get("summary"):
                    file_summaries.append(f"- **{f['path']}**: {f['summary']}")

        files = pipeline_state.get("files_changed")
        if isinstance(files, list) and files:
            parts.append(f"### {'改动总结' if zh else 'Change Summary'}\n")
            parts.append(
                f"{'本次修改了' if zh else 'Modified'} **{len(files)}** "
                f"{'个文件' if zh else 'file(s)'}{'：' if zh else ':'}"
            )
            if file_summaries:
                parts.extend(file_summaries)
            else:
                for file_path in files[:10]:
                    parts.append(f"- `{file_path}`")
            parts.append("")

        diff = str(pipeline_state.get("diff") or "")
        if diff:
            parts.append(f"### {'代码变更' if zh else 'Code Changes'}\n")
            parts.append(f"```diff\n{diff}\n```")
            parts.append("")

        parts.append(f"### {'流水线执行' if zh else 'Pipeline'}\n")
        parts.append(f"- {'代码生成：' if zh else 'Code generation: '}{pipeline_state.get('codegen_provider', 'unknown')}")
        method = str(pipeline_state.get("patch_method") or "")
        if method:
            parts.append(f"- {'补丁应用方式：' if zh else 'Patch applied via: '}{method}")
        if pipeline_state.get("test_skipped"):
            parts.append(f"- {'测试：已跳过（无测试配置）' if zh else 'Tests: skipped (no test config)'}")
        else:
            parts.append(f"- {'测试：通过' if zh else 'Tests: passed'}")
        parts.append(f"- {'审查：' if zh else 'Review: '}{pipeline_state.get('review_verdict', 'N/A')}")

        jira_writeback = pipeline_state.get("jira_writeback")
        if isinstance(jira_writeback, dict) and jira_writeback.get("transition"):
            parts.append(f"- {'Jira：已转换状态' if zh else 'Jira: transitioned'}")
        elif isinstance(jira_writeback, dict) and jira_writeback.get("comment"):
            parts.append(f"- {'Jira：已添加评论' if zh else 'Jira: commented'}")
        else:
            parts.append(f"- {'Jira：未找到 issue key，跳过回写' if zh else 'Jira: no issue key found, writeback skipped'}")

        completeness = pipeline_state.get("completeness_check")
        if isinstance(completeness, dict):
            if completeness.get("complete"):
                parts.append(f"\n### {'完整度检查' if zh else 'Completeness Check'}\n")
                parts.append(f"{'所有目标关键词已清除。' if zh else 'All target keywords removed.'}")
            else:
                remaining = completeness.get("remaining_files", 0)
                hits = completeness.get("remaining_hits", 0)
                parts.append(f"\n### {'完整度检查' if zh else 'Completeness Check'}\n")
                parts.append(
                    f"{'仍有' if zh else 'Still '}"
                    f"**{remaining}** {'个文件包含目标关键词' if zh else ' file(s) contain target keywords'}"
                    f"{'（共' if zh else ' ('}{hits} {'处）' if zh else ' hits)'}："
                )
                details = completeness.get("details", {})
                for path, count in details.items():
                    parts.append(f"- `{path}` ({count} {'处' if zh else 'hit(s)'})")

        return "\n".join(parts)

    def _execute_develop_tool(
        self,
        *,
        task: Task,
        actor_name: str,
        tool_name: str,
        payload: dict[str, object],
        stage: WorkflowStage,
        role: RoleName,
        approval_id: str | None,
        pipeline_state: dict[str, object],
    ) -> dict[str, object] | None:
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=stage,
            role=role,
            tool_name=tool_name,
            message=f"Requesting development pipeline tool '{tool_name}'.",
            payload={
                "approval_id": approval_id,
                "payload_preview": self._preview_develop_payload(payload),
            },
        )
        try:
            result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name=tool_name,
                payload=payload,
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=stage,
                role=role,
                approval_id=approval_id,
            )
            self._sync_retry_count(task)
        except ToolApprovalRequired as exc:
            self._sync_retry_count(task)
            self._pause_for_tool_approval(
                task=task,
                tool_name=exc.tool_name,
                execution_id=exc.execution_id,
                approval_id=exc.approval_id,
                stage=stage,
                role=role,
            )
            self._preserve_develop_pipeline_state(
                task=task,
                pipeline_state={**pipeline_state, "paused_tool_name": exc.tool_name},
            )
            return None
        except Exception as exc:
            self._sync_retry_count(task)
            failed_event_type = (
                EventType.TOOL_TIMED_OUT
                if isinstance(exc, ToolInvocationError) and exc.timed_out
                else EventType.TOOL_FAILED
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=failed_event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=stage,
                role=role,
                tool_name=tool_name,
                message="Development pipeline tool failed.",
                payload={"error": str(exc), "approval_id": approval_id},
            )
            raise

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_SUCCEEDED,
            source=EventSource.TOOL_GATEWAY,
            stage=stage,
            role=role,
            tool_name=tool_name,
            message=f"Development pipeline tool '{tool_name}' completed.",
            payload=result,
        )
        commit_checkpoint(self.db, label=f"develop_tool_{tool_name}")
        return result

    def _gather_codegen_context(self, *, task: Task, plan: GeneratedPlan) -> dict[str, str]:
        """Read affected files from the source tree, sandbox, or configured knowledge index.

        Uses two strategies (grep-first for higher precision):
        1. Grep discovery — search the source tree for keywords from the task.
        2. Plan locations — files identified by the planner/knowledge retrieval
           (only if not already found by grep).

        Always returns full file contents (required for JSON-mode codegen
        where the model must produce complete modified files for difflib).
        """
        context_files: dict[str, str] = {}
        source_path = self._resolve_knowledge_source_path()
        sandbox_dir = self._develop_sandbox_dir(task)

        # --- Strategy 1 (priority): grep keywords in source tree ---
        grep_keywords = self._extract_grep_keywords(task)
        grep_hits: dict[str, list[int]] = {}
        if source_path and grep_keywords:
            grep_hits = self._grep_source_tree(source_path, grep_keywords)
            for relative_path in grep_hits:
                if relative_path in context_files:
                    continue
                full_content = self._read_context_file(
                    source_path=source_path,
                    sandbox_dir=sandbox_dir,
                    relative_path=relative_path,
                )
                if full_content is not None:
                    context_files[relative_path] = full_content

        # --- Strategy 2: plan locations (fill remaining slots) ---
        for location in plan.affected_code_locations:
            relative_path = self._normalize_codegen_path(location.relative_path)
            if not relative_path or relative_path in context_files:
                continue
            content = self._read_context_file(
                source_path=source_path,
                sandbox_dir=sandbox_dir,
                relative_path=relative_path,
            )
            if content is not None:
                context_files[relative_path] = content

        # --- Strategy 3: knowledge citations from the planning phase ---
        # Fix for tasks where the request describes the change conceptually
        # (e.g. "remove hardcoded username Minij across the codebase") and
        # neither grep keywords nor the planner's affected_code_locations
        # resolve to any file — but knowledge.search *did* return relevant
        # citations during the planning prefetch. Those citations are still
        # grounding, not edit targets, so we only fall back here when both
        # earlier strategies produced no context. Without this fallback the
        # pipeline hard-fails with "no context for affected files" even
        # though the grounding data already exists in the task's events.
        if not context_files:
            for citation_path in self._citation_paths_from_planning_events(task):
                relative_path = self._normalize_codegen_path(citation_path)
                if not relative_path or relative_path in context_files:
                    continue
                content = self._read_context_file(
                    source_path=source_path,
                    sandbox_dir=sandbox_dir,
                    relative_path=relative_path,
                )
                if content is not None:
                    context_files[relative_path] = content

        # --- Strategy 4: must_touch_files (guarantee full file context) ---
        # The planner declares which files MUST be modified. Earlier strategies
        # may miss these if grep keywords don't match or affected_code_locations
        # is empty.  Reading them here ensures codegen always receives the
        # complete file contents it needs — the same quality of context that a
        # human-written spec would provide.
        must_touch = getattr(plan, "must_touch_files", None) or []
        for mt_path in must_touch:
            relative_path = self._normalize_codegen_path(mt_path)
            if not relative_path or relative_path in context_files:
                continue
            content = self._read_context_file(
                source_path=source_path,
                sandbox_dir=sandbox_dir,
                relative_path=relative_path,
            )
            if content is not None:
                context_files[relative_path] = content

        return context_files

    def _citation_paths_from_planning_events(self, task: Task) -> list[str]:
        """Pull relative_path values from the most recent KNOWLEDGE_RETRIEVED
        event for this task. Used as Strategy 3 fallback in
        _gather_codegen_context. Returns an empty list if no knowledge
        retrieval ran or citations are missing.
        """
        stmt = (
            select(Event.payload_json)
            .where(Event.task_id == task.id)
            .where(Event.event_type == EventType.KNOWLEDGE_RETRIEVED)
            .order_by(Event.created_at.desc())
            .limit(4)
        )
        paths: list[str] = []
        seen: set[str] = set()
        try:
            payloads = list(self.db.scalars(stmt))
        except Exception:
            return paths
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            citations = payload.get("citations")
            if not isinstance(citations, list):
                continue
            for entry in citations:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("relative_path") or entry.get("file_path") or entry.get("path")
                if not isinstance(raw, str):
                    continue
                trimmed = raw.strip()
                if trimmed and trimmed not in seen:
                    seen.add(trimmed)
                    paths.append(trimmed)
        return paths

    @staticmethod
    def _extract_snippets(
        full_content: str,
        matched_lines: list[int],
        *,
        radius: int = 30,
    ) -> str:
        """Extract snippets around matched line numbers.

        Returns the relevant portions of the file with line-number markers
        so the LLM knows exactly where each snippet starts.  If the snippets
        cover > 80% of the file, return the full file instead.
        """
        lines = full_content.splitlines()
        total = len(lines)
        if not matched_lines or total == 0:
            return full_content

        # Build merged ranges
        ranges: list[tuple[int, int]] = []
        for ln in sorted(set(matched_lines)):
            start = max(0, ln - 1 - radius)
            end = min(total, ln - 1 + radius + 1)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))

        # If snippets cover most of the file, return the whole thing
        covered = sum(e - s for s, e in ranges)
        if covered >= total * 0.8:
            return full_content

        parts: list[str] = []
        for start, end in ranges:
            parts.append(f"[lines {start + 1}-{end}]")
            parts.extend(lines[start:end])
            parts.append("")  # blank separator

        return "\n".join(parts)

    def _read_context_file(
        self,
        *,
        source_path: Path | None,
        sandbox_dir: Path,
        relative_path: str,
    ) -> str | None:
        """Try reading a file from source path, sandbox, or knowledge index."""
        content = self._read_knowledge_source_context_file(
            source_path=source_path,
            relative_path=relative_path,
        )
        if content is not None:
            return content
        content = self._read_sandbox_context_file(
            sandbox_dir=sandbox_dir,
            relative_path=relative_path,
        )
        if content is not None:
            return content
        return self._read_knowledge_context_file(relative_path) or None

    @staticmethod
    def _detect_rename_pair(task: Task) -> tuple[str, str] | None:
        """Detect if the task is a simple identifier rename.

        Returns (old_name, new_name) if a rename pair is found, else None.
        """
        noise = {
            "the", "a", "an", "all", "function", "method", "class", "variable",
            "constant", "field", "property", "parameter", "argument", "identifier",
            "name", "symbol", "from", "this", "that", "every", "each", "with",
        }
        request = task.request_text or ""

        def _is_code_ident(s: str) -> bool:
            return len(s) >= 4 and " " not in s and (any(c.isupper() for c in s) or "_" in s)

        # Strategy 1: find "rename ... X ... to ... Y" where X and Y are code identifiers
        rename_match = re.search(r"[Rr]ename\b(.+?)(?:\.|$)", request)
        if rename_match:
            fragment = rename_match.group(1)
            to_split = re.split(r"\s+to\s+", fragment, maxsplit=1)
            if len(to_split) == 2:
                before_words = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b", to_split[0])
                after_words = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b", to_split[1])
                old_candidates = [w for w in before_words if _is_code_ident(w) and w.lower() not in noise]
                new_candidates = [w for w in after_words if _is_code_ident(w) and w.lower() not in noise]
                if old_candidates and new_candidates:
                    old, new = old_candidates[-1], new_candidates[0]
                    if old != new:
                        return (old, new)

        # Strategy 2: check translation intent + grounding_terms
        translation = task.translation_json or {}
        objective = (translation.get("objective") or "").lower()
        if "rename" not in objective and "refactor" not in objective:
            return None
        terms = translation.get("grounding_terms", [])
        idents = [
            t for t in terms
            if isinstance(t, str) and _is_code_ident(t) and t.lower() not in noise
        ]
        if len(idents) >= 2:
            return (idents[0], idents[1]) if idents[0] != idents[1] else None
        return None

    @staticmethod
    def _deterministic_rename(
        *,
        context_files: dict[str, str],
        old_name: str,
        new_name: str,
    ) -> dict[str, object] | None:
        """Replace old_name with new_name in all context files, producing a unified diff."""
        import difflib

        diff_parts: list[str] = []
        files_changed: list[str] = []
        file_summaries: list[dict[str, str]] = []

        for rel_path, original_content in context_files.items():
            if old_name not in original_content:
                continue
            new_content = original_content.replace(old_name, new_name)
            if new_content == original_content:
                continue

            # Generate unified diff
            orig_lines = original_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                orig_lines, new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
            diff_text = "".join(diff)
            if diff_text:
                diff_parts.append(diff_text)
                files_changed.append(rel_path)
                count = original_content.count(old_name)
                file_summaries.append({
                    "file": rel_path,
                    "summary": f"Renamed {count} occurrence(s) of {old_name} → {new_name}",
                })

        if not diff_parts:
            return None

        return {
            "diff": "\n".join(diff_parts),
            "files_changed": files_changed,
            "file_summaries": file_summaries,
            "provider_name": "deterministic_rename",
        }

    @staticmethod
    def _extract_grep_keywords(task: Task) -> list[str]:
        """Extract concrete grep-able keywords from task context.

        Sources (in priority order):
        1. Quoted strings from the request text.
        2. search_queries from semantic translation (multi-query).
        3. grounding_terms from semantic translation.
        4. CamelCase / PascalCase identifiers from the request text.
        """
        keywords: list[str] = []
        seen_lower: set[str] = set()

        def _add(term: str) -> None:
            t = term.strip()
            if t and len(t) >= 2 and t.lower() not in seen_lower:
                seen_lower.add(t.lower())
                keywords.append(t)

        request_text = task.request_text or ""

        # 1. Quoted strings (e.g. "Minij", "master admin")
        for match in re.finditer(r"""['"]([^'"]{2,40})['"]""", request_text):
            _add(match.group(1))

        translation = task.translation_json or {}

        # 2. search_queries — the translator already generated multi-angle queries
        for sq in translation.get("search_queries", []):
            if isinstance(sq, str):
                _add(sq)

        # 3. grounding_terms
        for term in translation.get("grounding_terms", []):
            if isinstance(term, str):
                _add(term)

        # 4. camelCase identifiers (e.g. getLoggedInEmail, getCurrentUserEmail)
        for match in re.finditer(r"\b([a-z][a-zA-Z]{4,})\b", request_text):
            candidate = match.group(1)
            # Must contain at least one uppercase letter to be camelCase
            if any(c.isupper() for c in candidate):
                _add(candidate)

        # 5. PascalCase identifiers (e.g. SessionManager, HandymanApp)
        for match in re.finditer(r"\b([A-Z][a-z]{2,}(?:[A-Z][a-z]*)*)\b", request_text):
            _add(match.group(1))

        return keywords[:16]

    def _grep_source_tree(
        self, source_path: Path, keywords: list[str],
    ) -> dict[str, list[int]]:
        """Search the source tree for keywords.

        Returns a dict mapping relative file paths to lists of matching
        line numbers (1-based).  Results are sorted by hit count descending
        so the most relevant files come first.  At most 15 unique files.
        """
        code_extensions = {".kt", ".java", ".xml", ".json", ".py", ".ts", ".tsx", ".js", ".jsx"}
        EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
        max_files = 25
        # rel_path -> set of matched line numbers
        hits: dict[str, set[int]] = {}

        candidate_files: list[Path] = []
        try:
            for file_path in source_path.rglob("*"):
                if EXCLUDED_DIRS.intersection(file_path.parts):
                    continue
                if file_path.suffix.lower() in code_extensions and file_path.is_file():
                    candidate_files.append(file_path)
                if len(candidate_files) >= 2000:
                    break
        except OSError:
            return {}

        for keyword in keywords:
            keyword_lower = keyword.lower()
            for file_path in candidate_files:
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                matched_lines: list[int] = []
                for line_no, line in enumerate(lines, 1):
                    if keyword_lower in line.lower():
                        matched_lines.append(line_no)
                if matched_lines:
                    rel = file_path.relative_to(source_path).as_posix()
                    normalized = self._normalize_codegen_path(rel)
                    if normalized:
                        hits.setdefault(normalized, set()).update(matched_lines)

        # Sort by hit count descending, take top N
        sorted_paths = sorted(hits.keys(), key=lambda p: len(hits[p]), reverse=True)
        return {p: sorted(hits[p]) for p in sorted_paths[:max_files]}

    def _ensure_develop_sandbox(self, *, task: Task, plan: GeneratedPlan) -> dict[str, object]:
        sandbox = self._build_develop_sandbox(task)
        if sandbox.exists():
            return {"status": "ready", "sandbox_dir": str(sandbox.work_dir)}

        repo_url = self._resolve_develop_repo_url(task=task, plan=plan)
        if not repo_url:
            raise ToolInvocationError(
                f"No sandbox exists for task {task.id}, and no repository URL or source path is configured."
            )

        try:
            result = sandbox.clone(
                repo_url,
                timeout_seconds=float(
                    getattr(self.tool_gateway.settings, "sandbox_clone_timeout_seconds", 120.0)
                ),
            )
        except SandboxError as exc:
            raise ToolInvocationError(str(exc), retryable=False) from exc
        return {"status": "cloned", **result}

    def _build_develop_sandbox(self, task: Task) -> ExecutionSandbox:
        return ExecutionSandbox(
            task_id=task.id,
            base_dir=str(getattr(self.tool_gateway.settings, "sandbox_base_dir", "data/sandboxes")),
        )

    def _develop_sandbox_dir(self, task: Task) -> Path:
        base_dir = Path(str(getattr(self.tool_gateway.settings, "sandbox_base_dir", "data/sandboxes")))
        return base_dir / task.id

    # ----- Compile repair loop --------------------------------------------- #

    def _attempt_compile_repair(
        self,
        *,
        task: Task,
        actor_name: str,
        compile_errors: list[dict],
        sandbox_dir: Path,
        pipeline_state: dict,
        approval_id: str | None,
    ) -> tuple[bool, list[str]]:
        """Attempt a narrow syntax-only repair after compile gate failure.

        Processes each broken file individually (one codegen call per file)
        to stay within the 300s timeout. Returns (any_applied, files_touched)
        where files_touched is the list of file paths modified by repair diffs.
        """
        if not compile_errors:
            return False, []

        source_path = self._resolve_knowledge_source_path()
        any_applied = False
        all_repair_touched: list[str] = []

        for err in compile_errors[:5]:  # Cap at 5 files
            rel_path = err.get("file", "")
            error_msg = err.get("error", "syntax error")
            if not rel_path:
                continue

            full = sandbox_dir / rel_path.replace("/", "\\") if "\\" in str(sandbox_dir) else sandbox_dir / rel_path
            if not full.exists():
                continue

            try:
                broken_content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Load original (pre-patch) version as reference
            orig_section = ""
            if source_path:
                orig = self._read_knowledge_source_context_file(
                    source_path=source_path,
                    relative_path=rel_path,
                )
                if orig:
                    orig_section = (
                        f"\nORIGINAL FILE (before the broken patch was applied):\n"
                        f"=== ORIGINAL {rel_path} ===\n{orig[:4000]}\n=== END ORIGINAL ===\n\n"
                    )

            repair_prompt = (
                f"STRUCTURAL REPAIR TASK — fix ONE broken file: {rel_path}\n\n"
                f"This file has a syntax error caused by a malformed patch. "
                "Common problems include:\n"
                "- Duplicated code blocks (same function/import appears twice)\n"
                "- Code from inside a function appearing AFTER the module's "
                "default export or closing brace\n"
                "- Missing or extra brackets/parentheses from misaligned diff hunks\n"
                "- Incomplete statements where lines were deleted incorrectly\n\n"
                "RULES:\n"
                "- Compare the BROKEN file with the ORIGINAL to find structural damage\n"
                "- Remove any duplicated code blocks\n"
                "- Fix bracket/parenthesis matching\n"
                "- Restore proper function and component structure\n"
                "- Keep the INTENDED changes (like role simplification, removing "
                "hardcoded values) but fix the broken structure\n"
                "- Do NOT add new features or change business logic beyond what "
                "the original patch intended\n\n"
                f"ERROR:\n  {rel_path}: {error_msg[:300]}\n\n"
                + orig_section
                + f"Output ONLY valid unified diff hunks that fix {rel_path}.\n"
                f"Start with 'diff --git a/{rel_path} b/{rel_path}'.\n"
                "If no fix is needed, output nothing.\n"
            )

            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="codegen.repair",
                message=f"Attempting per-file syntax repair for {rel_path}",
            )

            # Cooldown before repair CLI call — avoids rate limiting from
            # the preceding codegen + retry batches.
            time.sleep(15)

            repair_payload = {
                "plan_json": {"objective": f"Fix syntax errors in {rel_path}", "steps": []},
                "context_files": {rel_path: broken_content},
                "task_description": repair_prompt,
            }
            repair_result = None
            for _repair_attempt in range(2):  # 1 retry on failure
                try:
                    repair_result = self._execute_develop_tool(
                        task=task,
                        actor_name=actor_name,
                        tool_name="codegen.generate_patch",
                        payload=repair_payload,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        approval_id=approval_id,
                        pipeline_state=pipeline_state,
                    )
                    break  # Success
                except Exception as exc:
                    if _repair_attempt == 0:
                        time.sleep(20)  # Cool down before retry
                        continue
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="codegen.repair",
                        message=f"Syntax repair codegen failed for {rel_path}: {exc}",
                    )
            if repair_result is None:
                continue

            if not repair_result:
                continue

            repair_diff = str(repair_result.get("diff") or "").strip()
            if not repair_diff:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="codegen.repair",
                    message=f"Syntax repair for {rel_path} produced no diff.",
                )
                continue

            # Filter repair diff — only keep hunks targeting the broken file.
            # The LLM sometimes emits stray hunks for unrelated files.
            filtered_sections: list[str] = []
            for section in re.split(r"(?=^diff --git )", repair_diff, flags=re.MULTILINE):
                section = section.strip()
                if not section:
                    continue
                m_hdr = re.match(r"diff --git a/(.+?) b/", section)
                if m_hdr and m_hdr.group(1).strip() == rel_path:
                    filtered_sections.append(section)
                elif not m_hdr:
                    # Leading preamble (before first diff header) — keep
                    filtered_sections.append(section)
            repair_diff = "\n".join(filtered_sections).strip()
            if not repair_diff or "diff --git" not in repair_diff:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="codegen.repair",
                    message=f"Repair diff for {rel_path} contained only off-target hunks — skipped.",
                )
                continue

            # Apply repair diff to sandbox
            try:
                sandbox = self._build_develop_sandbox(task)
                sandbox.apply_patch(
                    repair_diff,
                    commit=False,
                    commit_message=f"syntax repair: {rel_path}",
                    timeout_seconds=15,
                )
                any_applied = True
                # Extract file paths touched by this repair diff
                for m in re.finditer(r"diff --git a/(.+?) b/", repair_diff):
                    touched_path = m.group(1).strip()
                    if touched_path and touched_path not in all_repair_touched:
                        all_repair_touched.append(touched_path)
                if rel_path not in all_repair_touched:
                    all_repair_touched.append(rel_path)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="codegen.repair",
                    message=f"Syntax repair applied to {rel_path}.",
                )
            except Exception as exc:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="codegen.repair",
                    message=f"Repair diff apply failed for {rel_path}: {exc}",
                )
                continue

        if any_applied:
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_SUCCEEDED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="codegen.repair",
                message=f"Per-file repair complete. {len(all_repair_touched)} file(s) repaired. Re-running compile gate.",
            )
        return any_applied, all_repair_touched

    # ----- T-038-A: retry plumbing ----------------------------------------- #

    MAX_CONFORMANCE_ATTEMPTS: int = 2
    DESTRUCTIVE_VERB_HINTS: tuple[str, ...] = (
        "remove", "delete", "clean", "rename", "refactor", "fix",
        "replace", "simplify", "strip", "eliminate", "drop", "disable",
    )

    def _build_codegen_task_description(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        pipeline_state: dict,
        batch_files: dict[str, str] | None = None,
    ) -> str:
        """Augment the user's request with strict directives the codegen
        tool must obey. Includes (a) shadow-implementation guard, (b) the
        planner's must_touch_files commitment, and (c) feedback from a
        previous failed conformance attempt when retrying.

        When *batch_files* is provided, the must-touch directive is scoped
        to only the files present in this batch's context. This prevents
        the model from hallucinating diffs for files it has no content for.
        """
        original = (task.request_text or "").strip()
        directives: list[str] = []

        request_lower = original.lower()
        if any(verb in request_lower for verb in self.DESTRUCTIVE_VERB_HINTS):
            directives.append(
                "DIRECTIVE: This task asks to modify or remove existing "
                "behavior. Prefer modifying existing files over creating "
                "new ones. Do not create a parallel implementation that "
                "leaves the dirty existing code untouched."
            )

        must_touch = list(getattr(plan, "must_touch_files", []) or [])
        if must_touch:
            # Scope to current batch: only list files the model actually has
            if batch_files is not None:
                must_touch = [f for f in must_touch if f in batch_files]
            if must_touch:
                directives.append(
                    "DIRECTIVE: The plan commits to modifying these files. "
                    "Your patch MUST modify each one (not merely create new "
                    "files alongside them): " + ", ".join(must_touch)
                    + "\n\nIMPORTANT: Only modify files whose content is "
                    "provided below. Do NOT generate diffs for files you "
                    "cannot see."
                )

        # Per-file parallel codegen: when batch has exactly 1 target file, add
        # a hard scope-lock directive. Otherwise CLI agents in worktree mode
        # (codex, claude_code) see the whole repo and decide to "helpfully"
        # also modify related files (e.g. database.rules.json, package.json),
        # causing overlapping diffs across parallel batches that corrupt each
        # other when merged.
        if batch_files is not None and len(batch_files) == 1:
            only_file = next(iter(batch_files.keys()))
            directives.append(
                f"CRITICAL SCOPE LOCK: This codegen call is ONE of several "
                f"parallel calls, each assigned exactly ONE target file. "
                f"YOUR ONLY TARGET FILE IS: {only_file}\n\n"
                f"Other parallel calls are handling the other EXISTING files "
                f"in the plan. You MUST NOT modify ANY other EXISTING file "
                f"than {only_file}. However, you MAY CREATE new files that "
                f"the task explicitly requires (e.g. new config files, rule "
                f"files, or documentation files) if those new files do not "
                f"currently exist in the repository. Do not modify or delete "
                f"any other existing file — even if you think a related "
                f"change would be helpful. Cross-file modifications from "
                f"multiple parallel calls will conflict and corrupt the "
                f"patch.\n\n"
                f"If {only_file} depends on changes to other files, note that "
                f"in your summary but do NOT implement those other changes — "
                f"they are someone else's responsibility in this pipeline.\n\n"
                f"Your output diff must contain hunks for EXACTLY ONE file: "
                f"{only_file}.\n\n"
                f"MINIMAL EDIT REQUIREMENT: Make the SMALLEST possible change "
                f"to {only_file} that achieves the task. Do NOT rewrite the "
                f"whole file. Do NOT restructure, reformat, or reorganise "
                f"existing code. Do NOT remove unrelated imports, variables, "
                f"functions, or comments. Only add/change/remove the lines "
                f"that are DIRECTLY required by this specific task. Preserve "
                f"all other existing code character-for-character. Aim for a "
                f"diff of under 30 lines changed unless the task explicitly "
                f"calls for more."
            )

        feedback = pipeline_state.get("conformance_feedback")
        if isinstance(feedback, list) and feedback:
            joined = "; ".join(str(item) for item in feedback if item)
            if joined:
                directives.append(
                    "RETRY FEEDBACK: A previous patch was rejected by the "
                    "spec-conformance gate for these reasons — " + joined +
                    ". Address each reason in this attempt."
                )

        if not directives:
            return original
        return original + "\n\n" + "\n\n".join(directives)

    @staticmethod
    def _strip_duplicate_diff_hunks(diff_text: str, seen_files: set[str]) -> str:
        """Remove diff sections for files already produced by an earlier batch.

        Splits on ``diff --git`` or ``--- a/`` boundaries and drops any
        section whose target file path is in *seen_files*.
        """
        import re as _re
        # Split on "diff --git a/X b/X" or bare "--- a/X" headers
        sections = _re.split(r"(?m)^(?=diff --git |--- a/)", diff_text)
        kept: list[str] = []
        for section in sections:
            if not section.strip():
                continue
            # Extract file path from "diff --git a/X b/X" or "--- a/X"
            m = _re.match(r"diff --git a/(.+?) b/", section)
            if not m:
                m = _re.match(r"--- a/(.+)", section)
            if m:
                fpath = m.group(1).strip()
                if fpath in seen_files:
                    continue  # Skip duplicate
            kept.append(section)
        return "\n".join(kept)

    def _reset_for_conformance_retry(
        self,
        *,
        task: Task,
        pipeline_state: dict,
        feedback: list[str],
    ) -> None:
        """Clear pipeline_state of all stages downstream of context_files
        so the next pipeline pass re-runs codegen→apply→review→conformance.
        Also wipes the on-disk sandbox so apply_patch starts from a clean
        clone instead of stacking diffs.
        """
        for key in (
            "codegen_result",
            "diff",
            "files_changed",
            "codegen_provider",
            "file_summaries",
            "sandbox_result",
            "patch_method",
            "completeness_check",
            "test_result",
            "review_result",
            "review_verdict",
            "conformance_report",
            "diff_shape_done",
            "diff_shape",
            "compile_gate_done",
            "compile_gate",
            "failing_test_gate_done",
            "failing_test_gate",
            "goal_decomp_done",
            "goal_decomposition",
            "symbol_ref_done",
            "symbol_ref",
            "evidence_chain_validated",
            "evidence_chain_gaps",
            "goal_attestation",
            "retry_done",
        ):
            pipeline_state.pop(key, None)
        pipeline_state["conformance_feedback"] = list(feedback)
        pipeline_state["conformance_attempts"] = (
            int(pipeline_state.get("conformance_attempts", 0) or 0) + 1
        )

        sandbox_dir = self._develop_sandbox_dir(task)
        if sandbox_dir.exists():
            try:
                shutil.rmtree(sandbox_dir, ignore_errors=False)
            except OSError:
                # best-effort; if the dir can't be removed (file lock on
                # Windows, etc.), apply_patch will surface the failure.
                pass

        self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

    @staticmethod
    def _normalize_codegen_path(relative_path: str) -> str | None:
        normalized = str(relative_path or "").strip().replace("\\", "/")
        if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return None
        path = Path(normalized)
        if any(part in {"", ".", ".."} for part in path.parts):
            return None
        return normalized

    # Filenames must be at least 3 chars, contain a dot, and end with a known
    # code/config extension. Allows optional directory prefix (slash-separated).
    _FILENAME_PATTERN = re.compile(
        r"\b([\w\-./]*[\w\-]+\.(?:json|rules|js|jsx|ts|tsx|py|kt|java|go|rs|rb|"
        r"yaml|yml|toml|xml|html|css|scss|md|sh|sql|proto|env|conf))\b"
    )

    @classmethod
    def _extract_filenames_from_request(cls, request_text: str) -> list[str]:
        """Pull explicit filenames out of the request text.

        Used as a fallback signal when the planner mislabels
        affected_code_locations (picks grounding files instead of actual
        targets). The request text from Jira issues often names the files
        explicitly (e.g., "create database.rules.json, firestore.rules").
        """
        if not request_text:
            return []
        matches = cls._FILENAME_PATTERN.findall(request_text)
        seen: set[str] = set()
        out: list[str] = []
        for m in matches:
            norm = cls._normalize_codegen_path(m)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _anchor_precheck_fails(self, task: Task) -> bool:
        """Defense line 2: reject tasks whose anchors are absent from the knowledge source.

        Returns True (and fails the task) when ALL anchors from the
        translation are missing from the source tree. Checks grounding_terms,
        search_queries, AND quoted identifiers from the normalized request.
        Partial hits proceed normally — the anchor might be a new concept
        being added.
        """
        translation = task.translation_json or {}
        anchors = list(translation.get("grounding_terms") or [])

        # Also pull search_queries from translation (often more specific)
        search_queries = translation.get("search_queries") or []
        for sq in search_queries:
            if sq and sq not in anchors:
                anchors.append(sq)

        # Also extract quoted identifiers from the normalized request
        normalized = translation.get("normalized_request") or ""
        if normalized:
            from app.services.spec_conformance import _extract_quoted_anchors
            for qa in _extract_quoted_anchors(normalized):
                if qa and qa not in anchors:
                    anchors.append(qa)

        if not anchors:
            return False

        source_path = self._resolve_knowledge_source_path()
        if source_path is None:
            return False

        from app.services.spec_conformance import _anchor_matches_fuzzy

        # Fuzzy match (CamelCase-aware, prefix-stem, path+content): avoids
        # rejecting natural-language QA queries like "job management page"
        # that reference files named JobManagement.js. Strict substring
        # match stays inside spec_conformance's other callers where
        # exact-match semantics matter.
        missing = [a for a in anchors if not _anchor_matches_fuzzy(source_path, a)]
        if missing and len(missing) == len(anchors):
            msg = (
                "## Task rejected: anchors not found\n\n"
                f"The request references {missing!r} but none of these "
                f"appear in the configured knowledge source "
                f"({source_path.name}). This likely means the task is "
                f"targeting a different repository. Please verify the "
                f"knowledge source configuration."
            )
            self._fail_develop_pipeline(
                task=task,
                message=msg,
                event_type=EventType.EXECUTION_FAILED,
                stage=WorkflowStage.KNOWLEDGE,
                role=RoleName.KNOWLEDGE,
                payload={
                    "scenario": "anchor_not_found",
                    "missing_anchors": missing,
                    "source_name": source_path.name,
                },
            )
            return True
        return False

    def _resolve_knowledge_source_path(self) -> Path | None:
        path_str = str(getattr(self.tool_gateway.settings, "knowledge_source_path", "") or "").strip()
        if not path_str:
            return None
        path = Path(path_str)
        if path.is_dir():
            return path
        return None

    def _read_knowledge_source_context_file(self, *, source_path: Path | None, relative_path: str) -> str | None:
        if source_path is None:
            return None
        full_path = source_path / relative_path
        try:
            resolved_source = source_path.resolve()
            resolved_path = full_path.resolve()
            resolved_path.relative_to(resolved_source)
        except (OSError, ValueError):
            return None
        if not resolved_path.is_file():
            return None
        try:
            content = resolved_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        max_bytes = int(getattr(self.tool_gateway.settings, "knowledge_max_file_bytes", 120_000) or 120_000)
        if len(content) <= max_bytes:
            return content
        return content[:max_bytes] + "\n... (truncated)"

    @staticmethod
    def _read_sandbox_context_file(*, sandbox_dir: Path, relative_path: str) -> str | None:
        full_path = sandbox_dir / relative_path
        try:
            resolved_sandbox = sandbox_dir.resolve()
            resolved_path = full_path.resolve()
            resolved_path.relative_to(resolved_sandbox)
        except (OSError, ValueError):
            return None
        if not resolved_path.is_file():
            return None
        return resolved_path.read_text(encoding="utf-8", errors="replace")

    def _read_knowledge_context_file(self, relative_path: str) -> str | None:
        knowledge_service = getattr(self, "knowledge_service", None) or getattr(
            self.tool_gateway,
            "knowledge_service",
            None,
        )
        if knowledge_service is None:
            return None

        try:
            if hasattr(knowledge_service, "search"):
                result = knowledge_service.search(query=relative_path, top_k=1)
            else:
                result = knowledge_service.search_repositories(query=relative_path, top_k=1)
        except Exception:
            return None

        return self._extract_knowledge_content(relative_path=relative_path, result=result)

    @staticmethod
    def _extract_knowledge_content(*, relative_path: str, result: object) -> str | None:
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")

        if isinstance(result, list):
            for item in result:
                content = PrimaryOrchestrator._extract_knowledge_content(relative_path=relative_path, result=item)
                if content:
                    return content
            return None

        if not isinstance(result, dict):
            return None

        for key in ("content", "text", "snippet"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value

        citations = result.get("citations")
        if isinstance(citations, list):
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                citation_path = str(citation.get("relative_path") or "").strip().replace("\\", "/")
                snippet = citation.get("snippet")
                if citation_path == relative_path and isinstance(snippet, str) and snippet.strip():
                    return snippet

        packaged_context = result.get("packaged_context")
        if (
            isinstance(packaged_context, str)
            and packaged_context.strip()
            and not packaged_context.startswith("No repository citations matched")
            and (not isinstance(citations, list) or bool(citations))
        ):
            return packaged_context
        return None

    def _resolve_develop_repo_url(self, *, task: Task, plan: GeneratedPlan) -> str | None:
        candidate_values: list[object] = []
        if isinstance(task.translation_json, dict):
            candidate_values.extend(
                [
                    task.translation_json.get("repo_url"),
                    task.translation_json.get("repository_url"),
                    task.translation_json.get("source_path"),
                ]
            )
        if isinstance(plan.provider, dict):
            candidate_values.extend(
                [
                    plan.provider.get("repo_url"),
                    plan.provider.get("repository_url"),
                    plan.provider.get("source_path"),
                ]
            )
        candidate_values.extend(
            [
                getattr(self.tool_gateway.settings, "sandbox_repo_url", None),
                getattr(self.tool_gateway.settings, "repository_url", None),
                getattr(self.tool_gateway.settings, "knowledge_source_path", None),
            ]
        )

        for value in candidate_values:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return None

    @staticmethod
    def _load_develop_pipeline_state(task: Task) -> dict[str, object]:
        latest_result = getattr(task, "latest_result_json", None)
        if not isinstance(latest_result, dict):
            return {}
        state = latest_result.get("pipeline_state")
        return dict(state) if isinstance(state, dict) else {}

    @staticmethod
    def _preview_develop_payload(payload: dict[str, object]) -> dict[str, object]:
        preview = dict(payload)
        context_files = preview.get("context_files")
        if isinstance(context_files, dict):
            preview["context_files"] = {
                str(path): f"{len(str(content))} chars"
                for path, content in context_files.items()
            }
        for key in ("patch", "diff"):
            value = preview.get(key)
            if isinstance(value, str):
                preview[key] = f"{len(value)} chars"
        return preview

    def _preserve_develop_pipeline_state(self, *, task: Task, pipeline_state: dict[str, object]) -> None:
        # Strip large data (context_files, diff) before persisting to avoid bloating the DB
        persistable: dict[str, object] = {}
        for k, v in pipeline_state.items():
            if k == "context_files":
                continue
            if isinstance(v, ConformanceReport):
                # in-memory object is not JSON serializable; persist its payload
                persistable[k] = v.to_payload()
            else:
                persistable[k] = v
        latest_result = dict(task.latest_result_json) if isinstance(task.latest_result_json, dict) else {}
        latest_result["pipeline_state"] = persistable
        task.latest_result_json = latest_result
        try:
            self.db.flush()
        except Exception:
            pass  # best-effort persistence; don't break the pipeline

    @staticmethod
    def _is_missing_test_pipeline_config_error(error_message: str) -> bool:
        normalized = error_message.casefold()
        return "config not found" in normalized or ("not found" in normalized and "config" in normalized)

    def _request_jira_transition_approval(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        pipeline_state: dict[str, object],
        codegen_result: dict[str, object],
        review_result: dict[str, object],
        attestation: object,
    ) -> None:
        """Create a pending Approval for the final Jira transition step and
        park the task in AWAITING_APPROVAL. The diff, change summary,
        files_changed, conformance/review verdicts, and goal attestation
        are all put into both ``task.latest_result_json`` (so the task
        detail page can render them) and ``approval.request_payload_json``
        (so the approval queue page can).
        """
        issue_key = self._resolve_develop_issue_key(task) or "unknown"
        diff = str(pipeline_state.get("diff") or codegen_result.get("diff") or "")
        files_changed = codegen_result.get("files_changed") or pipeline_state.get("files_changed") or []
        summary_md = self._build_develop_summary(pipeline_state)

        # Reservations reviewer: LLM flags risks/trade-offs/gotchas a human
        # approver should see before approving. Fails safe to empty list.
        reservations: list[str] = []
        try:
            from app.services.reservations import build_reservations
            _reservations_report = build_reservations(
                task_request=task.request_text or "",
                change_summary=plan.change_summary or "",
                diff=diff,
                plan_objective=getattr(plan, "objective", "") or "",
                settings=self.tool_gateway.settings,
            )
            reservations = _reservations_report.reservations
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_SUCCEEDED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="reservations.review",
                message=(
                    f"Reservations reviewer flagged {len(reservations)} item(s)."
                    if reservations
                    else "Reservations reviewer found no flags."
                ),
                payload={
                    "reservations": reservations,
                    "provider": _reservations_report.provider,
                    "model": _reservations_report.model,
                },
            )
            commit_checkpoint(self.db, label="reservations_reviewer_done")
        except Exception as exc:  # noqa: BLE001
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_FAILED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="reservations.review",
                message=f"Reservations reviewer failed (non-blocking): {exc}",
                payload={"error": str(exc)[:500]},
            )

        preview_result = {
            "scenario": "jira_issue_develop",
            "issue_key": issue_key,
            "summary": plan.change_summary,
            "files_changed": list(files_changed),
            "diff": diff,
            "patch_method": pipeline_state.get("patch_method", ""),
            "test_skipped": pipeline_state.get("test_skipped", False),
            "review_verdict": review_result.get("verdict", ""),
            "jira_transitioned": False,
            "conformance_report": pipeline_state.get("conformance_report"),
            "goal_attestation": attestation,
            "reservations": reservations,
        }

        approval = Approval(
            task_id=task.id,
            action_name="jira.transition_issue",
            status=ApprovalStatus.PENDING,
            requested_by_role=RoleName.REVIEWER,
            approver_role=ActorRole.TEAM_LEAD.value,
            requested_by_actor_name=task.actor_name,
            risk_level=task.risk_level,
            risk_category=task.risk_category,
            reason=(
                "Code changes passed spec conformance and goal attestation. "
                "Manual approval required before transitioning the Jira issue."
            ),
            request_payload_json={
                "stage": "post_codegen_pre_jira_transition",
                "scenario": "jira_issue_develop",
                "issue_key": issue_key,
                "summary_markdown": summary_md,
                "files_changed": list(files_changed),
                "diff": diff,
                "review_verdict": review_result.get("verdict"),
                "conformance_report": pipeline_state.get("conformance_report"),
                "goal_attestation": attestation,
            },
            policy_snapshot_json={
                "decision": "require_approval",
                "source": "develop_post_conformance_gate",
                "tool_name": "jira.transition_issue",
                "actor_name": task.actor_name,
                "actor_role": task.actor_role.value,
                "risk_level": task.risk_level.value,
                "risk_category": task.risk_category.value,
                "required_approver_role": ActorRole.TEAM_LEAD.value,
            },
        )
        self.db.add(approval)
        self.db.flush()

        # Mark pipeline_state so resume_after_approval knows to skip straight
        # to jira_writeback without re-running earlier stages.
        pipeline_state["pending_jira_approval_id"] = approval.id
        self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        task.pending_approval = True
        task.latest_result_json = {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "message": summary_md,
            "approval_id": approval.id,
            "result": preview_result,
            "pipeline_state": pipeline_state,
        }

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.AWAITING_APPROVAL,
            new_stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Awaiting human approval before Jira transition.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_REQUESTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Approval requested for Jira transition.",
            payload={
                "approval_id": approval.id,
                "action_name": approval.action_name,
                "approver_role": approval.approver_role,
                "issue_key": issue_key,
                "files_changed": list(files_changed),
                "reservations": reservations,
            },
        )
        attempt_index = pipeline_state.get("workspace_attempt_index")
        stage_completed = (
            f"attempt_{attempt_index:03d}"
            if isinstance(attempt_index, int) and attempt_index >= 1
            else "review"
        )
        self._workspace_append_audit(
            task,
            "approval.requested",
            {"approval_id": approval.id, "action_name": approval.action_name},
        )
        self._workspace_write_checkpoint(
            task,
            stage_completed=stage_completed,
            next_stage="approval",
            resume_args={"approval_id": approval.id},
        )
        commit_checkpoint(self.db, label="awaiting_approval_jira_transition")

    def _fail_develop_pipeline(
        self,
        *,
        task: Task,
        message: str,
        event_type: EventType = EventType.EXECUTION_FAILED,
        stage: WorkflowStage = WorkflowStage.ACTION,
        role: RoleName = RoleName.ACTION,
        payload: dict[str, object] | None = None,
    ) -> None:
        task.pending_approval = False
        task.latest_result_json = {
            "status": TaskStatus.FAILED.value,
            "message": message,
            **(payload or {}),
        }
        record_event(
            self.db,
            task_id=task.id,
            event_type=event_type,
            source=EventSource.ORCHESTRATOR,
            stage=stage,
            role=role,
            message=message,
            payload=payload,
        )
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=role,
            source=EventSource.ORCHESTRATOR,
            message=message,
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after Jira issue development pipeline failure.",
            payload={"message": message},
        )
        self._workspace_append_audit(
            task,
            "pipeline.failed",
            {"message": message, "stage": stage.value if hasattr(stage, "value") else str(stage)},
        )

    @staticmethod
    def _count_changed_files(codegen_result: dict[str, object]) -> int:
        files_changed = codegen_result.get("files_changed")
        if isinstance(files_changed, list):
            return len(files_changed)
        return 0

    @staticmethod
    def _safe_int(value: object, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_review_violations(review_result: dict[str, object]) -> str:
        violations = review_result.get("violations")
        if not isinstance(violations, list) or not violations:
            return "reviewer returned a block verdict"

        messages: list[str] = []
        for violation in violations[:5]:
            if isinstance(violation, dict):
                message = str(violation.get("message") or violation.get("rule_name") or "").strip()
            else:
                message = str(violation).strip()
            if message:
                messages.append(message)
        return "; ".join(messages) if messages else "reviewer returned a block verdict"

    @staticmethod
    def _resolve_develop_issue_key(task: Task) -> str | None:
        if isinstance(task.translation_json, dict):
            issue_key = str(task.translation_json.get("issue_key") or "").strip().upper()
            if issue_key:
                return issue_key
        reference = extract_jira_issue_reference(task.request_text)
        return reference.issue_key if reference else None

    @staticmethod
    def _build_develop_jira_comment(
        *,
        codegen_result: dict[str, object],
        test_result: dict[str, object],
        review_result: dict[str, object],
    ) -> str:
        files_changed = codegen_result.get("files_changed")
        files_text = ", ".join(str(path) for path in files_changed[:5]) if isinstance(files_changed, list) else ""
        if not files_text:
            files_text = "none reported"

        passed_count = PrimaryOrchestrator._safe_int(test_result.get("passed_count"), default=0)
        total_steps = PrimaryOrchestrator._safe_int(test_result.get("total_steps"), default=0)
        review_verdict = str(review_result.get("verdict") or "pass")
        summary = str(codegen_result.get("summary") or "Generated and applied code changes.").strip()
        return "\n".join(
            [
                "Automated development pipeline completed.",
                f"Summary: {summary}",
                f"Files changed: {files_text}",
                f"Tests: {passed_count}/{total_steps} passed.",
                f"Review: {review_verdict}.",
            ]
        )

    def _resolve_develop_done_transition(self) -> str:
        transition_name = str(getattr(self.tool_gateway.settings, "jira_develop_done_transition", "") or "").strip()
        return transition_name or "Done"

    def _pause_for_tool_approval(
        self,
        *,
        task: Task,
        tool_name: str,
        execution_id: str,
        approval_id: str,
        stage: WorkflowStage,
        role: RoleName,
    ) -> None:
        task.pending_approval = True
        task.latest_result_json = {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "message": f"Tool '{tool_name}' requires approval before execution.",
            "approval_id": approval_id,
            "execution_id": execution_id,
        }
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.AWAITING_APPROVAL,
            new_stage=stage,
            role=role,
            source=EventSource.ORCHESTRATOR,
            message=f"Task paused: tool '{tool_name}' awaiting approval.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=stage,
            role=role,
            tool_name=tool_name,
            message=f"Approval requested for tool '{tool_name}'.",
            payload={"approval_id": approval_id, "execution_id": execution_id},
        )
        commit_checkpoint(self.db, label="awaiting_approval_tool_gate")

    def _execute_writeback_plan(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        approval_id: str | None = None,
    ) -> None:
        """Chain Jira comment and transition writes under a single approval."""
        semantic_translation = (
            GeneratedSemanticTranslation.model_validate(task.translation_json or {})
            if task.translation_json
            else self.semantic_translator.translate(
                task_id=task.id,
                request_text=task.request_text,
                scenario=task.scenario,
                actor_name=actor_name,
            ).translation
        )
        if not task.translation_json:
            task.translation_json = semantic_translation.model_dump(mode="json")

        base_payload = self.action_agent.build_payload(
            task_id=task.id,
            request_text=task.request_text,
            scenario=task.scenario,
            semantic_translation=semantic_translation,
        )

        issue_key = str(base_payload.get("issue_key") or "").strip().upper()
        comment_text = str(base_payload.get("text") or "").strip()
        transition_name = str(base_payload.get("transition_name") or "").strip()

        if not issue_key or (not comment_text and not transition_name):
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": "Jira writeback requires an issue key and at least one comment or transition.",
                "payload": base_payload,
            }
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.ACTION,
                source=EventSource.ORCHESTRATOR,
                message="Task failed before Jira writeback execution because the action payload was incomplete.",
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.FINAL_RESPONSE_EMITTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                message="Final response emitted after Jira writeback payload validation failure.",
                payload={"payload": base_payload},
            )
            return

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.EXECUTING,
            new_stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            source=EventSource.ORCHESTRATOR,
            message="Task entered writeback execution after approval.",
            payload={"approval_id": approval_id},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            message="Jira writeback execution started.",
            payload={"plan_id": plan.plan_id, "approval_id": approval_id},
        )

        combined_result: dict[str, object] = {"issue_key": issue_key}

        if comment_text:
            tool_name = "jira.add_comment"
            comment_payload = {
                "issue_key": issue_key,
                "text": comment_text,
            }
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Requesting Jira comment post.",
                payload={"approval_id": approval_id, "payload_preview": comment_payload},
            )
            try:
                comment_result = self.tool_gateway.execute(
                    task_id=task.id,
                    tool_name=tool_name,
                    payload=comment_payload,
                    actor_context={"actor_name": actor_name, "task_id": task.id},
                    session_id=task.session_id,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    approval_id=approval_id,
                )
                self._sync_retry_count(task)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.TOOL_GATEWAY,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name=tool_name,
                    message="Jira comment posted.",
                    payload=comment_result,
                )
                combined_result["comment"] = comment_result
            except ToolApprovalRequired as exc:
                self._sync_retry_count(task)
                self._pause_for_tool_approval(
                    task=task,
                    tool_name=exc.tool_name,
                    execution_id=exc.execution_id,
                    approval_id=exc.approval_id,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                )
                return
            except Exception as exc:
                self._sync_retry_count(task)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.TOOL_GATEWAY,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name=tool_name,
                    message="Jira comment post failed.",
                    payload={"error": str(exc), "approval_id": approval_id},
                )
                combined_result["comment_error"] = str(exc)
                if not transition_name:
                    task.latest_result_json = {
                        "status": TaskStatus.FAILED.value,
                        "message": f"Jira comment post failed: {exc}",
                        **combined_result,
                    }
                    set_task_status(
                        self.db,
                        task=task,
                        new_status=TaskStatus.FAILED,
                        new_stage=WorkflowStage.DONE,
                        role=RoleName.ACTION,
                        source=EventSource.ORCHESTRATOR,
                        message="Task failed during Jira comment post.",
                    )
                    return

        if transition_name:
            tool_name = "jira.transition_issue"
            transition_payload = {
                "issue_key": issue_key,
                "transition_name": transition_name,
            }
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Requesting Jira status transition.",
                payload={"approval_id": approval_id, "payload_preview": transition_payload},
            )
            try:
                transition_result = self.tool_gateway.execute(
                    task_id=task.id,
                    tool_name=tool_name,
                    payload=transition_payload,
                    actor_context={"actor_name": actor_name, "task_id": task.id},
                    session_id=task.session_id,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    approval_id=approval_id,
                )
                self._sync_retry_count(task)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.TOOL_GATEWAY,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name=tool_name,
                    message="Jira issue transitioned.",
                    payload=transition_result,
                )
                combined_result["transition"] = transition_result
            except ToolApprovalRequired as exc:
                self._sync_retry_count(task)
                self._pause_for_tool_approval(
                    task=task,
                    tool_name=exc.tool_name,
                    execution_id=exc.execution_id,
                    approval_id=exc.approval_id,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                )
                return
            except Exception as exc:
                self._sync_retry_count(task)
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.TOOL_GATEWAY,
                    stage=WorkflowStage.ACTION,
                    role=RoleName.ACTION,
                    tool_name=tool_name,
                    message="Jira transition failed.",
                    payload={"error": str(exc), "approval_id": approval_id},
                )
                combined_result["transition_error"] = str(exc)
                task.latest_result_json = {
                    "status": TaskStatus.FAILED.value,
                    "message": f"Jira transition failed: {exc}",
                    **combined_result,
                }
                set_task_status(
                    self.db,
                    task=task,
                    new_status=TaskStatus.FAILED,
                    new_stage=WorkflowStage.DONE,
                    role=RoleName.ACTION,
                    source=EventSource.ORCHESTRATOR,
                    message="Task failed during Jira transition.",
                )
                return

        status_parts: list[str] = []
        if "comment" in combined_result:
            status_parts.append(f"commented on {issue_key}")
        if "transition" in combined_result:
            transition = combined_result["transition"]
            from_status = transition.get("from_status", "?") if isinstance(transition, dict) else "?"
            to_status = transition.get("to_status", "?") if isinstance(transition, dict) else "?"
            status_parts.append(f"transitioned {issue_key} from {from_status} to {to_status}")

        task.latest_result_json = {
            "status": TaskStatus.COMPLETED.value,
            "message": f"Jira writeback completed: {' and '.join(status_parts)}.",
            **combined_result,
        }
        task.pending_approval = False
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.COMPLETED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.ACTION,
            source=EventSource.ORCHESTRATOR,
            message="Jira writeback task completed.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.EXECUTION_COMPLETED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            message="Jira writeback execution completed.",
            payload={"plan_id": plan.plan_id, "approval_id": approval_id},
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after Jira writeback.",
            payload=combined_result,
        )

    @staticmethod
    def _build_failed_output_message(
        *,
        plan: GeneratedPlan,
        result: dict[str, object],
        review_summary: str,
    ) -> str:
        if plan.final_output_contract.type == "knowledge_answer":
            answer = result.get("answer")
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            return (
                "I could not produce a grounded repository answer from the current indexed knowledge. "
                "Add a file path, class name, error log, or sync the knowledge source and try again."
            )
        return review_summary

    @staticmethod
    def _resolve_tool_name(plan: GeneratedPlan) -> str:
        for step in plan.steps:
            if step.tool_name:
                return step.tool_name
        return plan.tools[0].tool_name

    def _sync_retry_count(self, task: Task) -> None:
        stmt = (
            select(ToolExecution)
            .where(ToolExecution.task_id == task.id)
            .order_by(ToolExecution.started_at.desc())
            .limit(1)
        )
        latest_execution = self.db.scalars(stmt).first()
        if latest_execution is not None:
            task.retry_count = max(task.retry_count, max(latest_execution.attempt_count - 1, 0))
