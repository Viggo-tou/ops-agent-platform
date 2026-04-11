from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schemas import GeneratedPlan, GeneratedSemanticTranslation
from app.agents.service import ActionAgent, PrimaryAgentPlanner, ReviewerAgent
from app.agents.translation import SemanticTranslator
from app.core.enums import ActorRole, ApprovalStatus, EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.core.jira import extract_jira_issue_reference, looks_like_jira_issue_url
from app.models.approval import Approval
from app.models.task import Task
from app.models.tool_execution import ToolExecution
from app.services.events import record_event, set_task_status
from app.tools.gateway import ToolGateway, ToolInvocationError


def _contains_word(text: str, *keywords: str) -> bool:
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords)


def _truncate_text(value: object, *, limit: int) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 1)]}..."


def classify_request(request_text: str) -> str:
    lowered = request_text.lower()
    jira_reference = extract_jira_issue_reference(request_text)
    if jira_reference and (
        looks_like_jira_issue_url(request_text)
        or _contains_word(lowered, "plan", "breakdown", "implement", "implementation", "rollout", "scope")
    ):
        return "jira_issue_plan"
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


class PrimaryOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.primary_agent = PrimaryAgentPlanner()
        self.semantic_translator = SemanticTranslator()
        self.action_agent = ActionAgent()
        self.reviewer_agent = ReviewerAgent()
        self.tool_gateway = ToolGateway(db)

    def bootstrap_task(self, task: Task, *, actor_name: str) -> None:
        planning_request_text = task.request_text
        semantic_translation = self._translate_request(task=task, actor_name=actor_name, issue_context=None)
        task.translation_json = semantic_translation.model_dump(mode="json")

        issue_context: dict[str, object] | None = None
        planning_knowledge_context: dict[str, object] | None = None

        if task.scenario == "jira_issue_plan":
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
        elif task.translation_json:
            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=None,
                planning_knowledge_context=None,
            )

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
        self._execute_plan(task=task, actor_name=actor_name, plan=plan_document, approval_id=approval_id)

    def _translate_request(
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
        return translation_document

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
