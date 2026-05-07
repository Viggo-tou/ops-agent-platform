from __future__ import annotations

import json
import logging
import re
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schemas import GeneratedPlan, GeneratedSemanticTranslation
from app.agents.service import ActionAgent, PrimaryAgentPlanner, ReviewerAgent
from app.agents.translation import SemanticTranslator
from app.core.config import get_settings
from app.core.enums import ActorRole, ApprovalStatus, EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.core.jira import extract_jira_issue_reference, looks_like_jira_issue_url
from app.core.telemetry import get_current_trace_id, get_tracer
from app.core.timeouts import external_http_timeout
from app.models.approval import Approval
from app.models.event import Event
from app.models.task import Task
from app.models.tool_execution import ToolExecution
from app.schemas.evidence import EvidenceItem
from app.schemas.knowledge import KnowledgeClaim
from app.services.events import commit_checkpoint, record_event as _record_event, set_task_status as _set_task_status
from app.services.evidence_chain import EvidenceChainReport, check_evidence_chain
from app.services.failure_diagnosis import FailureKind, run_diagnosis
from app.services.checkpointing import CheckpointStage, TaskCheckpoint, read_task_checkpoint, write_task_checkpoint
from app.services.memory import MemoryService
from app.services.sandbox import ExecutionSandbox, SandboxError
from app.services.spec_conformance import (
    ConformanceReport,
    build_goal_attestation,
    check_spec_conformance,
)
from app.services.task_workspace import TaskWorkspace
from app.tools.gateway import ToolApprovalRequired, ToolGateway, ToolInvocationError

logger = logging.getLogger("orchestrator")


def record_event(
    db: Session,
    *,
    task_id: str,
    event_type: EventType,
    source: EventSource,
    message: str,
    session_id: str | None = None,
    stage: WorkflowStage | None = None,
    role: RoleName | None = None,
    tool_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    event = _record_event(
        db,
        task_id=task_id,
        event_type=event_type,
        source=source,
        message=message,
        session_id=session_id,
        stage=stage,
        role=role,
        tool_name=tool_name,
        payload=payload,
    )
    if event_type in {
        EventType.REVIEW_FAILED,
        EventType.COMPILE_FAILED,
        EventType.FAILURE_DIAGNOSIS_GENERATED,
        EventType.TOOL_FAILED,
        EventType.TOOL_TIMED_OUT,
    }:
        try:
            task = db.get(Task, task_id)
            MemoryService(db, get_settings()).maybe_record_gate_event(event=event, task=task)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory gate-failure hook failed",
                extra={"task_id": task_id, "event_type": str(event_type), "error": str(exc)[:300]},
            )
    return event


def set_task_status(
    db: Session,
    *,
    task: Task,
    new_status: TaskStatus,
    new_stage: WorkflowStage,
    role: RoleName | None,
    message: str,
    source: EventSource = EventSource.SYSTEM,
    payload: dict[str, Any] | None = None,
) -> None:
    _set_task_status(
        db,
        task=task,
        new_status=new_status,
        new_stage=new_stage,
        role=role,
        message=message,
        source=source,
        payload=payload,
    )
    if new_status in {TaskStatus.COMPLETED, TaskStatus.AWAITING_APPROVAL}:
        try:
            MemoryService(db, get_settings()).promote_pending(task_id=task.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory promotion hook failed",
                extra={"task_id": task.id, "status": str(new_status), "error": str(exc)[:300]},
            )


class RepairRoundTimeout(Exception):
    """Raised when a single compile-repair round exceeds its deadline.

    The caller in the multi-round loop catches this so the round is
    counted as a failed round instead of poisoning the whole budget.
    """


def _contains_word(text: str, *keywords: str) -> bool:
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords)


def _truncate_text(value: object, *, limit: int) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 1)]}..."


def _json_size_bytes(value: object) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=True).encode("utf-8"))


def _truncated_traceback(exc: BaseException, *, limit: int = 2000) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(text) <= limit:
        return text
    return text[:limit]


def _normalize_diff_path(path: object) -> str:
    return str(path or "").strip().replace("\\", "/")


def _diff_sections_for_path(diff: str, rel_path: str) -> list[str]:
    target = _normalize_diff_path(rel_path)
    if not target or not diff.strip():
        return []

    sections = [
        section.strip()
        for section in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
        if section.strip()
    ]
    if not sections:
        return []

    matched: list[str] = []
    saw_git_header = False
    for section in sections:
        header = re.match(r"diff --git a/(.+?) b/(.+?)(?:\r?\n|$)", section)
        if header is None:
            continue
        saw_git_header = True
        a_path = _normalize_diff_path(header.group(1))
        b_path = _normalize_diff_path(header.group(2))
        if target in {a_path, b_path}:
            matched.append(section)

    if matched:
        return matched
    if not saw_git_header and "diff --git" not in diff:
        return [diff.strip()]
    return []


def _slice_diff_for_path(diff: str, rel_path: str) -> str:
    return "\n".join(_diff_sections_for_path(diff, rel_path)).strip()


def _diff_sections_by_file(diff: str) -> dict[str, str]:
    by_file: dict[str, list[str]] = {}
    for section in re.split(r"(?=^diff --git )", diff or "", flags=re.MULTILINE):
        section = section.strip()
        if not section:
            continue
        header = re.match(r"diff --git a/(.+?) b/(.+?)(?:\r?\n|$)", section)
        if header is None:
            continue
        path = _normalize_diff_path(header.group(2) or header.group(1))
        if path:
            by_file.setdefault(path, []).append(section)
    return {path: "\n".join(sections).strip() for path, sections in by_file.items()}


def _capture_first_attempt_diff(pipeline_state: dict[str, object], diff: object) -> None:
    diff_text = str(diff or "").strip()
    if not diff_text or pipeline_state.get("first_attempt_diff"):
        return
    pipeline_state["first_attempt_diff"] = diff_text
    by_file = _diff_sections_by_file(diff_text)
    if by_file:
        pipeline_state["first_attempt_diff_by_file"] = by_file


def _normalize_intent_line(line: str) -> str:
    return line.strip()


def _is_counted_intent_line(line: str) -> bool:
    stripped = _normalize_intent_line(line)
    if not stripped:
        return False
    return not (stripped.startswith("//") or stripped.startswith("#"))


def _changed_lines_from_diff(diff: str, rel_path: str, marker: str) -> list[str]:
    if marker not in {"+", "-"}:
        return []
    lines: list[str] = []
    for section in _diff_sections_for_path(diff, rel_path):
        for raw_line in section.splitlines():
            if marker == "+" and raw_line.startswith("+++") or marker == "-" and raw_line.startswith("---"):
                continue
            if raw_line.startswith(marker):
                normalized = _normalize_intent_line(raw_line[1:])
                if _is_counted_intent_line(normalized):
                    lines.append(normalized)
    return lines


def _intent_lines_from_first_attempt(first_attempt_diff: str, rel_path: str) -> list[str]:
    return _changed_lines_from_diff(first_attempt_diff, rel_path, "+")


def _count_intent_preservation(
    first_attempt_diff: str,
    rel_path: str,
    repair_diff: str,
    baseline_content: str,
) -> float:
    """Return the fraction of first-attempt added lines preserved by repair.

    The repair diff is applied to the already-broken file. If an intent line
    is not removed by repair, it is presumed preserved. If it is removed, it
    still counts when repair re-adds the same normalized line or when the line
    already existed in the original baseline content.
    """
    intent_lines = _intent_lines_from_first_attempt(first_attempt_diff, rel_path)
    if not intent_lines:
        return 1.0

    repair_added = set(_changed_lines_from_diff(repair_diff, rel_path, "+"))
    repair_removed = set(_changed_lines_from_diff(repair_diff, rel_path, "-"))
    baseline_lines = {
        normalized
        for normalized in (_normalize_intent_line(line) for line in baseline_content.splitlines())
        if _is_counted_intent_line(normalized)
    }

    preserved = 0
    for line in intent_lines:
        if line in repair_added or line in baseline_lines or line not in repair_removed:
            preserved += 1
    return preserved / len(intent_lines)


def _intent_lines_dropped_by_repair(
    *,
    first_attempt_diff: str,
    rel_path: str,
    repair_diff: str,
    baseline_content: str,
    limit: int = 30,
) -> list[str]:
    """Return the specific intent lines repair lost relative to first attempt.

    Mirrors the bookkeeping in ``_count_intent_preservation`` but returns
    the line list (capped) so the orchestrator can name them in a
    second-chance repair prompt (Leg 4 — turn silent intent_dropped into
    actionable feedback for the LLM).
    """
    intent_lines = _intent_lines_from_first_attempt(first_attempt_diff, rel_path)
    if not intent_lines:
        return []
    repair_added = set(_changed_lines_from_diff(repair_diff, rel_path, "+"))
    repair_removed = set(_changed_lines_from_diff(repair_diff, rel_path, "-"))
    baseline_lines = {
        normalized
        for normalized in (_normalize_intent_line(line) for line in baseline_content.splitlines())
        if _is_counted_intent_line(normalized)
    }
    dropped: list[str] = []
    for line in intent_lines:
        if line in repair_added or line in baseline_lines or line not in repair_removed:
            continue
        dropped.append(line)
        if len(dropped) >= limit:
            break
    return dropped


def _compile_repair_intent_dropped(
    *,
    first_attempt_diff: str,
    rel_path: str,
    repair_diff: str,
    baseline_content: str,
    threshold: float,
) -> tuple[bool, float, int]:
    intent_count = len(_intent_lines_from_first_attempt(first_attempt_diff, rel_path))
    if threshold <= 0 or intent_count == 0:
        return False, 1.0, intent_count
    ratio = _count_intent_preservation(
        first_attempt_diff=first_attempt_diff,
        rel_path=rel_path,
        repair_diff=repair_diff,
        baseline_content=baseline_content,
    )
    return ratio < threshold, ratio, intent_count


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
            "Marked as",
            "Advance",
            "Move to",
            "in progress",
            "done",
            "complete",
            "close",
            "reopen",
            "comment",
            "Comment",
            "Remark",
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
        self.primary_agent = PrimaryAgentPlanner(db=db)
        self.semantic_translator = SemanticTranslator(db=db)
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

    def _workspace_write_intent(
        self,
        task: Task,
        *,
        issue_context: dict[str, object] | None = None,
        write_checkpoint: bool = True,
    ) -> None:
        language = detect_user_language(task.request_text or "")
        jira_issue_key = ""
        jira_issue_body = ""
        if issue_context:
            jira_issue_key = str(issue_context.get("issue_key") or "").strip()
            jira_issue_body = self._jira_issue_body_from_context(issue_context)

        def _write(workspace: TaskWorkspace) -> None:
            workspace.write_intent(
                intent_text=task.request_text or "",
                request_text=task.request_text or "",
                jira_issue_body=jira_issue_body,
                jira_issue_key=jira_issue_key,
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
                    "jira_issue_key": jira_issue_key,
                    "jira_issue_body_present": bool(jira_issue_body),
                },
            )
            if write_checkpoint:
                workspace.write_checkpoint(
                    stage_completed="intake",
                    next_stage="semantic_translation",
                    resume_args={"task_id": task.id},
                )

        self._workspace_call(task, _write)
        self._write_task_checkpoint(
            task,
            stage="intake",
            output_payload=self._task_checkpoint_payload(
                task,
                issue_context=issue_context,
                language=language,
            ),
        )
        if issue_context:
            self._workspace_add_spec_anchor_evidence(task, issue_context=issue_context)

    @staticmethod
    def _jira_issue_body_from_context(issue_context: dict[str, object]) -> str:
        parts: list[str] = []
        for label, key in (("Summary", "summary"), ("Description", "description")):
            value = str(issue_context.get(key) or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        return "\n\n".join(parts).strip()

    def _workspace_add_spec_anchor_evidence(
        self,
        task: Task,
        *,
        issue_context: dict[str, object],
    ) -> None:
        jira_issue_body = self._jira_issue_body_from_context(issue_context)
        if not jira_issue_body:
            return
        anchor_text = "\n".join(
            part
            for part in (
                task.request_text or "",
                str(issue_context.get("summary") or ""),
                str(issue_context.get("description") or ""),
            )
            if part.strip()
        )
        file_paths = self._extract_filenames_from_request(anchor_text)
        if not file_paths:
            return
        issue_key = str(issue_context.get("issue_key") or "").strip()
        items = [
            EvidenceItem(
                id=f"spec_anchor:{task.id}:{index}",
                source="spec_anchor",
                file_path=file_path,
                snippet=jira_issue_body[:4000],
                chunk_kind="synthetic",
                retrieval_channel="jira_issue_body",
                metadata={"issue_key": issue_key},
            )
            for index, file_path in enumerate(file_paths, start=1)
        ]
        self._workspace_call(task, lambda workspace: workspace.add_evidence(items))

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

    def _task_checkpoint_payload(self, task: Task, **extra: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "translation_json": getattr(task, "translation_json", None),
            "plan_json": getattr(task, "plan_json", None),
            "review_json": getattr(task, "review_json", None),
        }
        latest_result_json = getattr(task, "latest_result_json", None)
        if isinstance(latest_result_json, dict):
            payload["latest_result_json"] = latest_result_json
            pipeline_state = latest_result_json.get("pipeline_state")
            if isinstance(pipeline_state, dict):
                payload["pipeline_state"] = pipeline_state
        payload.update(extra)
        return payload

    def _write_task_checkpoint(
        self,
        task: Task,
        *,
        stage: CheckpointStage,
        output_payload: dict[str, object] | None = None,
        sandbox_snapshot_id: str | None = None,
        can_resume: bool = True,
        resume_method: str = "replay_from_output",
    ) -> TaskCheckpoint | None:
        if not bool(getattr(self.tool_gateway.settings, "resumability_enabled", True)):
            return None
        return write_task_checkpoint(
            self.db,
            task=task,
            stage=stage,
            output_payload=output_payload or self._task_checkpoint_payload(task),
            sandbox_snapshot_id=sandbox_snapshot_id,
            can_resume=can_resume,
            resume_method=resume_method,  # type: ignore[arg-type]
        )

    def _restore_task_checkpoint_payload(self, task: Task, checkpoint: TaskCheckpoint) -> None:
        payload = checkpoint.output_payload
        for attr in ("translation_json", "plan_json", "review_json"):
            value = payload.get(attr)
            if isinstance(value, dict):
                setattr(task, attr, value)

        latest_result = payload.get("latest_result_json")
        if isinstance(latest_result, dict):
            task.latest_result_json = dict(latest_result)

        pipeline_state = payload.get("pipeline_state")
        if isinstance(pipeline_state, dict):
            latest = dict(task.latest_result_json) if isinstance(task.latest_result_json, dict) else {}
            latest["pipeline_state"] = dict(pipeline_state)
            task.latest_result_json = latest

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
        self._write_task_checkpoint(
            task,
            stage="translate",
            output_payload=self._task_checkpoint_payload(
                task,
                semantic_translation=task.translation_json,
            ),
        )

        issue_context: dict[str, object] | None = None
        planning_knowledge_context: dict[str, object] | None = None

        if task.scenario in {"jira_issue_plan", "jira_issue_develop"}:
            issue_context = self._prefetch_jira_issue_context(
                task=task,
                actor_name=actor_name,
                issue_key=semantic_translation.issue_key,
            )
            if issue_context is None:
                # Refuse to fabricate requirements for a missing Jira issue.
                # _prefetch_jira_issue_context has already marked the task FAILED.
                # See P69-7 incident: the previous "graceful fallback" reset
                # the task to CREATED and synthesised an empty issue body, which
                # caused codegen to invent a generic Login.js change for a ghost
                # ticket. The downstream gates can't catch this because they
                # only validate diff shape, not whether the requirement existed.
                return
            self._workspace_write_intent(
                task,
                issue_context=issue_context,
                write_checkpoint=False,
            )

            semantic_translation = self._translate_request(
                task=task,
                actor_name=actor_name,
                issue_context=issue_context,
            )
            self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
            task.translation_json = semantic_translation.model_dump(mode="json")
            self._write_task_checkpoint(
                task,
                stage="translate",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=issue_context,
                ),
            )
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
            self._write_task_checkpoint(
                task,
                stage="retrieve",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=issue_context,
                    planning_knowledge_context=planning_knowledge_context,
                    planning_request_text=planning_request_text,
                ),
            )
        elif task.scenario == "jira_issue_writeback":
            issue_context = self._prefetch_jira_issue_context(
                task=task,
                actor_name=actor_name,
                issue_key=semantic_translation.issue_key,
            )
            if issue_context is None:
                return
            self._workspace_write_intent(
                task,
                issue_context=issue_context,
                write_checkpoint=False,
            )

            semantic_translation = self._translate_request(
                task=task,
                actor_name=actor_name,
                issue_context=issue_context,
            )
            self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
            task.translation_json = semantic_translation.model_dump(mode="json")
            self._write_task_checkpoint(
                task,
                stage="translate",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=issue_context,
                ),
            )

            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=None,
            )
            self._write_task_checkpoint(
                task,
                stage="retrieve",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=issue_context,
                    planning_knowledge_context=None,
                    planning_request_text=planning_request_text,
                ),
            )
        elif task.translation_json:
            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                translation_document=task.translation_json,
                issue_context=None,
                planning_knowledge_context=None,
            )
            self._write_task_checkpoint(
                task,
                stage="retrieve",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=None,
                    planning_knowledge_context=None,
                    planning_request_text=planning_request_text,
                ),
            )

        # --- Defense line 2: anchor pre-check ---
        # If translation extracted grounding_terms/anchors, verify at least one
        # exists in the knowledge source tree. If ALL are missing, the task is
        # likely targeting the wrong repository — fail fast before planning.
        if self._anchor_precheck_fails(task):
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
        self._write_task_checkpoint(
            task,
            stage="plan",
            output_payload=self._task_checkpoint_payload(
                task,
                semantic_translation=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=planning_knowledge_context,
                planning_request_text=planning_request_text,
                plan_json=task.plan_json,
            ),
        )
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
        self._write_task_checkpoint(
            task,
            stage="review_pre",
            output_payload=self._task_checkpoint_payload(
                task,
                semantic_translation=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=planning_knowledge_context,
                planning_request_text=planning_request_text,
                plan_json=task.plan_json,
                review_json=task.review_json,
            ),
        )

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
            self._write_task_checkpoint(
                task,
                stage="awaiting_approval",
                output_payload=self._task_checkpoint_payload(
                    task,
                    approval_id=approval.id,
                    plan_json=task.plan_json,
                    review_json=task.review_json,
                ),
            )

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
            # T-PIPELINE-REPAIR-CAP: granting the compile-repair-cap approval
            # means a reviewer has manually fixed (or chosen to accept) the
            # sandbox state. Clear the cap flag and re-run compile_gate so
            # the pipeline either sails through or surfaces another failure.
            pending_compile_id = pipeline_state.get("pending_compile_repair_approval_id")
            if pending_compile_id == approval_id:
                pipeline_state.pop("pending_compile_repair_approval_id", None)
                pipeline_state.pop("compile_repair_cap_exceeded", None)
                pipeline_state.pop("compile_gate_done", None)
                pipeline_state.pop("compile_gate", None)
                pipeline_state.pop("evidence_chain_validated", None)
                pipeline_state.pop("evidence_chain", None)
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                self._execute_develop_pipeline(
                    task=task, actor_name=actor_name, plan=plan_document, approval_id=approval_id
                )
                return
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

    def resume_task(self, *, task: Task, actor_name: str | None = None) -> bool:
        if not bool(getattr(self.tool_gateway.settings, "resumability_enabled", True)):
            return False
        checkpoint = read_task_checkpoint(task)
        if checkpoint is None or not checkpoint.can_resume:
            return False
        actor = actor_name or task.actor_name
        self._restore_task_checkpoint_payload(task, checkpoint)
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TASK_RESUMED,
            source=EventSource.SYSTEM,
            stage=task.workflow_stage,
            role=RoleName.SYSTEM,
            message=f"Task resumed from checkpoint stage {checkpoint.stage}.",
            payload={
                "resumed_from_stage": checkpoint.stage,
                "resume_method": checkpoint.resume_method,
                "checkpoint_completed_at": checkpoint.completed_at.isoformat(),
            },
        )
        commit_checkpoint(self.db, label=f"task_resumed_{checkpoint.stage}")

        if checkpoint.stage == "awaiting_approval":
            return True

        if checkpoint.stage in {"codegen", "compile", "review_post"}:
            plan_document = GeneratedPlan.model_validate(task.plan_json or {})
            self._prepare_develop_resume(task=task, checkpoint=checkpoint)
            self._execute_plan(task=task, actor_name=actor, plan=plan_document)
            return True

        if checkpoint.stage == "review_pre":
            plan_document = GeneratedPlan.model_validate(task.plan_json or {})
            self._resume_from_review_checkpoint(task=task, actor_name=actor, plan=plan_document)
            return True

        if checkpoint.stage == "plan":
            plan_document = GeneratedPlan.model_validate(task.plan_json or {})
            self._resume_after_plan_checkpoint(task=task, actor_name=actor, plan=plan_document)
            return True

        if checkpoint.stage in {"intake", "translate", "retrieve"}:
            self._resume_pre_plan_checkpoint(task=task, actor_name=actor, checkpoint=checkpoint)
            return True

        return False

    def _resume_pre_plan_checkpoint(
        self,
        *,
        task: Task,
        actor_name: str,
        checkpoint: TaskCheckpoint,
    ) -> None:
        payload = checkpoint.output_payload
        if checkpoint.stage == "intake":
            semantic_translation = self._translate_request(task=task, actor_name=actor_name, issue_context=None)
            self._apply_jira_issue_key_fallback(task=task, semantic_translation=semantic_translation)
            task.translation_json = semantic_translation.model_dump(mode="json")
            self._write_task_checkpoint(
                task,
                stage="translate",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                ),
            )
        else:
            semantic_translation = GeneratedSemanticTranslation.model_validate(task.translation_json or {})
        resume_stage = "translate" if checkpoint.stage == "intake" else checkpoint.stage
        issue_context = payload.get("issue_context")
        if not isinstance(issue_context, dict):
            issue_context = None
        planning_knowledge_context = payload.get("planning_knowledge_context")
        if not isinstance(planning_knowledge_context, dict):
            planning_knowledge_context = None
        planning_request_text = payload.get("planning_request_text")
        if not isinstance(planning_request_text, str) or not planning_request_text.strip():
            planning_request_text = task.request_text

        if resume_stage == "translate":
            if task.scenario in {"jira_issue_plan", "jira_issue_develop", "jira_issue_writeback"}:
                if issue_context is None:
                    issue_context = self._prefetch_jira_issue_context(
                        task=task,
                        actor_name=actor_name,
                        issue_key=semantic_translation.issue_key,
                    )
                    if issue_context is None:
                        return
                if task.scenario in {"jira_issue_plan", "jira_issue_develop"} and planning_knowledge_context is None:
                    planning_knowledge_context = self._prefetch_planning_repository_context(
                        task=task,
                        actor_name=actor_name,
                        semantic_translation=semantic_translation,
                    )
                planning_request_text = self._augment_request_with_context(
                    original_request=task.request_text,
                    translation_document=task.translation_json or {},
                    issue_context=issue_context,
                    planning_knowledge_context=(
                        planning_knowledge_context
                        if task.scenario in {"jira_issue_plan", "jira_issue_develop"}
                        else None
                    ),
                )
            else:
                planning_request_text = self._augment_request_with_context(
                    original_request=task.request_text,
                    translation_document=task.translation_json or {},
                    issue_context=None,
                    planning_knowledge_context=None,
                )
            self._write_task_checkpoint(
                task,
                stage="retrieve",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                    issue_context=issue_context,
                    planning_knowledge_context=planning_knowledge_context,
                    planning_request_text=planning_request_text,
                ),
            )

        if self._anchor_precheck_fails(task):
            return

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.PLANNING,
            new_stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            source=EventSource.ORCHESTRATOR,
            message="Task resumed planner execution from checkpoint.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.PLANNING_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            message="Planner role resumed structured plan generation.",
            payload={"actor_name": actor_name, "resumed_from_stage": checkpoint.stage},
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
            message="Execution plan generated after resume.",
            payload={
                "actor_name": actor_name,
                "plan": task.plan_json,
                "provider_name": planning_result.provider_name,
                "model_name": planning_result.model_name,
                "used_fallback": planning_result.used_fallback,
                "fallback_reason": planning_result.fallback_reason,
            },
        )
        self._workspace_write_plan(task, plan_document, reason="resume_planner_generated")
        self._write_task_checkpoint(
            task,
            stage="plan",
            output_payload=self._task_checkpoint_payload(
                task,
                semantic_translation=task.translation_json,
                issue_context=issue_context,
                planning_knowledge_context=planning_knowledge_context,
                planning_request_text=planning_request_text,
                plan_json=task.plan_json,
            ),
        )
        commit_checkpoint(self.db, label="resume_plan_generated")
        self._resume_after_plan_checkpoint(task=task, actor_name=actor_name, plan=plan_document)

    def _resume_after_plan_checkpoint(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
    ) -> None:
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.REVIEWING,
            new_stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Reviewer resumed plan validation from checkpoint.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.REVIEW_STARTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Reviewer role resumed pre-execution validation.",
            payload={"plan_id": plan.plan_id},
        )
        review_result = self.reviewer_agent.review_plan(
            task_id=task.id,
            actor_name=actor_name,
            plan=plan,
        )
        task.review_json = review_result.review.model_dump(mode="json")
        self._write_task_checkpoint(
            task,
            stage="review_pre",
            output_payload=self._task_checkpoint_payload(
                task,
                plan_json=task.plan_json,
                review_json=task.review_json,
            ),
        )
        self._resume_from_review_checkpoint(task=task, actor_name=actor_name, plan=plan)

    def _resume_from_review_checkpoint(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
    ) -> None:
        review_json = task.review_json if isinstance(task.review_json, dict) else {}
        verdict = str(review_json.get("verdict") or "").casefold()
        if verdict == "approved":
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.REVIEW_PASSED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                message="Reviewer-approved plan replayed from checkpoint.",
                payload={"review": review_json},
            )
            commit_checkpoint(self.db, label="resume_review_passed_pre_execution")
            self._execute_plan(task=task, actor_name=actor_name, plan=plan)
            return
        if verdict == "requires_approval":
            self._request_pre_execution_resume_approval(task=task, plan=plan, review_json=review_json)
            return
        task.latest_result_json = {
            "status": TaskStatus.FAILED.value,
            "message": str(review_json.get("summary") or "Reviewer rejected the plan before execution."),
            "review": review_json,
        }
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Task failed during resumed plan review.",
        )

    def _request_pre_execution_resume_approval(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        review_json: dict[str, object],
    ) -> None:
        approval_requirements = review_json.get("approval_requirements")
        required_approver_role = ActorRole.TEAM_LEAD.value
        if isinstance(approval_requirements, list) and approval_requirements:
            first = approval_requirements[0]
            if isinstance(first, dict) and first.get("approver_role"):
                required_approver_role = str(first["approver_role"])
        approval = Approval(
            task_id=task.id,
            action_name=self._resolve_tool_name(plan),
            status=ApprovalStatus.PENDING,
            requested_by_role=RoleName.REVIEWER,
            approver_role=required_approver_role,
            requested_by_actor_name=task.actor_name,
            risk_level=task.risk_level,
            risk_category=task.risk_category,
            reason="Reviewer marked the resumed plan as approval-required before execution.",
            request_payload_json={
                "request_text": task.request_text,
                "scenario": task.scenario,
                "proposed_plan": task.plan_json,
                "review": review_json,
            },
            policy_snapshot_json={
                "decision": "require_approval",
                "source": "reviewer_pre_execution_gate_resume",
                "tool_name": self._resolve_tool_name(plan),
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
            "review": review_json,
        }
        self._write_task_checkpoint(
            task,
            stage="awaiting_approval",
            output_payload=self._task_checkpoint_payload(
                task,
                approval_id=approval.id,
                plan_json=task.plan_json,
                review_json=review_json,
            ),
        )
        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.AWAITING_APPROVAL,
            new_stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Task is awaiting manual approval after resumed review.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_REQUESTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Approval requested for resumed planned action.",
            payload={
                "approval_id": approval.id,
                "action_name": approval.action_name,
                "approver_role": approval.approver_role,
                "review_summary": review_json.get("summary"),
            },
        )
        commit_checkpoint(self.db, label="resume_awaiting_approval_pre_execution")

    def _prepare_develop_resume(self, *, task: Task, checkpoint: TaskCheckpoint) -> None:
        if task.scenario != "jira_issue_develop":
            return
        pipeline_state = self._load_develop_pipeline_state(task)
        if checkpoint.stage == "codegen":
            sandbox = self._build_develop_sandbox(task)
            if sandbox.exists() and not sandbox.is_clean():
                snapshot_id = (
                    checkpoint.sandbox_snapshot_id
                    or str(pipeline_state.get("pre_codegen_snapshot_id") or "")
                )
                if snapshot_id and sandbox.rollback_to_snapshot(snapshot_id):
                    pipeline_state.pop("sandbox_result", None)
                    pipeline_state.pop("patch_method", None)
                    pipeline_state["sandbox_rollback_on_resume"] = True
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                else:
                    raise SandboxError("Cannot resume: sandbox has uncommitted changes and no valid snapshot.")

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

        translator_settings = self.semantic_translator.settings
        provider_mode = translator_settings.semantic_translator_provider
        will_call_minimax = provider_mode == "minimax" or (
            provider_mode == "auto" and bool(translator_settings.minimax_api_key)
        )
        request_size_bytes = _json_size_bytes(
            {
                "request_text": task.request_text,
                "scenario": task.scenario,
                "actor_name": actor_name,
                "issue_context": issue_context,
            }
        )
        translation_started = time.perf_counter()
        if will_call_minimax:
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.MM_TRANSLATION_STARTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PRIMARY,
                message="MiniMax semantic translation call started.",
                payload={
                    "provider_name": "minimax",
                    "model_name": translator_settings.semantic_translator_model,
                    "request_size_bytes": request_size_bytes,
                    "duration_ms": 0,
                },
            )
            commit_checkpoint(self.db, label="mm_translation_started")

        try:
            translation_result = self.semantic_translator.translate(
                task_id=task.id,
                request_text=task.request_text,
                scenario=task.scenario,
                actor_name=actor_name,
                issue_context=issue_context,
            )
        except Exception as exc:
            if will_call_minimax:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.MM_TRANSLATION_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.PLANNING,
                    role=RoleName.PRIMARY,
                    message="MiniMax semantic translation call failed.",
                    payload={
                        "provider_name": "minimax",
                        "model_name": translator_settings.semantic_translator_model,
                        "request_size_bytes": request_size_bytes,
                        "duration_ms": int((time.perf_counter() - translation_started) * 1000),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:5000],
                        "traceback": _truncated_traceback(exc),
                    },
                )
                commit_checkpoint(self.db, label="mm_translation_failed")
            raise
        translation_document = translation_result.translation

        if translation_result.used_fallback and translation_result.fallback_reason:
            if will_call_minimax:
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.MM_TRANSLATION_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.PLANNING,
                    role=RoleName.PRIMARY,
                    message="MiniMax semantic translation failed and fallback translation was used.",
                    payload={
                        "provider_name": "minimax",
                        "model_name": translator_settings.semantic_translator_model,
                        "request_size_bytes": request_size_bytes,
                        "duration_ms": int((time.perf_counter() - translation_started) * 1000),
                        "error_type": "ProviderFallback",
                        "error_message": translation_result.fallback_reason[:1000],
                        "traceback": "",
                    },
                )
                commit_checkpoint(self.db, label="mm_translation_failed")
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
        elif will_call_minimax:
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.MM_TRANSLATION_SUCCEEDED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PRIMARY,
                message="MiniMax semantic translation call completed.",
                payload={
                    "provider_name": "minimax",
                    "model_name": translator_settings.semantic_translator_model,
                    "request_size_bytes": request_size_bytes,
                    "duration_ms": int((time.perf_counter() - translation_started) * 1000),
                },
            )
            commit_checkpoint(self.db, label="mm_translation_succeeded")

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

        jira_started = time.perf_counter()
        jira_request_size_bytes = _json_size_bytes({"issue_key": issue_key})
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.JIRA_FETCH_STARTED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            tool_name="jira.get_issue",
            message="Jira issue context fetch started.",
            payload={
                "provider_name": "jira",
                "model_name": None,
                "request_size_bytes": jira_request_size_bytes,
                "duration_ms": 0,
                "issue_key": issue_key,
            },
        )
        commit_checkpoint(self.db, label="jira_fetch_started")

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
            error_kind, user_message = self._classify_jira_error(exc, issue_key)
            failure_payload = {
                "issue_key": issue_key,
                "error": str(exc),
                "error_kind": error_kind,
                "http_status": getattr(exc, "http_status", None),
            }
            if event_type == EventType.TOOL_TIMED_OUT:
                failure_payload.update(
                    {
                        "reason": "external_api_timeout",
                        "provider_name": "jira",
                    }
                )
            # Disambiguate 404: Jira returns 404 for both "issue missing" and
            # "token can't see project". Probe /myself to detect the
            # token-expiry case so the user gets the right remediation.
            if error_kind == "not_found_or_invisible":
                probe_status = self._probe_jira_auth_health()
                if probe_status == 401:
                    error_kind = "auth_expired"
                    user_message = (
                        f"Jira authentication failed (HTTP 401 on /myself). The API token has "
                        f"expired or been revoked, which makes issue lookups appear as 404. "
                        f"Refresh OPS_AGENT_JIRA_API_TOKEN in apps/backend/.env from "
                        f"https://id.atlassian.com/manage-profile/security/api-tokens and restart the backend. "
                        f"(Original failure: {exc})"
                    )
                    failure_payload["error_kind"] = error_kind
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.JIRA_FETCH_FAILED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PLANNER,
                tool_name="jira.get_issue",
                message="Jira issue context fetch failed.",
                payload={
                    "provider_name": "jira",
                    "model_name": None,
                    "request_size_bytes": jira_request_size_bytes,
                    "duration_ms": int((time.perf_counter() - jira_started) * 1000),
                    "issue_key": issue_key,
                    "http_status": getattr(exc, "http_status", None),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:5000],
                    "traceback": _truncated_traceback(exc),
                },
            )
            commit_checkpoint(self.db, label="jira_fetch_failed")
            record_event(
                self.db,
                task_id=task.id,
                event_type=event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.PLANNING,
                role=RoleName.PLANNER,
                tool_name="jira.get_issue",
                message="Planner failed to load Jira issue context before plan generation.",
                payload=failure_payload,
            )
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": user_message,
                "error": str(exc),
                "error_kind": error_kind,
                "http_status": getattr(exc, "http_status", None),
                "semantic_translation": task.translation_json,
            }
            if event_type == EventType.TOOL_TIMED_OUT:
                task.latest_result_json["reason"] = "external_api_timeout"
                task.latest_result_json["provider_name"] = "jira"
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                source=EventSource.ORCHESTRATOR,
                message=user_message,
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.FINAL_RESPONSE_EMITTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.PRIMARY,
                message="Final response emitted after Jira context preload failure.",
                payload={"issue_key": issue_key, "error_kind": error_kind},
            )
            return None

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.JIRA_FETCH_SUCCEEDED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.PLANNING,
            role=RoleName.PLANNER,
            tool_name="jira.get_issue",
            message="Jira issue context fetch completed.",
            payload={
                "provider_name": "jira",
                "model_name": None,
                "request_size_bytes": jira_request_size_bytes,
                "duration_ms": int((time.perf_counter() - jira_started) * 1000),
                "issue_key": issue_key,
                "http_status": result.get("_status_code") if isinstance(result, dict) else None,
            },
        )
        commit_checkpoint(self.db, label="jira_fetch_succeeded")
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

    def _probe_jira_auth_health(self) -> int | None:
        """Probe /rest/api/3/myself to disambiguate 404 vs 401 on issue lookups.

        Returns the HTTP status code, or None if we couldn't reach Jira
        (connection error, timeout, missing config). Caller uses 401 → token
        expired; 200 → token healthy so the upstream 404 is genuine; anything
        else is treated as inconclusive.

        Kept narrow: short timeout (5s), no retries, swallows all errors —
        this is a diagnostic call, not a primary code path.
        """
        try:
            settings = self.tool_gateway.settings
            base_url = (settings.jira_base_url or "").rstrip("/")
            if not base_url:
                return None
            headers = {"Accept": "application/json"}
            auth: tuple[str, str] | None = None
            if getattr(settings, "jira_bearer_token", None):
                headers["Authorization"] = f"Bearer {settings.jira_bearer_token}"
            elif settings.jira_email and settings.jira_api_token:
                auth = (settings.jira_email, settings.jira_api_token)
            else:
                return None
            with httpx.Client(timeout=external_http_timeout(5.0)) as client:
                response = client.get(
                    f"{base_url}/rest/api/3/myself",
                    headers=headers,
                    auth=auth,
                )
            return response.status_code
        except Exception:
            return None

    @staticmethod
    def _classify_jira_error(exc: Exception, issue_key: str) -> tuple[str, str]:
        """Map a Jira tool exception to (error_kind, user_facing_message).

        Jira returns 404 for both "issue does not exist" and "no permission to
        see it" — the API deliberately conflates them to avoid leaking issue
        existence. Token expiry surfaces as 401 on /myself but as 404 on
        /issue/X. Distinguishing matters because the remediation differs:

          - 401 anywhere -> token is invalid; user must refresh credentials
          - 403 -> token valid but no project access
          - 404 -> issue genuinely missing OR token can't see the project
          - 408/429/5xx -> transient; retry safe

        We only see the status code when the tool layer raises
        ToolInvocationError with `http_status` populated (added 2026-04-27).
        """
        http_status = getattr(exc, "http_status", None)
        timed_out = bool(getattr(exc, "timed_out", False))

        if timed_out:
            return (
                "transient_timeout",
                f"Jira lookup for {issue_key} timed out. The Jira service may be slow — retry shortly.",
            )
        if http_status == 401:
            return (
                "auth_expired",
                (
                    f"Jira authentication failed (HTTP 401). The API token has expired or been "
                    f"revoked. Refresh OPS_AGENT_JIRA_API_TOKEN in apps/backend/.env from "
                    f"https://id.atlassian.com/manage-profile/security/api-tokens and restart the backend."
                ),
            )
        if http_status == 403:
            return (
                "permission_denied",
                f"Jira denied access to {issue_key} (HTTP 403). The token is valid but the account lacks permission for this project.",
            )
        if http_status == 404:
            return (
                "not_found_or_invisible",
                (
                    f"Jira issue {issue_key} could not be retrieved (HTTP 404). The issue may be "
                    f"deleted, in a different project, or the API token may not have access. "
                    f"Verify the key, then if the issue should exist, try refreshing OPS_AGENT_JIRA_API_TOKEN."
                ),
            )
        if http_status is not None and 500 <= http_status < 600:
            return (
                "transient_server_error",
                f"Jira returned HTTP {http_status} for {issue_key}. Likely transient — retry shortly.",
            )
        if http_status in {408, 429}:
            return (
                "rate_limited",
                f"Jira rate-limited the request for {issue_key} (HTTP {http_status}). Retry after backoff.",
            )
        return (
            "unknown",
            f"Failed to load Jira issue {issue_key}: {exc}",
        )

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
            # Sandbox-source alignment: if KB router/scoring selected a specific
            # source, look up its on-disk path from knowledge_source_specs and
            # persist on task.translation_json["source_path"] so that
            # _resolve_develop_repo_url picks it up (priority #1 in the chain)
            # instead of falling back to settings.knowledge_source_path (which
            # may point at a different repo entirely in multi-source setups).
            try:
                from app.services.source_spec_lookup import lookup_source_path
                trace = result.get("answer_trace") if isinstance(result, dict) else None
                selected = trace.get("selected_sources") if isinstance(trace, dict) else None
                primary_name = ""
                if isinstance(selected, list) and selected:
                    primary_name = str(selected[0] or "").strip()
                if primary_name:
                    resolved = lookup_source_path(primary_name, self.tool_gateway.settings)
                    if resolved:
                        translation = dict(task.translation_json) if isinstance(task.translation_json, dict) else {}
                        translation["source_path"] = resolved
                        translation["source_name"] = primary_name
                        task.translation_json = translation
                        self.db.flush()
            except Exception:
                pass  # best-effort; downstream chain still has fallbacks
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
            self._write_task_checkpoint(
                task,
                stage="translate",
                output_payload=self._task_checkpoint_payload(
                    task,
                    semantic_translation=task.translation_json,
                ),
            )

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
            failure_payload = {"error": str(exc), "approval_id": approval_id}
            latest_result = {
                "status": TaskStatus.FAILED.value,
                "message": str(exc),
            }
            if failed_event_type == EventType.TOOL_TIMED_OUT:
                failure_payload.update(
                    {
                        "reason": "external_api_timeout",
                        "provider_name": tool_name.split(".", 1)[0],
                    }
                )
                latest_result.update(failure_payload)
            record_event(
                self.db,
                task_id=task.id,
                event_type=failed_event_type,
                source=EventSource.TOOL_GATEWAY,
                stage=execution_stage,
                role=execution_role,
                tool_name=tool_name,
                message="Tool execution failed.",
                payload=failure_payload,
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
                payload=failure_payload,
            )
            task.latest_result_json = latest_result
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
        if tool_name == "knowledge.search":
            self._write_task_checkpoint(
                task,
                stage="retrieve",
                output_payload=self._task_checkpoint_payload(
                    task,
                    tool_name=tool_name,
                    tool_result=result,
                ),
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
        self._write_task_checkpoint(
            task,
            stage="review_post",
            output_payload=self._task_checkpoint_payload(
                task,
                tool_name=tool_name,
                tool_result=result,
                review_json=task.review_json,
            ),
        )

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
            message="Jira Development pipeline started." if user_lang == "zh" else "Task entered Jira issue development pipeline.",
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
        _pipeline_source_path = self._resolve_knowledge_source_path(task)

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
            _evidence_source_name = (
                str(translation.get("source_name") or "").strip()
                or str(getattr(self.tool_gateway.settings, "knowledge_source_name", "") or "").strip()
                or None
            )
            try:
                evidence = build_evidence_bundle(
                    request_text=task.request_text,
                    normalized_request=translation.get("normalized_request"),
                    source_tree=_pipeline_source_path,
                    grounding_terms=translation.get("grounding_terms"),
                    planner_must_touch=getattr(plan, "must_touch_files", None) or [],
                    has_destructive_verb=_has_destructive_verb(task.request_text or ""),
                    db=self.db,
                    source_name=_evidence_source_name,
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
                # Option B: pre-codegen source injection.
                # The evidence_bundle's FTS5 anchor matching surfaced
                # additional must_touch / candidate files that the planner
                # didn't pre-commit. Read their actual content and inject
                # into context_files so codegen sees real method signatures
                # and existing field shapes, not just file paths. Without
                # this, the LLM has been hallucinating API surfaces (e.g.
                # invented SessionManager method names) and producing
                # patches that don't match the real code's contracts.
                _inject_paths: list[str] = []
                _inject_paths.extend(evidence.must_touch_files or [])
                # Top candidates (capped) — useful supporting context.
                for cf in (evidence.candidate_files or [])[:5]:
                    if cf not in _inject_paths:
                        _inject_paths.append(cf)
                _injected_count = 0
                _injected_bytes = 0
                for rel in _inject_paths:
                    norm_rel = self._normalize_codegen_path(rel)
                    if not norm_rel or norm_rel in context_files:
                        continue
                    body = self._read_context_file(
                        source_path=_pipeline_source_path,
                        sandbox_dir=self._develop_sandbox_dir(task),
                        relative_path=norm_rel,
                    )
                    if body is None:
                        continue
                    # 50KB per file cap to avoid prompt bloat
                    if len(body) > 50_000:
                        body = body[:50_000]
                    context_files[norm_rel] = body
                    _injected_count += 1
                    _injected_bytes += len(body)
                if _injected_count > 0:
                    pipeline_state["context_file_paths"] = list(context_files)
                    pipeline_state["context_files"] = context_files
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.KNOWLEDGE,
                        role=RoleName.KNOWLEDGE,
                        tool_name="codegen_context.inject_from_evidence",
                        message=(
                            f"Injected {_injected_count} file(s) "
                            f"({_injected_bytes} bytes) from evidence "
                            f"into codegen context."
                        ),
                        payload={
                            "injected_files": _inject_paths[:_injected_count],
                            "injected_bytes": _injected_bytes,
                        },
                    )
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
                    _capture_first_attempt_diff(pipeline_state, codegen_result["diff"])
                    pipeline_state["files_changed"] = codegen_result.get("files_changed", [])
                    pipeline_state["codegen_provider"] = "deterministic_rename"
                    pipeline_state["file_summaries"] = codegen_result.get("file_summaries", [])
                    self._workspace_write_attempt_diff(
                        task,
                        pipeline_state,
                        diff=str(codegen_result.get("diff") or ""),
                    )
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                    self._write_task_checkpoint(
                        task,
                        stage="codegen",
                        output_payload=self._task_checkpoint_payload(
                            task,
                            pipeline_state=self._load_develop_pipeline_state(task),
                            codegen_result=codegen_result,
                            plan_json=task.plan_json,
                        ),
                        resume_method="redo_stage",
                    )
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.ACTION,
                        role=RoleName.ACTION,
                        tool_name="codegen.deterministic_rename",
                        message=(
                            f"Deterministic rename completed: {rename_pair[0]} → {rename_pair[1]}, "
                            f"Modified {len(codegen_result.get('files_changed', []))}  file(s)"
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
                            f"Deterministic rename skipped: '{rename_pair[0]}' 在 {len(context_files)}  not found in context files, "
                            f"Falling back to LLM codegen"
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
            _must_touch = list(getattr(plan, "must_touch_files", None) or [])

            def _path_in_must_touch(candidate: str) -> bool:
                # Suffix-tolerant match: evidence anchors sometimes hold
                # basenames while plan emits full paths (and vice versa).
                # Without this tolerance, _must_touch_existing comes up
                # empty and the batcher falls back to "all files in one
                # batch", letting codegen modify files outside the plan's
                # allowed_set (caught later by validation, but only after
                # a wasted DeepSeek call).
                if candidate in _must_touch:
                    return True
                for target in _must_touch:
                    if target.endswith("/" + candidate) or candidate.endswith("/" + target):
                        return True
                return False

            _must_touch_existing = [
                (p, c) for p, c in existing_files if _path_in_must_touch(p)
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
            merged_claims: list[dict[str, object]] = []
            seen_files: set[str] = set()
            codegen_provider = "unknown"

            # Pipe translation constraints into plan_json so codegen sees them
            _plan_json_for_codegen = dict(task.plan_json or plan.model_dump(mode="json"))
            _translation = task.translation_json or {}
            if _translation.get("constraints"):
                _plan_json_for_codegen["constraints"] = _translation["constraints"]
            memory_context = self._build_codegen_memory_context(task)
            if memory_context:
                _plan_json_for_codegen["memory_context"] = memory_context

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
                batch_claims = batch_result.get("claims")
                if isinstance(batch_claims, list):
                    merged_claims.extend(
                        claim for claim in batch_claims if isinstance(claim, dict)
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
            if merged_claims:
                codegen_result["claims"] = merged_claims
            pipeline_state["codegen_result"] = codegen_result
            pipeline_state["diff"] = codegen_result["diff"]
            _capture_first_attempt_diff(pipeline_state, codegen_result["diff"])
            pipeline_state["files_changed"] = merged_files_changed
            pipeline_state["codegen_provider"] = codegen_provider
            pipeline_state["file_summaries"] = merged_file_summaries
            self._workspace_write_attempt_diff(
                task,
                pipeline_state,
                diff=str(codegen_result.get("diff") or ""),
            )
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            self._write_task_checkpoint(
                task,
                stage="codegen",
                output_payload=self._task_checkpoint_payload(
                    task,
                    pipeline_state=self._load_develop_pipeline_state(task),
                    codegen_result=codegen_result,
                    plan_json=task.plan_json,
                ),
                resume_method="redo_stage",
            )
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

        # Static shape pre-gate (Stage X.1): catches destructive empty patches
        # (pure-deletion of must_touch files) before any LLM gate runs.
        # Codex consult verdict on P69-19 dogfood: review gates approved a
        # patch that deleted package+imports and added 0 lines, claiming
        # "all goals met". Static line-count check rejects in ms.
        try:
            from app.services.diff_shape_check import evaluate_patch_shape
            must_touch_paths = list(getattr(plan, "must_touch_files", []) or [])
            task_intent = " ".join([
                str(getattr(plan, "objective", "") or ""),
                str(getattr(task, "request_text", "") or ""),
            ])
            shape = evaluate_patch_shape(diff, must_touch_files=must_touch_paths, task_intent=task_intent)
            record_event(
                self.db,
                task_id=task.id,
                event_type=(
                    EventType.TOOL_FAILED if shape.destructive else EventType.TOOL_SUCCEEDED
                ),
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="diff_shape_check.evaluate",
                message=(
                    f"diff shape: added={shape.totals['added']} "
                    f"removed={shape.totals['removed']} destructive={shape.destructive}"
                ),
                payload=shape.to_payload(),
            )
            if shape.destructive:
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=f"Diff shape pre-gate rejected destructive patch: {shape.reason}",
                    payload={
                        "plan_id": plan.plan_id,
                        "shape_check": shape.to_payload(),
                    },
                )
                return
        except Exception as exc:  # noqa: BLE001
            self._workspace_append_audit(
                task,
                "diff_shape_check.errored",
                {"error": str(exc)[:400]},
            )

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
                sandbox = self._build_develop_sandbox(task)
                pre_apply_snapshot = sandbox.snapshot_id()
                if pre_apply_snapshot:
                    pipeline_state["pre_codegen_snapshot_id"] = pre_apply_snapshot
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                self._write_task_checkpoint(
                    task,
                    stage="codegen",
                    output_payload=self._task_checkpoint_payload(
                        task,
                        pipeline_state=self._load_develop_pipeline_state(task),
                        codegen_result=codegen_result,
                        plan_json=task.plan_json,
                    ),
                    sandbox_snapshot_id=pre_apply_snapshot,
                    resume_method="redo_stage",
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
            post_apply_snapshot = self._build_develop_sandbox(task).snapshot_id()
            if post_apply_snapshot:
                pipeline_state["sandbox_snapshot_id"] = post_apply_snapshot
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            self._write_task_checkpoint(
                task,
                stage="codegen",
                output_payload=self._task_checkpoint_payload(
                    task,
                    pipeline_state=self._load_develop_pipeline_state(task),
                    codegen_result=codegen_result,
                    sandbox_result=sandbox_result,
                    plan_json=task.plan_json,
                ),
                sandbox_snapshot_id=post_apply_snapshot,
                resume_method="replay_from_output",
            )

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
                    if bool(getattr(self.tool_gateway.settings, "verification_profile_enabled", True)):
                        test_result = self._prepare_compile_only_verification(
                            task=task,
                            plan=plan,
                            pipeline_state=pipeline_state,
                            error_message=error_message,
                        )
                    else:
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

        # --- Diff symbol verifier (post-codegen, pre-compile) ---
        # Catches "Receiver.member" hallucinations where the receiver is a
        # PascalCase class declared in the repo but the named member doesn't
        # exist on it (e.g. v46 P69-17 SessionManager.getHomeAddress, v50
        # SessionManager.fabricated). Findings are stored in pipeline_state
        # so compile_repair_loop can fold them into the repair prompt with
        # actionable signal ("X has these members: [...], your invented name
        # is not among them"). Provider-agnostic — operates only on diff text.
        if not pipeline_state.get("diff_symbol_verifier_done"):
            try:
                from app.services.diff_symbol_verifier import verify_diff_symbols
                _diff_text = str(pipeline_state.get("diff") or "")
                _repo_path = self._resolve_develop_repo_url(task=task, plan=plan)
                if _diff_text and _repo_path:
                    _repo_root = Path(_repo_path)
                    if _repo_root.exists() and _repo_root.is_dir():
                        _dsv_report = verify_diff_symbols(
                            diff=_diff_text,
                            repo_root=_repo_root,
                        )
                        pipeline_state["diff_symbol_verifier"] = _dsv_report.to_payload()
                        if _dsv_report.has_hallucinations:
                            record_event(
                                self.db,
                                task_id=task.id,
                                event_type=EventType.REVIEW_FAILED,
                                source=EventSource.ORCHESTRATOR,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                tool_name="diff_symbol_verifier.flagged",
                                message=(
                                    f"Symbol verifier flagged "
                                    f"{len(_dsv_report.findings)} hallucinated "
                                    f"reference(s); compile_repair will receive "
                                    f"actionable feedback."
                                ),
                                payload=_dsv_report.to_payload(),
                            )
                            self._workspace_append_audit(
                                task,
                                "diff_symbol_verifier.flagged",
                                _dsv_report.to_payload(),
                            )
                pipeline_state["diff_symbol_verifier_done"] = True
            except Exception as _dsv_exc:  # noqa: BLE001
                # Verifier MUST be soft — failures fall through to compile_gate.
                import logging as _log
                _log.getLogger("orchestrator").warning(
                    "diff_symbol_verifier.errored: %s", str(_dsv_exc)[:200]
                )
                pipeline_state["diff_symbol_verifier_done"] = True
                pipeline_state["diff_symbol_verifier_skipped"] = "errored"
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        # --- Compile gate (T-040 defense line 5) with multi-round repair ---
        # T-PIPELINE-REPAIR-CAP: up to N repair rounds, then either pass,
        # transition to AWAITING_APPROVAL with a structured payload (default),
        # or fall back to legacy fail-fast when the operator opts out.
        if not pipeline_state.get("compile_gate_done"):
            outcome = self._run_compile_repair_loop(
                task=task,
                actor_name=actor_name,
                plan=plan,
                pipeline_state=pipeline_state,
                approval_id=approval_id,
            )
            if outcome in {"approval_requested", "failed"}:
                return
            self._write_task_checkpoint(
                task,
                stage="compile",
                output_payload=self._task_checkpoint_payload(
                    task,
                    pipeline_state=self._load_develop_pipeline_state(task),
                    compile_result=pipeline_state.get("compile_gate"),
                    plan_json=task.plan_json,
                ),
                sandbox_snapshot_id=self._build_develop_sandbox(task).snapshot_id(),
            )

        # --- Stage X.8.b feature-presence pre-gate ---
        # Catches the P69-17 failure mode: compile_repair reverted the
        # feature, baseline file ships, all LLM gates pass on diff text
        # but FILE has no implementation. Static check: each must_touch
        # file must contain at least one required token derived from
        # plan.objective + translation.search_queries + spec text.
        if not pipeline_state.get("feature_presence_done"):
            try:
                from app.services.feature_presence_check import (
                    derive_required_tokens,
                    derive_required_tokens_strict,
                    evaluate_feature_presence,
                    extract_added_lines_per_file,
                    merge_diffs_by_file,
                )
                translation = task.translation_json if isinstance(task.translation_json, dict) else {}
                # G2 — prefer strict tokens (CamelCase / snake_case only,
                # generic English dropped). Fall back to legacy permissive
                # tokens when strict yields nothing, so tasks with purely
                # natural-language specs still get a (weaker) check.
                strict_tokens = derive_required_tokens_strict(
                    objective=str(getattr(plan, "objective", "") or ""),
                    grounding_terms=translation.get("grounding_terms") or [],
                    spec_text=str(translation.get("normalized_request") or ""),
                    must_touch_files=list(getattr(plan, "must_touch_files", []) or []),
                )
                required_tokens = strict_tokens or derive_required_tokens(
                    objective=str(getattr(plan, "objective", "") or ""),
                    search_queries=translation.get("search_queries") or [],
                    must_touch_files=list(getattr(plan, "must_touch_files", []) or []),
                    spec_text=str(translation.get("normalized_request") or ""),
                )
                must_touch = list(getattr(plan, "must_touch_files", []) or [])
                file_contents: dict[str, str] = {}
                fp_sandbox_dir = self._develop_sandbox_dir(task)
                if fp_sandbox_dir.exists():
                    for rel in must_touch:
                        try:
                            full = fp_sandbox_dir / rel
                            if full.is_file():
                                file_contents[rel] = full.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            pass
                # Defensive: skip when no file contents read (e.g. test
                # fixtures without populated sandbox, or read errors).
                if not file_contents:
                    pipeline_state["feature_presence_done"] = True
                    pipeline_state["feature_presence_skipped"] = "no_file_contents"
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                else:
                    # Option A: feature_presence repair loop. Mirrors the
                    # compile-repair pattern — when the gate rejects, build
                    # a focused repair prompt listing what's missing and
                    # re-call codegen. Bounded by MAX_FP_REPAIR rounds
                    # (default 2). After exhaustion, fail-close.
                    MAX_FP_REPAIR = 2
                    presence = None
                    for fp_round in range(MAX_FP_REPAIR + 1):  # initial + N repairs
                        diff_text = pipeline_state.get("diff") or ""
                        diff_added_per_file = extract_added_lines_per_file(diff_text)
                        presence = evaluate_feature_presence(
                            must_touch_files=must_touch,
                            file_contents=file_contents,
                            required_tokens=required_tokens,
                            diff_added_per_file=diff_added_per_file or None,
                            min_tokens_per_file_ratio=0.5,
                        )
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=(
                                EventType.TOOL_FAILED
                                if presence.feature_absent
                                else EventType.TOOL_SUCCEEDED
                            ),
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="feature_presence_check.evaluate",
                            message=(
                                f"feature presence (round {fp_round}): "
                                f"{presence.reason[:200]}"
                            ),
                            payload={
                                **presence.to_payload(),
                                "fp_repair_round": fp_round,
                            },
                        )
                        if not presence.feature_absent:
                            break  # gate passed
                        if fp_round >= MAX_FP_REPAIR:
                            break  # exhausted; fall through to fail-close

                        # Build a focused repair prompt listing what's missing.
                        sample_unmatched = (
                            presence.unmatched_required_files[:3] if presence else []
                        )
                        # Include the accumulated diff so codegen can see what
                        # the previous rounds already produced and only ADD the
                        # missing pieces — not re-write everything from scratch
                        # (which is what caused the v14 fix-A-lose-B oscillation).
                        previous_diff_text = pipeline_state.get("diff") or ""
                        # Cap at 12KB to fit comfortably in any provider's
                        # context budget without truncating the prompt itself.
                        if len(previous_diff_text) > 12_000:
                            previous_diff_text = previous_diff_text[:12_000] + "\n[truncated]"
                        fp_repair_prompt = (
                            f"FEATURE_PRESENCE REPAIR (round {fp_round + 1}): "
                            f"Your previous diff is INCOMPLETE — the gate rejected "
                            f"it because the diff ADDITIONS lack substantive "
                            f"identifier-shaped tokens implementing the feature.\n\n"
                            f"Files still missing real implementation: "
                            f"{sample_unmatched}.\n\n"
                            f"Required spec tokens (derived from grounding terms / "
                            f"objective / file basenames): {required_tokens[:10]}.\n\n"
                            f"PREVIOUS DIFF (accumulated across earlier rounds — "
                            f"DO NOT undo any of this; only ADD what's missing):\n"
                            f"```diff\n{previous_diff_text}\n```\n\n"
                            f"You MUST extend the diff to ADD code that actually "
                            f"USES these symbols (function calls, references, real "
                            f"logic). Do NOT just declare new fields/variables and "
                            f"stop — write the loading / binding / handler logic "
                            f"that connects the spec to running behavior. Adding "
                            f"comments describing what the code 'will do' does NOT "
                            f"count — only added executable lines do.\n\n"
                            f"Output a unified diff covering the missing files. "
                            f"For files already in the previous diff, reproduce "
                            f"their existing changes AND add the missing logic on "
                            f"top. For new files, add fresh blocks."
                        )
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_CALL_REQUESTED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="feature_presence_check.repair",
                            message=(
                                f"Feature presence repair round {fp_round + 1} "
                                f"of {MAX_FP_REPAIR}: re-prompting codegen with "
                                f"missing-implementation feedback"
                            ),
                            payload={"unmatched": sample_unmatched},
                        )
                        # Cooldown to avoid LLM rate-limit thrash
                        time.sleep(10)
                        try:
                            repair_result = self._execute_develop_tool(
                                task=task,
                                actor_name=actor_name,
                                tool_name="codegen.generate_patch",
                                payload={
                                    "plan_json": {
                                        "objective": "Repair feature_presence rejection",
                                        "steps": [],
                                    },
                                    "context_files": pipeline_state.get(
                                        "context_files", {}
                                    ),
                                    "task_description": fp_repair_prompt,
                                },
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                approval_id=approval_id,
                                pipeline_state=pipeline_state,
                            )
                        except Exception as exc:
                            record_event(
                                self.db,
                                task_id=task.id,
                                event_type=EventType.TOOL_FAILED,
                                source=EventSource.ORCHESTRATOR,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                tool_name="feature_presence_check.repair",
                                message=f"Repair codegen call failed: {exc}",
                            )
                            break
                        repair_diff = str(
                            (repair_result or {}).get("diff", "")
                        ).strip()
                        if not repair_diff:
                            record_event(
                                self.db,
                                task_id=task.id,
                                event_type=EventType.TOOL_FAILED,
                                source=EventSource.ORCHESTRATOR,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                tool_name="feature_presence_check.repair",
                                message="Repair codegen produced no diff",
                            )
                            break
                        # Merge per-file with previous accumulated diff so we
                        # never lose changes from earlier rounds when this
                        # round's codegen only produced a partial diff (e.g.
                        # touched .kt but not the .xml that was already
                        # changed in the prior round).
                        previous_diff = pipeline_state.get("diff") or ""
                        merged_diff = merge_diffs_by_file(previous_diff, repair_diff)
                        pipeline_state["diff"] = merged_diff
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="feature_presence_check.repair",
                            message=(
                                f"Repair produced new diff "
                                f"({len(repair_diff)} chars); merged with "
                                f"prior {len(previous_diff)} chars -> "
                                f"{len(merged_diff)} chars total; re-evaluating"
                            ),
                            payload={
                                "round": fp_round + 1,
                                "previous_diff_size": len(previous_diff),
                                "new_diff_size": len(repair_diff),
                                "merged_diff_size": len(merged_diff),
                            },
                        )
                    # End repair loop. If still feature_absent, fail-close.
                    if presence is not None and presence.feature_absent:
                        self._fail_develop_pipeline(
                            task=task,
                            event_type=EventType.REVIEW_FAILED,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            message=(
                                f"Feature presence pre-gate rejected after "
                                f"{MAX_FP_REPAIR} repair attempt(s): "
                                f"{presence.reason}"
                            ),
                            payload={
                                "plan_id": plan.plan_id,
                                "feature_presence": presence.to_payload(),
                            },
                        )
                        return
                    pipeline_state["feature_presence_done"] = True
                    pipeline_state["feature_presence"] = (
                        presence.to_payload() if presence else {}
                    )
                    self._preserve_develop_pipeline_state(
                        task=task, pipeline_state=pipeline_state
                    )
            except Exception as exc:
                self._workspace_append_audit(
                    task,
                    "feature_presence_check.errored",
                    {"error": str(exc)[:400]},
                )

        # --- SymbolGraph ref-validity gate (post-codegen, pre-compile) ---
        # Catches the v9 P69-17 failure class: codegen adds a reference
        # (e.g. AndroidManifest @string/google_maps_api_key) without adding
        # the corresponding declaration (no <string name="google_maps_api_key">
        # in strings.xml). Generic across languages — uses the SymbolGraph
        # plug-in registry (currently Python via stdlib ast, Kotlin via
        # tree-sitter, XML via lxml + regex).
        if not pipeline_state.get("symbol_graph_done"):
            try:
                # Lazy-import the SymbolGraph framework + every registered
                # extractor. Each extractor module auto-registers itself.
                from app.services.symbol_graph import (  # noqa: F401
                    python_extractor,
                )
                from app.services.symbol_graph.pipeline_hook import (
                    check_changed_files,
                )
                from app.services.symbol_graph.registry import (
                    registered_extensions,
                )
                # Optional language plug-ins. Wrap in try/except so a
                # missing tree-sitter wheel doesn't crash the pipeline.
                try:
                    from app.services.symbol_graph import kotlin_extractor  # noqa: F401
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from app.services.symbol_graph import xml_extractor  # noqa: F401
                except Exception:  # noqa: BLE001
                    pass

                sg_source_root = _pipeline_source_path
                if sg_source_root is None or not sg_source_root.exists():
                    pipeline_state["symbol_graph_done"] = True
                    pipeline_state["symbol_graph_skipped"] = "no_source_tree"
                else:
                    # Enumerate repo files for the SymbolGraph build:
                    #   (a) any file whose extension has a registered
                    #       extractor (Python ast / Kotlin tree-sitter /
                    #       XML lxml / ...).
                    #   (b) any file under res/<KIND>[-qualifier]/ whose
                    #       parent directory matches a known file-based
                    #       resource kind (drawable, layout, menu,
                    #       navigation, ...). These contribute Decls by
                    #       file existence in pipeline_hook even when no
                    #       extractor is registered for their extension
                    #       (e.g. PNG / JPG / WebP drawables).
                    sg_exts = set(registered_extensions())
                    # Match the kinds list in pipeline_hook to keep the
                    # two in sync. (Importing the constant directly would
                    # be cleaner but pipeline_hook's import auto-runs
                    # python_extractor.register; we already imported it.)
                    _SG_FILE_BASED_KINDS = {
                        "drawable", "layout", "menu", "navigation",
                        "anim", "animator", "color", "font",
                        "interpolator", "mipmap", "raw", "transition",
                        "xml",
                    }
                    sg_all_files: list[str] = []
                    for fp in sg_source_root.rglob("*"):
                        if not fp.is_file():
                            continue
                        ext = fp.suffix.lstrip(".").lower()
                        try:
                            rel = str(fp.relative_to(sg_source_root)).replace("\\", "/")
                        except ValueError:
                            continue
                        if ext in sg_exts:
                            sg_all_files.append(rel)
                            continue
                        # File-based-resource path check. Any parent
                        # segment named res/<KIND> or res/<KIND>-qualifier
                        # qualifies the file for Decl emission.
                        parts = rel.split("/")
                        if "res" in parts:
                            ri = parts.index("res")
                            if ri + 2 < len(parts):  # res/<KIND>/<file>
                                kind = parts[ri + 1].split("-", 1)[0]
                                if kind in _SG_FILE_BASED_KINDS:
                                    sg_all_files.append(rel)

                    sg_changed_files = tuple(
                        str(p).replace("\\", "/")
                        for p in (pipeline_state.get("files_changed") or [])
                    )
                    if sg_changed_files and sg_all_files:
                        sg_report = check_changed_files(
                            repo_root=sg_source_root,
                            all_repo_files=tuple(sg_all_files),
                            changed_files=sg_changed_files,
                        )
                        sg_payload = sg_report.to_payload()
                        pipeline_state["symbol_graph"] = sg_payload
                        record_event(
                            self.db,
                            task_id=task.id,
                            event_type=(
                                EventType.TOOL_FAILED
                                if not sg_report.passed
                                else EventType.TOOL_SUCCEEDED
                            ),
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="symbol_graph.ref_validity",
                            message=(
                                f"SymbolGraph ref-validity: "
                                f"{len(sg_report.violations)} violation(s), "
                                f"refs_checked={sg_report.refs_checked}, "
                                f"files_covered={sg_report.files_covered}, "
                                f"files_skipped={sg_report.files_skipped}"
                            ),
                            payload=sg_payload,
                        )
                        if not sg_report.passed:
                            self._fail_develop_pipeline(
                                task=task,
                                event_type=EventType.REVIEW_FAILED,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                message=(
                                    f"SymbolGraph rejected diff: "
                                    f"{len(sg_report.violations)} unresolved "
                                    f"reference(s) in changed files."
                                ),
                                payload={
                                    "plan_id": plan.plan_id,
                                    "symbol_graph": sg_payload,
                                },
                            )
                            return
                    pipeline_state["symbol_graph_done"] = True
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            except Exception as exc:  # noqa: BLE001
                # SymbolGraph is a best-effort gate. Errors must never
                # block the pipeline — only true unresolved refs do.
                self._workspace_append_audit(
                    task,
                    "symbol_graph.errored",
                    {"error": str(exc)[:400]},
                )
                pipeline_state["symbol_graph_done"] = True
                pipeline_state["symbol_graph_skipped"] = "errored"

        # --- R1 semantic_review: LLM completeness + risk reviewer ---
        # Runs after compile + SymbolGraph pass. Calls a strict reviewer
        # LLM that scores completeness (0-100) against the original
        # spec and lists structured findings (orphan UI, hardcoded stubs,
        # missing routes, unbound fields, race conditions, ...).
        # Findings are anti-hallucination grounded: every claim must
        # cite a verbatim diff substring; ungrounded findings are
        # dropped before the verdict.
        # Threshold: completeness_pct >= 80 AND zero high-severity
        # findings to pass. On fail, feeds findings into A-style
        # repair loop (codegen.generate_patch with structured repair
        # prompt + accumulated diff).
        if not pipeline_state.get("semantic_review_done"):
            try:
                from app.services.semantic_review import (
                    SemanticReviewError,
                    evaluate_semantic_review,
                )
                from app.services.feature_presence_check import (
                    merge_diffs_by_file as _merge_diffs_by_file,
                )

                _SR_PASS_THRESHOLD = 80
                _SR_MAX_REPAIR = 2
                _sr_translation = (
                    task.translation_json
                    if isinstance(task.translation_json, dict) else {}
                )
                # Build a rich spec from every available source. Previously
                # only used plan.objective (often generic boilerplate like
                # "Implement the Jira issue by generating code...") plus
                # the bare ticket key, which produced reviews like "no spec
                # provided". The full ticket body lives in translation.
                # search_queries[0] (the planner's expanded prompt) and
                # grounding_terms are also signal-rich. Cap the joined
                # text to keep the LLM prompt within budget.
                _sr_search_qs = _sr_translation.get("search_queries") or []
                _sr_search_q0 = (
                    str(_sr_search_qs[0])[:3000] if _sr_search_qs else ""
                )
                _sr_grounding = _sr_translation.get("grounding_terms") or []
                _sr_grounding_str = (
                    "Spec keywords: " + ", ".join(str(g) for g in _sr_grounding[:20])
                    if _sr_grounding else ""
                )
                _sr_spec_parts = [
                    str(getattr(plan, "objective", "") or ""),
                    _sr_search_q0,
                    str(_sr_translation.get("normalized_request") or ""),
                    _sr_grounding_str,
                    str(task.request_text or ""),
                ]
                # Dedup adjacent duplicates (common when the user request
                # is just the ticket key and translation echoes it).
                _seen: set[str] = set()
                _dedup: list[str] = []
                for part in _sr_spec_parts:
                    norm = part.strip()
                    if norm and norm not in _seen:
                        _seen.add(norm)
                        _dedup.append(norm)
                _sr_spec_text = "\n\n".join(_dedup)

                # Read post-edit content of the changed files for the
                # reviewer's context.
                _sr_sandbox_dir = self._develop_sandbox_dir(task)
                sr_report = None
                for sr_round in range(_SR_MAX_REPAIR + 1):
                    diff_for_review = pipeline_state.get("diff") or ""
                    _sr_file_contents: dict[str, str] = {}
                    if _sr_sandbox_dir.exists():
                        for rel in (pipeline_state.get("files_changed") or []):
                            try:
                                full = _sr_sandbox_dir / rel
                                if full.is_file():
                                    _sr_file_contents[rel] = full.read_text(
                                        encoding="utf-8", errors="replace"
                                    )[:12_000]
                            except Exception:
                                pass

                    try:
                        sr_report = evaluate_semantic_review(
                            spec_text=_sr_spec_text,
                            diff=diff_for_review,
                            file_contents=_sr_file_contents,
                            settings=self.tool_gateway.settings,
                            pass_threshold=_SR_PASS_THRESHOLD,
                            # DeepSeek has working credit; Anthropic key is
                            # often exhausted in this dogfood env. Both
                            # providers wrap the same Anthropic-compat
                            # /v1/messages schema.
                            provider="deepseek",
                            timeout_seconds=90.0,
                        )
                    except SemanticReviewError as exc:
                        # LLM call failure is non-blocking — log + skip
                        # the gate (do NOT fail the pipeline on infra
                        # errors of an advisory gate).
                        record_event(
                            self.db, task_id=task.id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="semantic_review.evaluate",
                            message=f"Semantic review errored (skipped): {exc}",
                            payload={"error": str(exc)[:500]},
                        )
                        pipeline_state["semantic_review_skipped"] = "errored"
                        sr_report = None
                        break

                    sr_event = record_event(
                        self.db, task_id=task.id,
                        event_type=(
                            EventType.TOOL_SUCCEEDED if sr_report.passed
                            else EventType.REVIEW_FAILED
                        ),
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="semantic_review.evaluate",
                        message=(
                            f"semantic_review (round {sr_round}): "
                            f"completeness={sr_report.completeness_pct}%, "
                            f"high={sr_report.high_severity_count()}, "
                            f"findings={len(sr_report.findings)}, "
                            f"dropped_no_evidence={sr_report.findings_dropped_no_evidence}"
                        ),
                        payload={
                            **sr_report.to_payload(),
                            "sr_repair_round": sr_round,
                        },
                    )
                    # R3a: persist findings to AgentMemory so future tasks
                    # with similar code paths see this kind of bug as
                    # planner context. Only fires when the report has
                    # actionable (high/medium) findings.
                    if not sr_report.passed and sr_report.findings:
                        try:
                            from app.services.memory import MemoryService
                            _mem = MemoryService(self.db, self.tool_gateway.settings)
                            n_recorded = _mem.record_semantic_review_findings(
                                task=task,
                                review_payload=sr_report.to_payload(),
                                provenance_event_id=getattr(sr_event, "id", None),
                            )
                            if n_recorded:
                                logger.info(
                                    "R3a: persisted %d semantic_review finding(s) to memory",
                                    n_recorded,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "R3a memory persist failed: %s", exc,
                            )
                    if sr_report.passed:
                        break
                    if sr_round >= _SR_MAX_REPAIR:
                        break

                    # Build repair prompt from grounded findings + previous diff
                    findings_lines = sr_report.repair_prompt_lines()
                    previous_diff_text = pipeline_state.get("diff") or ""
                    if len(previous_diff_text) > 12_000:
                        previous_diff_text = previous_diff_text[:12_000] + "\n[truncated]"
                    sr_repair_prompt = (
                        f"SEMANTIC_REVIEW REPAIR (round {sr_round + 1}): "
                        f"the reviewer scored completeness "
                        f"{sr_report.completeness_pct}% (need >= "
                        f"{_SR_PASS_THRESHOLD}%) and flagged "
                        f"{sr_report.high_severity_count()} HIGH and "
                        f"{len(sr_report.findings) - sr_report.high_severity_count()}"
                        f" non-high finding(s). Address each:\n\n"
                        + "\n".join(findings_lines)
                        + "\n\nPREVIOUS DIFF (accumulated — DO NOT undo):\n"
                        f"```diff\n{previous_diff_text}\n```\n\n"
                        "Output a unified diff. For files already in the "
                        "previous diff, reproduce existing changes AND add "
                        "the missing logic on top. Comments/declarations "
                        "alone do not satisfy the findings — only added "
                        "executable lines do."
                    )
                    record_event(
                        self.db, task_id=task.id,
                        event_type=EventType.TOOL_CALL_REQUESTED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="semantic_review.repair",
                        message=(
                            f"Semantic review repair round {sr_round + 1} "
                            f"of {_SR_MAX_REPAIR}: re-prompting codegen with "
                            f"{len(sr_report.findings)} grounded finding(s)"
                        ),
                    )
                    time.sleep(10)  # Rate-limit cooldown
                    # R3b: context expansion — for every file mentioned by
                    # a grounded finding, ensure the codegen sees the
                    # POST-EDIT full content (not just the diff). This
                    # gives the LLM the surrounding code shape so its
                    # repair patch fits the actual structure rather than
                    # guessing API surfaces.
                    sr_expanded_context = dict(
                        pipeline_state.get("context_files", {}) or {}
                    )
                    sr_finding_files: set[str] = set()
                    for f in sr_report.findings:
                        fp = (f.file or "").strip().replace("\\", "/")
                        if fp:
                            sr_finding_files.add(fp)
                    sr_added_count = 0
                    for fp in sr_finding_files:
                        if fp in sr_expanded_context:
                            continue  # already in context
                        try:
                            full = _sr_sandbox_dir / fp
                            if full.is_file():
                                content = full.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                                if len(content) > 30_000:
                                    content = content[:30_000] + "\n[truncated]"
                                sr_expanded_context[fp] = content
                                sr_added_count += 1
                        except Exception:
                            pass
                    if sr_added_count:
                        record_event(
                            self.db, task_id=task.id,
                            event_type=EventType.TOOL_SUCCEEDED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="semantic_review.context_expand",
                            message=(
                                f"R3b: expanded codegen context with "
                                f"{sr_added_count} finding-referenced "
                                f"file(s) (full post-edit content)"
                            ),
                            payload={
                                "files_added": sorted(sr_finding_files),
                            },
                        )
                    try:
                        sr_repair_result = self._execute_develop_tool(
                            task=task,
                            actor_name=actor_name,
                            tool_name="codegen.generate_patch",
                            payload={
                                "plan_json": {
                                    "objective": "Address semantic_review findings",
                                    "steps": [],
                                },
                                "context_files": sr_expanded_context,
                                "task_description": sr_repair_prompt,
                            },
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            approval_id=approval_id,
                            pipeline_state=pipeline_state,
                        )
                    except Exception as exc:
                        record_event(
                            self.db, task_id=task.id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="semantic_review.repair",
                            message=f"Repair codegen call failed: {exc}",
                        )
                        break
                    sr_repair_diff = str(
                        (sr_repair_result or {}).get("diff", "")
                    ).strip()
                    if not sr_repair_diff:
                        record_event(
                            self.db, task_id=task.id,
                            event_type=EventType.TOOL_FAILED,
                            source=EventSource.ORCHESTRATOR,
                            stage=WorkflowStage.REVIEW,
                            role=RoleName.REVIEWER,
                            tool_name="semantic_review.repair",
                            message="Repair codegen produced no diff",
                        )
                        break
                    sr_merged = _merge_diffs_by_file(
                        previous_diff_text, sr_repair_diff,
                    )
                    pipeline_state["diff"] = sr_merged
                    record_event(
                        self.db, task_id=task.id,
                        event_type=EventType.TOOL_SUCCEEDED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="semantic_review.repair",
                        message=(
                            f"Repair produced {len(sr_repair_diff)} chars; "
                            f"merged with prior {len(previous_diff_text)} -> "
                            f"{len(sr_merged)} total; re-evaluating"
                        ),
                    )

                # End of repair loop. semantic_review is ADVISORY at
                # the moment — even if not-passed it does NOT fail-close
                # the pipeline (still sends to AWAITING_APPROVAL with
                # findings attached). The reviewer sees the report and
                # decides. To make it strict, set fail_closed=True on a
                # later iteration.
                if sr_report is not None:
                    pipeline_state["semantic_review"] = sr_report.to_payload()
                pipeline_state["semantic_review_done"] = True
                self._preserve_develop_pipeline_state(
                    task=task, pipeline_state=pipeline_state,
                )
            except Exception as exc:  # noqa: BLE001
                self._workspace_append_audit(
                    task, "semantic_review.errored",
                    {"error": str(exc)[:400]},
                )
                pipeline_state["semantic_review_done"] = True
                pipeline_state["semantic_review_skipped"] = "errored"

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
            self._write_task_checkpoint(
                task,
                stage="review_post",
                output_payload=self._task_checkpoint_payload(
                    task,
                    pipeline_state=self._load_develop_pipeline_state(task),
                    review_result=review_result,
                    plan_json=task.plan_json,
                ),
                sandbox_snapshot_id=self._build_develop_sandbox(task).snapshot_id(),
            )

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
                    payload={"error": str(exc)[:5000]},
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

        # --- T-041-04: evidence chain validation before approval/writeback ---
        # T-PIPELINE-REPAIR-CAP: skip the chain when the task already
        # transitioned to approval after compile-repair cap exhaust — the
        # diff is partial and the reviewer is already in the loop.
        if not pipeline_state.get("evidence_chain_validated"):
            if bool(pipeline_state.get("compile_repair_cap_exceeded")):
                pipeline_state["evidence_chain"] = {
                    "closed": True,
                    "skipped": True,
                    "summary": (
                        "Evidence chain skipped: compile-repair cap exceeded "
                        "and the task is already awaiting human approval."
                    ),
                    "findings": [],
                    "diagnostic": {"reason": "compile_repair_cap_exceeded"},
                }
                pipeline_state["evidence_chain_validated"] = True
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                return
            if bool(getattr(self.tool_gateway.settings, "evidence_chain_gate_enabled", True)):
                workspace = self._task_workspace(task)
                try:
                    citations = workspace.list_evidence()
                except Exception:  # noqa: BLE001
                    citations = []
                chain_attestation = self._evidence_chain_attestation(pipeline_state)
                evidence_chain_report = check_evidence_chain(
                    workspace=workspace,
                    diff=diff,
                    plan=plan,
                    claims=self._extract_evidence_chain_claims(
                        pipeline_state=pipeline_state,
                        codegen_result=codegen_result,
                        review_result=review_result,
                    ),
                    citations=citations,
                    attestation=chain_attestation,
                    settings=self.tool_gateway.settings,
                )
                pipeline_state["evidence_chain"] = evidence_chain_report.to_payload()
                pipeline_state["evidence_chain_validated"] = True

                if not evidence_chain_report.closed:
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.REVIEW_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="evidence_chain.broken",
                        message=evidence_chain_report.summary,
                        payload=evidence_chain_report.to_payload(),
                    )
                    self._fail_develop_pipeline(
                        task=task,
                        event_type=EventType.REVIEW_FAILED,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        message=self._evidence_chain_block_message(evidence_chain_report),
                        payload={
                            "evidence_chain": evidence_chain_report.to_payload(),
                            "plan_id": plan.plan_id,
                        },
                    )
                    return

                warnings = [
                    finding
                    for finding in evidence_chain_report.findings
                    if finding.severity == "warn"
                ]
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_SUCCEEDED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name=(
                        "evidence_chain.warning"
                        if warnings
                        else "evidence_chain.check"
                    ),
                    message=evidence_chain_report.summary,
                    payload=evidence_chain_report.to_payload(),
                )
            else:
                pipeline_state["evidence_chain"] = {
                    "closed": True,
                    "skipped": True,
                    "summary": "Evidence chain gate disabled by configuration.",
                    "findings": [],
                    "diagnostic": {},
                }
                pipeline_state["evidence_chain_validated"] = True
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
                evidence_chain=pipeline_state.get("evidence_chain"),
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
                "evidence_chain": pipeline_state.get("evidence_chain"),
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

        parts.append(f"## {issue_key} {'Development completed' if zh else 'Development Complete'}\n")

        # --- Change summary section ---
        raw_file_summaries = pipeline_state.get("file_summaries")
        file_summaries: list[str] = []
        if isinstance(raw_file_summaries, list):
            for f in raw_file_summaries:
                if isinstance(f, dict) and f.get("path") and f.get("summary"):
                    file_summaries.append(f"- **{f['path']}**: {f['summary']}")

        files = pipeline_state.get("files_changed")
        if isinstance(files, list) and files:
            parts.append(f"### {'Change summary' if zh else 'Change Summary'}\n")
            parts.append(
                f"{'Modified in this run:' if zh else 'Modified'} **{len(files)}** "
                f"{' file(s)' if zh else 'file(s)'}{'：' if zh else ':'}"
            )
            if file_summaries:
                parts.extend(file_summaries)
            else:
                for file_path in files[:10]:
                    parts.append(f"- `{file_path}`")
            parts.append("")

        diff = str(pipeline_state.get("diff") or "")
        if diff:
            parts.append(f"### {'Code changes' if zh else 'Code Changes'}\n")
            parts.append(f"```diff\n{diff}\n```")
            parts.append("")

        parts.append(f"### {'Pipeline execution' if zh else 'Pipeline'}\n")
        parts.append(f"- {'Codegen: ' if zh else 'Code generation: '}{pipeline_state.get('codegen_provider', 'unknown')}")
        method = str(pipeline_state.get("patch_method") or "")
        if method:
            parts.append(f"- {'Patch apply method: ' if zh else 'Patch applied via: '}{method}")
        if pipeline_state.get("test_skipped"):
            parts.append(f"- {'Tests: skipped (no test config)' if zh else 'Tests: skipped (no test config)'}")
        else:
            parts.append(f"- {'Tests: passed' if zh else 'Tests: passed'}")
        parts.append(f"- {'Review: ' if zh else 'Review: '}{pipeline_state.get('review_verdict', 'N/A')}")

        jira_writeback = pipeline_state.get("jira_writeback")
        if isinstance(jira_writeback, dict) and jira_writeback.get("transition"):
            parts.append(f"- {'Jira: status transitioned' if zh else 'Jira: transitioned'}")
        elif isinstance(jira_writeback, dict) and jira_writeback.get("comment"):
            parts.append(f"- {'Jira: comment added' if zh else 'Jira: commented'}")
        else:
            parts.append(f"- {'Jira: no issue key found, skipping writeback' if zh else 'Jira: no issue key found, writeback skipped'}")

        completeness = pipeline_state.get("completeness_check")
        if isinstance(completeness, dict):
            if completeness.get("complete"):
                parts.append(f"\n### {'Completeness Check' if zh else 'Completeness Check'}\n")
                parts.append(f"{'All target keywords cleared。' if zh else 'All target keywords removed.'}")
            else:
                remaining = completeness.get("remaining_files", 0)
                hits = completeness.get("remaining_hits", 0)
                parts.append(f"\n### {'Completeness Check' if zh else 'Completeness Check'}\n")
                parts.append(
                    f"{'Still has' if zh else 'Still '}"
                    f"**{remaining}** {' file(s) contain target keyword(s)' if zh else ' file(s) contain target keywords'}"
                    f"{' (total ' if zh else ' ('}{hits} {' place(s))' if zh else ' hits)'}："
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
            failure_payload = {"error": str(exc), "approval_id": approval_id}
            if failed_event_type == EventType.TOOL_TIMED_OUT:
                failure_payload.update(
                    {
                        "reason": "external_api_timeout",
                        "provider_name": tool_name.split(".", 1)[0],
                    }
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
                payload=failure_payload,
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
        source_path = self._resolve_knowledge_source_path(task)
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
        settings = self.tool_gateway.settings
        external_root = getattr(settings, "sandbox_external_root", None)
        if external_root:
            root_path = Path(external_root)
            if not root_path.is_absolute():
                raise ValueError(
                    "sandbox_external_root must be an absolute path when set "
                    f"(got {external_root!r})."
                )
            return root_path / task.id
        base_dir = Path(str(getattr(settings, "sandbox_base_dir", "data/sandboxes")))
        return base_dir / task.id

    # ----- Compile repair loop --------------------------------------------- #

    def _run_compile_repair_loop(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        pipeline_state: dict[str, object],
        approval_id: str | None,
    ) -> str:
        """Run compile_gate with up to N repair rounds.

        Returns one of:
          - ``"passed"``           — gate passed (possibly after repair); pipeline continues.
          - ``"approval_requested"`` — cap exceeded, task parked in AWAITING_APPROVAL.
          - ``"failed"``           — cap exceeded, task FAILED (legacy fail-fast).
          - ``"errored"``          — gate itself errored or skipped; pipeline continues.
        """
        from app.services.compile_gate import run_compile_gate
        from app.services.verification_profile import resolve_verification_profile, run_compile_check

        sandbox_dir = self._develop_sandbox_dir(task)
        changed = list(pipeline_state.get("files_changed") or [])

        settings_obj = self.tool_gateway.settings
        profile_compile_enabled = (
            bool(pipeline_state.get("verification_compile_pending"))
            and bool(getattr(settings_obj, "verification_profile_enabled", True))
        )
        allowed_paths = self._verification_allowed_paths(plan)
        profile = None
        if profile_compile_enabled:
            if not allowed_paths:
                self._verification_skipped_result(
                    task=task,
                    pipeline_state=pipeline_state,
                    reason="empty_allowed_paths",
                    message="Verification skipped: plan has no allowed files to validate.",
                    payload={"plan_id": plan.plan_id},
                )
                pipeline_state["compile_gate_done"] = True
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                return "errored"
            profile = resolve_verification_profile(sandbox_dir, has_tests_yaml=False)
            pipeline_state["verification_profile"] = profile.to_dict()
            pipeline_state["verification_allowed_paths"] = sorted(allowed_paths)
            if profile.repo_type == "unknown" or not profile.compile_command:
                self._verification_skipped_result(
                    task=task,
                    pipeline_state=pipeline_state,
                    reason="unknown_repo_type",
                    message="Verification skipped: repository type could not be detected.",
                    payload={"plan_id": plan.plan_id, "profile": profile.to_dict()},
                )
                pipeline_state["compile_gate_done"] = True
                self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                return "errored"

        default_max_rounds = int(getattr(settings_obj, "codegen_max_repair_rounds", 3))
        if profile_compile_enabled:
            default_max_rounds = int(
                getattr(settings_obj, "verification_max_repair_rounds", default_max_rounds)
            )
        max_rounds = max(0, default_max_rounds)
        files_per_round = max(
            1, int(getattr(settings_obj, "codegen_repair_files_per_round", 5))
        )
        round_timeout = float(
            getattr(settings_obj, "codegen_repair_round_timeout_seconds", 180.0)
        )
        # Stage 25 contract: when verification_profile is the active compile mode,
        # cap-exceeded means task FAILED (no silent awaiting_approval). Legacy
        # codegen-repair path retains the old "send to approval" semantics.
        if profile_compile_enabled:
            fail_to_approval = bool(
                getattr(settings_obj, "verification_compile_fail_to_approval", False)
            )
        else:
            fail_to_approval = bool(
                getattr(settings_obj, "codegen_repair_cap_exceeded_to_approval", True)
            )

        rounds_summary: list[dict] = []
        compile_passed = False
        compile_result = None
        compile_errored = False
        compile_unexpected_exception = False

        if not (sandbox_dir.exists() and changed):
            # Nothing to check — preserve legacy "skip silently" behaviour.
            pipeline_state["compile_gate_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            return "errored"

        for round_index in range(max_rounds + 1):
            if compile_passed:
                break
            try:
                if profile_compile_enabled and profile is not None:
                    timeout_seconds = int(
                        getattr(
                            settings_obj,
                            "verification_compile_timeout_seconds",
                            profile.timeout_seconds,
                        )
                    )
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.TOOL_CALL_REQUESTED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="verification.compile",
                        message=(
                            f"Running compile-only verification for {profile.repo_type} "
                            f"(round {round_index + 1})."
                        ),
                        payload={
                            "repo_type": profile.repo_type,
                            "command": profile.compile_command,
                            "timeout_seconds": timeout_seconds,
                        },
                    )
                    compile_result = run_compile_check(
                        sandbox=self._build_develop_sandbox(task),
                        profile=profile,
                        timeout_seconds=timeout_seconds,
                        max_output_bytes=int(
                            getattr(settings_obj, "sandbox_max_output_bytes", 65536)
                        ),
                    )
                else:
                    compile_result = run_compile_gate(
                        sandbox_dir=sandbox_dir,
                        changed_files=changed,
                    )
            except Exception as exc:
                import traceback as _tb
                tb_text = _tb.format_exc()
                compile_result = None
                compile_errored = True
                compile_unexpected_exception = True  # used by caller to fail-close
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="compile_gate.check",
                    message=f"Compile gate errored (unexpected): {exc}",
                    payload={"error": str(exc), "traceback": tb_text[:4000], "type": type(exc).__name__},
                )
                pipeline_state["compile_gate_unexpected_exception"] = True
                pipeline_state["compile_gate_traceback"] = tb_text[:4000]
                break

            if compile_result is None:
                break

            compile_errors = list(getattr(compile_result, "errors", []) or [])
            if profile_compile_enabled and allowed_paths:
                repairable_errors = [
                    error for error in compile_errors
                    if self._compile_error_in_allowed_paths(error, allowed_paths)
                ]
                if compile_errors and not repairable_errors:
                    self._verification_skipped_result(
                        task=task,
                        pipeline_state=pipeline_state,
                        reason="compile_errors_outside_allowed_paths",
                        message=(
                            "Verification skipped: compiler errors were outside "
                            "the plan's allowed file set."
                        ),
                        payload={
                            "plan_id": plan.plan_id,
                            "allowed_paths": sorted(allowed_paths),
                            "errors": compile_errors,
                        },
                    )
                    pipeline_state["compile_gate_done"] = True
                    self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
                    return "errored"
                compile_errors = repairable_errors

            pipeline_state["compile_gate"] = {
                "passed": compile_result.passed,
                "errors": compile_errors,
            }
            if profile_compile_enabled and profile is not None:
                pipeline_state["compile_gate"].update(
                    {
                        "verified_by": "compile",
                        "repo_type": profile.repo_type,
                        "command": profile.compile_command,
                        "timed_out": bool(getattr(compile_result, "timed_out", False)),
                        "duration_ms": int(getattr(compile_result, "duration_ms", 0)),
                    }
                )
            self._workspace_write_attempt_compile(
                task,
                pipeline_state,
                result_dict=pipeline_state["compile_gate"],
            )

            if compile_result.passed:
                if profile_compile_enabled:
                    pipeline_state.pop("verification_compile_pending", None)
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
                compile_passed = True
                break

            self._record_compile_failed_event(
                task=task,
                compile_errors=compile_errors,
                compile_result=compile_result,
                profile_repo_type=profile.repo_type if profile_compile_enabled and profile is not None else None,
                allowed_paths=allowed_paths if profile_compile_enabled else None,
            )

            if round_index == max_rounds:
                rounds_summary.append(
                    {
                        "round": round_index + 1,
                        "files_attempted": [],
                        "files_repaired": [],
                        "duration_seconds": 0.0,
                        "compile_gate_after": {
                            "passed": False,
                            "error_count": len(compile_errors),
                        },
                        "note": "no repair budget remaining",
                    }
                )
                self._workspace_append_audit(
                    task,
                    "compile_repair.cap_reached",
                    {
                        "rounds_attempted": max_rounds,
                        "residual_error_count": len(compile_errors),
                    },
                )
                break

            files_queued = [
                str(e.get("file") or "")
                for e in compile_errors[:files_per_round]
                if e.get("file")
            ]
            round_label = round_index + 1
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.TOOL_CALL_REQUESTED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="compile_repair.round_started",
                message=(
                    f"Compile repair round {round_label} starting "
                    f"({len(files_queued)} file(s) queued)."
                ),
                payload={
                    "round_index": round_label,
                    "files_queued": files_queued,
                },
            )
            self._workspace_append_audit(
                task,
                "compile_repair.round_started",
                {"round_index": round_label, "files_queued": files_queued},
            )

            round_started = time.monotonic()
            timed_out = False
            repaired = False
            files_touched: list[str] = []
            try:
                repaired, files_touched = self._attempt_compile_repair(
                    task=task,
                    actor_name=actor_name,
                    plan=plan,
                    compile_errors=compile_errors,
                    sandbox_dir=sandbox_dir,
                    pipeline_state=pipeline_state,
                    approval_id=approval_id,
                    timeout_seconds=round_timeout,
                    files_per_round=files_per_round,
                    allowed_paths=allowed_paths if profile_compile_enabled else None,
                )
            except RepairRoundTimeout as timeout_exc:
                timed_out = True
                repaired = False
                files_touched = []
                record_event(
                    self.db,
                    task_id=task.id,
                    event_type=EventType.TOOL_FAILED,
                    source=EventSource.ORCHESTRATOR,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    tool_name="compile_repair.round_timed_out",
                    message=f"Compile repair round {round_label} timed out: {timeout_exc}",
                    payload={
                        "round_index": round_label,
                        "timeout_seconds": round_timeout,
                    },
                )
                self._workspace_append_audit(
                    task,
                    "compile_repair.round_timed_out",
                    {"round_index": round_label, "timeout_seconds": round_timeout},
                )

            duration = round(time.monotonic() - round_started, 2)
            round_record = {
                "round": round_label,
                "files_attempted": files_queued,
                "files_repaired": list(files_touched) if repaired else [],
                "duration_seconds": duration,
                "timed_out": timed_out,
            }
            rounds_summary.append(round_record)
            record_event(
                self.db,
                task_id=task.id,
                event_type=(
                    EventType.TOOL_SUCCEEDED if repaired else EventType.TOOL_FAILED
                ),
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.REVIEW,
                role=RoleName.REVIEWER,
                tool_name="compile_repair.round_completed",
                message=(
                    f"Compile repair round {round_label} completed: "
                    f"{len(round_record['files_repaired'])} repaired, "
                    f"timed_out={timed_out}, duration={duration}s."
                ),
                payload=round_record,
            )
            self._workspace_append_audit(
                task,
                "compile_repair.round_completed",
                round_record,
            )

            if repaired:
                changed = list({*changed, *files_touched})
            # Loop continues — next iteration re-runs compile_gate.

        if compile_passed:
            pipeline_state["compile_gate_done"] = True
            pipeline_state["compile_repair_rounds"] = rounds_summary
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            return "passed"

        if compile_result is None or compile_errored:
            pipeline_state["compile_gate_done"] = True
            self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
            # Stage X.4: distinguish unexpected exception (fail-closed) from
            # expected skip (continue). Unexpected = bug in our compile-check
            # code; cannot trust downstream review gates because they only
            # see the diff, not post-apply file state. Block the pipeline.
            if compile_unexpected_exception:
                self._fail_develop_pipeline(
                    task=task,
                    event_type=EventType.REVIEW_FAILED,
                    stage=WorkflowStage.REVIEW,
                    role=RoleName.REVIEWER,
                    message=(
                        "Compile gate errored unexpectedly (likely a bug in "
                        "verification_profile/run_compile_check). Pipeline "
                        "blocked — cannot trust downstream gates without "
                        "structural validation."
                    ),
                    payload={
                        "plan_id": plan.plan_id,
                        "compile_traceback": pipeline_state.get("compile_gate_traceback", ""),
                    },
                )
                return "failed"
            return "errored"

        residual_errors = []
        compile_gate_payload = pipeline_state.get("compile_gate")
        if isinstance(compile_gate_payload, dict):
            raw_errors = compile_gate_payload.get("errors")
            if isinstance(raw_errors, list):
                residual_errors = raw_errors

        pipeline_state["compile_repair_rounds"] = rounds_summary
        pipeline_state["compile_repair_cap_exceeded"] = True
        self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.REVIEW_FAILED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            tool_name="compile_repair.cap_exceeded",
            message=(
                f"Compile gate still failing after {max_rounds} "
                f"repair round(s): {compile_result.summary()}"
            ),
            payload={
                "rounds_attempted": max_rounds,
                "rounds_summary": rounds_summary,
                "residual_compile_errors": residual_errors,
            },
        )
        if fail_to_approval:
            self._request_compile_repair_approval(
                task=task,
                plan=plan,
                pipeline_state=pipeline_state,
                rounds_summary=rounds_summary,
                residual_errors=residual_errors,
                sandbox_dir=sandbox_dir,
            )
            return "approval_requested"

        self._fail_develop_pipeline(
            task=task,
            event_type=EventType.REVIEW_FAILED,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message=(
                f"compile_gate_exhausted: {compile_result.summary()}"
            ),
            payload={
                "reason": "compile_gate_exhausted",
                "compile_gate": pipeline_state.get("compile_gate"),
                "rounds_summary": rounds_summary,
                "rounds_attempted": max_rounds,
            },
        )
        return "failed"

    @staticmethod
    def _compile_error_in_allowed_paths(error: dict, allowed_paths: set[str]) -> bool:
        file_path = str(error.get("file") or "").strip().replace("\\", "/")
        if not file_path:
            return True
        return file_path in allowed_paths

    @staticmethod
    def _build_related_files_section(
        rel_path: str,
        error_msg: str,
        allowed_paths: set[str] | None,
        sandbox_dir: Path,
    ) -> str:
        related_files_section = ""
        if "unresolved reference" in str(error_msg).lower() and allowed_paths:
            # When compile error is "Unresolved reference X", per-file repair
            # cannot resolve it without seeing where X is (or should be) declared.
            # Inject the current sandbox state of other in-scope files so the
            # LLM can pick option (b) UPDATE-to-current-name from L4f.
            related_chunks: list[str] = []
            related_total_bytes = 0
            MAX_FILES = 5
            MAX_BYTES_PER_FILE = 3000
            MAX_TOTAL_BYTES = 12000
            for related_rel in sorted(p for p in allowed_paths if p != rel_path):
                if len(related_chunks) >= MAX_FILES:
                    break
                if related_total_bytes >= MAX_TOTAL_BYTES:
                    break
                related_full = (
                    sandbox_dir / related_rel.replace("/", "\\")
                    if "\\" in str(sandbox_dir)
                    else sandbox_dir / related_rel
                )
                if not related_full.exists() or not related_full.is_file():
                    continue
                try:
                    related_content = related_full.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                if not related_content.strip():
                    continue
                truncated = related_content[:MAX_BYTES_PER_FILE]
                note = (
                    ""
                    if len(related_content) <= MAX_BYTES_PER_FILE
                    else f"\n# ... (truncated, original was {len(related_content)} chars)"
                )
                chunk = (
                    f"=== RELATED {related_rel} (current sandbox state) ===\n"
                    f"{truncated}{note}\n"
                    f"=== END RELATED {related_rel} ===\n\n"
                )
                related_chunks.append(chunk)
                related_total_bytes += len(chunk)
            if related_chunks:
                related_files_section = (
                    "\nRELATED IN-SCOPE FILES (current sandbox state — use these "
                    "to look up symbol declarations referenced by the broken file. "
                    "Per L4f, you may NOT invent new names; your repair must use a "
                    "name that already exists in either the broken file's original "
                    "version or in one of these related files.):\n"
                    + "".join(related_chunks)
                )
        return related_files_section

    def _record_compile_failed_event(
        self,
        *,
        task: Task,
        compile_errors: list[dict],
        compile_result: object,
        profile_repo_type: str | None,
        allowed_paths: set[str] | None,
    ) -> None:
        failed_files = sorted(
            {str(error.get("file") or "") for error in compile_errors if error.get("file")}
        )
        output = str(getattr(compile_result, "output", "") or "")
        excerpt = " ".join(output.strip().split())[:2000]
        if not excerpt and compile_errors:
            excerpt = "; ".join(str(error.get("error") or "") for error in compile_errors[:5])[:2000]
        payload: dict[str, object] = {
            "failed_files": failed_files,
            "errors": compile_errors,
            "error_excerpt": excerpt,
        }
        if profile_repo_type:
            payload["repo_type"] = profile_repo_type
            payload["verified_by"] = "compile"
        if allowed_paths is not None:
            payload["allowed_paths"] = sorted(allowed_paths)
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.COMPILE_FAILED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            tool_name="compile_failed",
            message="Compile verification failed; attempting repair within allowed files.",
            payload=payload,
        )

    def _attempt_compile_repair(
        self,
        *,
        task: Task,
        actor_name: str,
        plan: GeneratedPlan,
        compile_errors: list[dict],
        sandbox_dir: Path,
        pipeline_state: dict,
        approval_id: str | None,
        timeout_seconds: float | None = None,
        files_per_round: int | None = None,
        allowed_paths: set[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Attempt a narrow syntax-only repair after compile gate failure.

        Processes each broken file individually (one codegen call per file)
        to stay within the 300s timeout. Returns (any_applied, files_touched)
        where files_touched is the list of file paths modified by repair diffs.

        When *timeout_seconds* is provided, the round honours a deadline:
        if the elapsed time exceeds it before all files are processed, a
        ``RepairRoundTimeout`` is raised so the caller can record the
        timeout cleanly and move on to the next round.
        """
        if not compile_errors:
            return False, []

        source_path = self._resolve_knowledge_source_path(task)
        any_applied = False
        all_repair_touched: list[str] = []

        per_round_cap = (
            int(files_per_round)
            if files_per_round and files_per_round > 0
            else int(getattr(self.tool_gateway.settings, "codegen_repair_files_per_round", 5))
        )
        deadline: float | None = None
        if timeout_seconds is not None and timeout_seconds > 0:
            deadline = time.monotonic() + float(timeout_seconds)

        for err in compile_errors[:per_round_cap]:
            if deadline is not None and time.monotonic() >= deadline:
                raise RepairRoundTimeout(
                    f"Repair round exceeded {timeout_seconds:.1f}s deadline "
                    f"after touching {len(all_repair_touched)} file(s)."
                )
            rel_path = str(err.get("file") or "").strip().replace("\\", "/")
            error_msg = err.get("error", "syntax error")
            if not rel_path:
                continue
            if allowed_paths is not None and rel_path not in allowed_paths:
                continue

            full = sandbox_dir / rel_path.replace("/", "\\") if "\\" in str(sandbox_dir) else sandbox_dir / rel_path
            if not full.exists():
                continue

            try:
                broken_content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Load original (pre-patch) version as reference
            orig_content = ""
            orig_section = ""
            if source_path:
                orig = self._read_knowledge_source_context_file(
                    source_path=source_path,
                    relative_path=rel_path,
                )
                if orig:
                    orig_content = orig
                    orig_section = (
                        f"\nORIGINAL FILE (before the broken patch was applied):\n"
                        f"=== ORIGINAL {rel_path} ===\n{orig[:4000]}\n=== END ORIGINAL ===\n\n"
                    )

            first_attempt_diff = ""
            first_attempt_by_file = pipeline_state.get("first_attempt_diff_by_file")
            if isinstance(first_attempt_by_file, dict):
                first_attempt_diff = str(first_attempt_by_file.get(rel_path) or "").strip()
            if not first_attempt_diff:
                stored_first_attempt = str(pipeline_state.get("first_attempt_diff") or "").strip()
                if stored_first_attempt:
                    first_attempt_diff = _slice_diff_for_path(stored_first_attempt, rel_path)

            first_attempt_section = ""
            if first_attempt_diff:
                first_attempt_section = (
                    "FIRST-ATTEMPT DIFF FOR THIS FILE (intent anchor):\n"
                    "This is what we wanted to add. Do not drop these added "
                    "lines while fixing syntax; move or restructure them only "
                    "as needed to preserve the original task intent.\n"
                    f"=== FIRST ATTEMPT {rel_path} ===\n"
                    f"{first_attempt_diff[:6000]}\n"
                    "=== END FIRST ATTEMPT ===\n\n"
                )

            objective_section = (
                "ORIGINAL TASK OBJECTIVE:\n"
                f"- Objective: {_truncate_text(getattr(plan, 'objective', ''), limit=1000)}\n"
                f"- Request summary: {_truncate_text(getattr(plan, 'request_summary', ''), limit=1000)}\n"
                f"- Change summary: {_truncate_text(getattr(plan, 'change_summary', ''), limit=1000)}\n\n"
            )

            scope_section = ""
            if allowed_paths:
                scope_section = (
                    "Modify only files in must_touch_files/expected_new_files: "
                    + ", ".join(sorted(allowed_paths))
                    + "\n"
                )

            related_files_section = self._build_related_files_section(
                rel_path=rel_path,
                error_msg=str(error_msg),
                allowed_paths=allowed_paths,
                sandbox_dir=sandbox_dir,
            )

            unresolved_lock_rules = ""
            if "unresolved reference" in str(error_msg).lower():
                unresolved_lock_rules = (
                    "\nNAME-LOCK INVARIANT (L4f) — read carefully:\n"
                    "The error 'Unresolved reference X' means a symbol named X "
                    "was used but no declaration named X exists. Your ONLY two "
                    "legal fixes are:\n"
                    "  (a) RESTORE the original spelling X in the declaring "
                    "file (revert any rename you introduced).\n"
                    "  (b) UPDATE this referencing file to use the declaring "
                    "file's CURRENT name (whatever spelling exists in the "
                    "declaring file's latest version).\n"
                    "You are FORBIDDEN to introduce a third name. If the "
                    "previous round renamed X->Y in the declaring file and the "
                    "current round still has Unresolved reference X, the only "
                    "allowed action is option (a) RESTORE — do NOT pick a new "
                    "name like Z.\n"
                    "If you cannot decide, prefer (a) RESTORE — preserving the "
                    "original name is always safe.\n\n"
                )

            # Surface symbol verifier findings (Leg 2): when the
            # post-codegen verifier flagged hallucinated Receiver.member
            # references, fold them into the repair prompt as concrete
            # actionable signal so the LLM doesn't have to guess what the
            # compile error actually means.
            symbol_verifier_section = ""
            _dsv_payload = pipeline_state.get("diff_symbol_verifier") or {}
            _dsv_hallucinations = _dsv_payload.get("hallucinations") if isinstance(_dsv_payload, dict) else None
            if _dsv_hallucinations:
                _per_file_hits = [
                    h for h in _dsv_hallucinations
                    if isinstance(h, dict) and h.get("file") == rel_path
                ]
                if _per_file_hits:
                    _hit_lines = [
                        "SYMBOL VERIFIER REJECTIONS (these references in your "
                        "patch do NOT exist in the repository — fix these "
                        "FIRST):"
                    ]
                    for _h in _per_file_hits[:6]:
                        _avail = ", ".join((_h.get("available_members_sample") or [])[:6]) or "(none discovered)"
                        _hit_lines.append(
                            f"  - `{_h.get('receiver')}.{_h.get('member')}` "
                            f"in {_h.get('file')}: receiver `{_h.get('receiver')}` "
                            f"is in {_h.get('receiver_in') or '(unknown)'} which has "
                            f"members [{_avail}]. The named member does not exist."
                        )
                    _hit_lines.append(
                        "Either substitute one of the existing members above, "
                        "or declare the new symbol in the receiver's source "
                        "file. Do not invent names.\n"
                    )
                    symbol_verifier_section = "\n".join(_hit_lines) + "\n\n"

            repair_prompt = (
                f"STRUCTURAL REPAIR TASK - fix ONE broken file: {rel_path}\n\n"
                + objective_section
                + symbol_verifier_section
                + "These compile errors must be fixed without changing task scope.\n"
                + scope_section
                + related_files_section
                + "Common problems include:\n"
                "- Missing imports or unresolved symbols introduced by the patch\n"
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
                + unresolved_lock_rules
                + f"ERROR:\n  {rel_path}: {str(error_msg)[:1000]}\n\n"
                + orig_section
                + first_attempt_section
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

            try:
                intent_threshold = float(
                    getattr(
                        self.tool_gateway.settings,
                        "repair_intent_preservation_threshold",
                        0.4,
                    )
                    or 0.0
                )
            except (TypeError, ValueError):
                intent_threshold = 0.4
            if first_attempt_diff and intent_threshold > 0:
                intent_dropped, intent_ratio, intent_count = _compile_repair_intent_dropped(
                    first_attempt_diff=first_attempt_diff,
                    rel_path=rel_path,
                    repair_diff=repair_diff,
                    baseline_content=orig_content,
                    threshold=intent_threshold,
                )
                if intent_dropped:
                    intent_payload = {
                        "file": rel_path,
                        "intent_preservation_ratio": round(intent_ratio, 3),
                        "threshold": intent_threshold,
                        "intent_line_count": intent_count,
                    }
                    pipeline_state["compile_repair_intent_dropped"] = intent_payload
                    record_event(
                        self.db,
                        task_id=task.id,
                        event_type=EventType.REVIEW_FAILED,
                        source=EventSource.ORCHESTRATOR,
                        stage=WorkflowStage.REVIEW,
                        role=RoleName.REVIEWER,
                        tool_name="compile_repair.intent_dropped",
                        message=(
                            f"Repair for {rel_path} dropped first-attempt intent "
                            f"({intent_ratio:.0%} preserved; threshold {intent_threshold:.0%})."
                        ),
                        payload=intent_payload,
                    )
                    self._workspace_append_audit(
                        task,
                        "compile_repair.intent_dropped",
                        intent_payload,
                    )

                    # Leg 4: instead of giving up silently, give the LLM
                    # one explicit second chance with the names of the
                    # specific lines it dropped. This addresses the v55
                    # P69-19 failure pattern (claude_code repair generated
                    # diffs at 0.242 preservation 3 rounds in a row, with
                    # no feedback channel telling it WHAT it kept dropping).
                    dropped_lines = _intent_lines_dropped_by_repair(
                        first_attempt_diff=first_attempt_diff,
                        rel_path=rel_path,
                        repair_diff=repair_diff,
                        baseline_content=orig_content,
                    )
                    if dropped_lines and not pipeline_state.get(
                        f"compile_repair_intent_retry_{rel_path}"
                    ):
                        pipeline_state[f"compile_repair_intent_retry_{rel_path}"] = True
                        feedback_lines = "\n".join(
                            f"  - {line[:200]}" for line in dropped_lines[:15]
                        )
                        retry_prompt = (
                            repair_prompt
                            + "\n\n## INTENT-DROP RETRY GUIDANCE (you MUST read this) ##\n"
                            "Your previous repair attempt deleted these specific lines "
                            "from the first-attempt diff that implement the user's "
                            "requested feature:\n"
                            f"{feedback_lines}\n\n"
                            "Your repair MUST keep these lines in the post-patch file. "
                            "Only modify the specific line(s) that contain the "
                            "Unresolved-reference compile error. If a referenced symbol "
                            "doesn't exist, RENAME it to a real one (or restore an "
                            "existing one) — do NOT delete the surrounding "
                            "feature-implementing lines.\n"
                        )
                        try:
                            retry_payload = {
                                "plan_json": {"objective": f"Re-fix {rel_path} preserving intent", "steps": []},
                                "context_files": {rel_path: broken_content},
                                "task_description": retry_prompt,
                            }
                            retry_result = self._execute_develop_tool(
                                task=task,
                                actor_name=actor_name,
                                tool_name="codegen.generate_patch",
                                payload=retry_payload,
                                stage=WorkflowStage.REVIEW,
                                role=RoleName.REVIEWER,
                                approval_id=approval_id,
                                pipeline_state=pipeline_state,
                            )
                            retry_diff = str((retry_result or {}).get("diff") or "").strip()
                            # Filter to target file only.
                            _filtered: list[str] = []
                            for _section in re.split(r"(?=^diff --git )", retry_diff, flags=re.MULTILINE):
                                _section = _section.strip()
                                if not _section:
                                    continue
                                _hdr = re.match(r"diff --git a/(.+?) b/", _section)
                                if (_hdr and _hdr.group(1).strip() == rel_path) or not _hdr:
                                    _filtered.append(_section)
                            retry_diff = "\n".join(_filtered).strip()
                            if retry_diff and "diff --git" in retry_diff:
                                _retry_dropped, _retry_ratio, _ = _compile_repair_intent_dropped(
                                    first_attempt_diff=first_attempt_diff,
                                    rel_path=rel_path,
                                    repair_diff=retry_diff,
                                    baseline_content=orig_content,
                                    threshold=intent_threshold,
                                )
                                if not _retry_dropped:
                                    record_event(
                                        self.db,
                                        task_id=task.id,
                                        event_type=EventType.TOOL_SUCCEEDED,
                                        source=EventSource.ORCHESTRATOR,
                                        stage=WorkflowStage.REVIEW,
                                        role=RoleName.REVIEWER,
                                        tool_name="compile_repair.intent_retry_recovered",
                                        message=(
                                            f"Intent-drop retry recovered {rel_path}: "
                                            f"preservation {_retry_ratio:.0%} now passes."
                                        ),
                                    )
                                    repair_diff = retry_diff
                                    # Fall through to apply.
                                else:
                                    record_event(
                                        self.db,
                                        task_id=task.id,
                                        event_type=EventType.REVIEW_FAILED,
                                        source=EventSource.ORCHESTRATOR,
                                        stage=WorkflowStage.REVIEW,
                                        role=RoleName.REVIEWER,
                                        tool_name="compile_repair.intent_retry_failed",
                                        message=(
                                            f"Intent-drop retry still dropping intent for "
                                            f"{rel_path} ({_retry_ratio:.0%}); giving up this round."
                                        ),
                                    )
                                    self._preserve_develop_pipeline_state(
                                        task=task,
                                        pipeline_state=pipeline_state,
                                    )
                                    continue
                            else:
                                self._preserve_develop_pipeline_state(
                                    task=task,
                                    pipeline_state=pipeline_state,
                                )
                                continue
                        except Exception as _retry_exc:  # noqa: BLE001
                            import logging as _log
                            _log.getLogger("orchestrator").warning(
                                "intent_retry.errored: %s",
                                str(_retry_exc)[:200],
                            )
                            self._preserve_develop_pipeline_state(
                                task=task,
                                pipeline_state=pipeline_state,
                            )
                            continue
                    else:
                        self._preserve_develop_pipeline_state(
                            task=task,
                            pipeline_state=pipeline_state,
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

    def _build_codegen_memory_context(self, task: Task) -> str:
        settings_obj = self.tool_gateway.settings
        if not bool(getattr(settings_obj, "memory_enabled", True)):
            return ""
        try:
            service = MemoryService(self.db, settings_obj)
            top_n = int(getattr(settings_obj, "memory_top_n_per_query", 3) or 3)
            scopes = (
                "gate:compile_gate",
                "gate:spec_conformance",
                "gate:evidence_chain",
                "gate:runtime_validation",
                "gate:artifact_existence",
                "gate:failing_test_gate",
                "gate:failure_diagnosis",
                "gate:review",
            )
            memories = []
            seen: set[str] = set()
            for scope in scopes:
                for memory in service.query(
                    scope=scope,
                    kind="gate_failure_resolution",
                    text_hint=task.request_text or "",
                    top_n=top_n,
                ):
                    if memory.id in seen:
                        continue
                    seen.add(memory.id)
                    memories.append(memory)
                    if len(memories) >= top_n:
                        break
                if len(memories) >= top_n:
                    break
            rendered = service.attach_provenance_lines(memories)
            max_lines = max(1, int(getattr(settings_obj, "memory_max_lines_in_prompt", 30) or 30))
            return "\n".join(rendered.splitlines()[:max_lines]).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "codegen memory query failed",
                extra={"task_id": task.id, "error": str(exc)[:300]},
            )
            return ""

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

        source_path = self._resolve_knowledge_source_path(task)
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

    def _resolve_knowledge_source_path(self, task: Task | None = None) -> Path | None:
        """Resolve the active knowledge source path on disk.

        Priority chain (mirrors _resolve_develop_repo_url):
        1. task.translation_json["source_path"] — set by LLM source router
           when KB selected a specific source from knowledge_source_specs.
           This is the SAME path used for sandbox setup, so per-file
           context lookups must use it too — otherwise context fetches
           miss and fall back to KB search (cc_agent), wasting 60-180s
           per file.
        2. settings.knowledge_source_path — global fallback for
           single-source setups.
        """
        if task is not None and isinstance(task.translation_json, dict):
            translation_path = str(task.translation_json.get("source_path") or "").strip()
            if translation_path:
                p = Path(translation_path)
                if p.is_dir():
                    return p
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

    @staticmethod
    def _verification_allowed_paths(plan: GeneratedPlan) -> set[str]:
        paths: set[str] = set()
        for value in list(getattr(plan, "must_touch_files", []) or []) + list(
            getattr(plan, "expected_new_files", []) or []
        ):
            if not isinstance(value, str):
                continue
            normalized = value.strip().replace("\\", "/")
            if normalized:
                paths.add(normalized)
        return paths

    def _prepare_compile_only_verification(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        pipeline_state: dict[str, object],
        error_message: str,
    ) -> dict[str, object]:
        """Degrade missing tests.yaml to profile compile verification."""
        from app.services.verification_profile import resolve_verification_profile

        sandbox_dir = self._develop_sandbox_dir(task)
        allowed_paths = self._verification_allowed_paths(plan)
        if not allowed_paths:
            pipeline_state["compile_gate_done"] = True
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
            return {
                "status": "skipped",
                "overall_passed": True,
                "skipped_count": 1,
                "reason": error_message,
            }

        profile = resolve_verification_profile(sandbox_dir, has_tests_yaml=False)
        profile_payload = profile.to_dict()
        pipeline_state["verification_profile"] = profile_payload
        pipeline_state["verification_allowed_paths"] = sorted(allowed_paths)

        if profile.repo_type == "unknown" or not profile.compile_command:
            return self._verification_skipped_result(
                task=task,
                pipeline_state=pipeline_state,
                reason="unknown_repo_type",
                message="Verification skipped: repository type could not be detected.",
                payload={
                    "error": error_message,
                    "plan_id": plan.plan_id,
                    "profile": profile_payload,
                },
            )

        pipeline_state["verification_compile_pending"] = True
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TOOL_SUCCEEDED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            tool_name="verification_profile.resolve",
            message=(
                f"Test pipeline config missing; degrading to compile-only "
                f"verification for {profile.repo_type}."
            ),
            payload={
                "error": error_message,
                "plan_id": plan.plan_id,
                "profile": profile_payload,
                "allowed_paths": sorted(allowed_paths),
            },
        )
        return {
            "status": "compile_pending",
            "overall_passed": True,
            "total_steps": 0,
            "passed_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "verified_by": "compile",
            "repo_type": profile.repo_type,
            "reason": "tests.yaml missing; compile-only verification will run before review",
        }

    def _verification_skipped_result(
        self,
        *,
        task: Task,
        pipeline_state: dict[str, object],
        reason: str,
        message: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        pipeline_state["verification_skipped"] = True
        pipeline_state["verification_skip_reason"] = reason
        pipeline_state["compile_gate_done"] = True
        pipeline_state.pop("verification_compile_pending", None)
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.VERIFICATION_SKIPPED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            tool_name="verification_skipped",
            message=message,
            payload={**payload, "reason": reason},
        )
        return {
            "status": "skipped",
            "overall_passed": True,
            "total_steps": 0,
            "passed_count": 0,
            "failed_count": 0,
            "skipped_count": 1,
            "verified_by": None,
            "reason": reason,
        }

    @staticmethod
    def _evidence_chain_attestation(pipeline_state: dict[str, object]) -> dict[str, object] | None:
        attestation = pipeline_state.get("goal_attestation")
        if not isinstance(attestation, dict):
            return None
        payload: dict[str, object] = dict(attestation)
        goal_decomposition = pipeline_state.get("goal_decomposition")
        if isinstance(goal_decomposition, dict):
            file_justifications = goal_decomposition.get("file_justifications")
            if isinstance(file_justifications, list) and file_justifications:
                payload["file_justifications"] = file_justifications
        return payload

    @staticmethod
    def _extract_evidence_chain_claims(
        *,
        pipeline_state: dict[str, object],
        codegen_result: dict[str, object],
        review_result: dict[str, object],
    ) -> list[KnowledgeClaim]:
        claims: list[KnowledgeClaim] = []
        for container in (codegen_result, review_result, pipeline_state):
            raw_claims = container.get("claims") if isinstance(container, dict) else None
            if not isinstance(raw_claims, list):
                continue
            for raw_claim in raw_claims:
                if isinstance(raw_claim, KnowledgeClaim):
                    claims.append(raw_claim)
                    continue
                if not isinstance(raw_claim, dict):
                    continue
                try:
                    claims.append(KnowledgeClaim.model_validate(raw_claim))
                except Exception:  # noqa: BLE001
                    continue
        return claims

    @staticmethod
    def _evidence_chain_block_message(report: EvidenceChainReport) -> str:
        block_findings = [
            finding for finding in report.findings if finding.severity == "block"
        ]
        if not block_findings:
            return report.summary
        untracked = [
            finding for finding in block_findings if finding.rule == "untracked_file"
        ]
        if len(untracked) > 1:
            return (
                "Evidence chain broken: "
                f"{len(untracked)} modified files have no evidence backing."
            )
        return block_findings[0].message

    @staticmethod
    def _approval_evidence_chain_payload(evidence_chain: object | None) -> dict[str, object]:
        payload = evidence_chain if isinstance(evidence_chain, dict) else {}
        findings = payload.get("findings") if isinstance(payload, dict) else []
        diagnostic = payload.get("diagnostic") if isinstance(payload, dict) else {}
        warnings = [
            finding
            for finding in findings
            if isinstance(finding, dict) and finding.get("severity") == "warn"
        ] if isinstance(findings, list) else []
        diagnostic_dict = diagnostic if isinstance(diagnostic, dict) else {}
        return {
            "closed": bool(payload.get("closed", True)) if isinstance(payload, dict) else True,
            "warnings": warnings,
            "evidence_count": int(diagnostic_dict.get("evidence_count") or 0),
            "modified_files_with_evidence": list(
                diagnostic_dict.get("modified_files_with_evidence") or []
            ),
            "claims_high_confidence": int(
                diagnostic_dict.get("claims_high_confidence") or 0
            ),
        }

    def _request_jira_transition_approval(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        pipeline_state: dict[str, object],
        codegen_result: dict[str, object],
        review_result: dict[str, object],
        attestation: object,
        evidence_chain: object | None = None,
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
        evidence_chain_payload = self._approval_evidence_chain_payload(evidence_chain)

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
                payload={"error": str(exc)[:5000]},
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
            "evidence_chain": evidence_chain_payload,
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
                "evidence_chain": evidence_chain_payload,
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
        self._write_task_checkpoint(
            task,
            stage="awaiting_approval",
            output_payload=self._task_checkpoint_payload(
                task,
                approval_id=approval.id,
                pipeline_state=pipeline_state,
                plan_json=task.plan_json,
            ),
            sandbox_snapshot_id=self._build_develop_sandbox(task).snapshot_id(),
        )

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
                "evidence_chain": evidence_chain_payload,
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

    def _request_compile_repair_approval(
        self,
        *,
        task: Task,
        plan: GeneratedPlan,
        pipeline_state: dict[str, object],
        rounds_summary: list[dict],
        residual_errors: list[dict],
        sandbox_dir: Path,
    ) -> None:
        """Park the task in AWAITING_APPROVAL after the compile-repair cap is
        exhausted. The reviewer can then (a) manually patch the sandbox and
        grant the approval, (b) reject (→ task FAILED), or (c) extend the
        repair budget by approving with a 'retry' annotation.
        """
        rounds_attempted = sum(
            1
            for r in rounds_summary
            if r.get("note") != "no repair budget remaining"
        )
        if rounds_attempted == 0 and rounds_summary:
            # Fallback: use the highest round number recorded.
            rounds_attempted = max(int(r.get("round") or 0) for r in rounds_summary)
        residual_files = sorted(
            {str(e.get("file") or "") for e in residual_errors if e.get("file")}
        )
        message = (
            "Codegen produced patches that do not compile cleanly. "
            f"{rounds_attempted} repair round(s) attempted; "
            f"{len(residual_files)} file(s) still fail compile. "
            "Reviewer can: (a) manually fix in sandbox + grant approval; "
            "(b) reject and the task will be marked failed; "
            "(c) extend repair budget by approving with a 'retry' annotation."
        )
        diff_path = ""
        try:
            diff_path = str(sandbox_dir)
        except Exception:  # noqa: BLE001
            diff_path = ""

        approval_payload = {
            "decision": "compile_repair_cap_exceeded",
            "rounds_attempted": rounds_attempted,
            "rounds_summary": rounds_summary,
            "residual_compile_errors": residual_errors,
            "diff_path": diff_path,
            "message": message,
        }

        approval = Approval(
            task_id=task.id,
            action_name="compile_repair_cap_exceeded",
            status=ApprovalStatus.PENDING,
            requested_by_role=RoleName.REVIEWER,
            approver_role=ActorRole.TEAM_LEAD.value,
            requested_by_actor_name=task.actor_name,
            risk_level=task.risk_level,
            risk_category=task.risk_category,
            reason=(
                "Compile gate failed across the configured repair budget; "
                "human approval required to decide next step."
            ),
            request_payload_json=approval_payload,
            policy_snapshot_json={
                "decision": "require_approval",
                "source": "develop_compile_repair_cap_exceeded",
                "tool_name": "compile_repair_cap_exceeded",
                "actor_name": task.actor_name,
                "actor_role": task.actor_role.value,
                "risk_level": task.risk_level.value,
                "risk_category": task.risk_category.value,
                "required_approver_role": ActorRole.TEAM_LEAD.value,
            },
        )
        self.db.add(approval)
        self.db.flush()

        pipeline_state["pending_compile_repair_approval_id"] = approval.id
        pipeline_state["compile_repair_cap_exceeded"] = True
        pipeline_state["compile_repair_rounds"] = rounds_summary
        self._preserve_develop_pipeline_state(task=task, pipeline_state=pipeline_state)

        task.pending_approval = True
        task.latest_result_json = {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "message": message,
            "approval_id": approval.id,
            "result": approval_payload,
            "pipeline_state": pipeline_state,
        }
        self._write_task_checkpoint(
            task,
            stage="awaiting_approval",
            output_payload=self._task_checkpoint_payload(
                task,
                approval_id=approval.id,
                pipeline_state=pipeline_state,
                plan_json=task.plan_json,
            ),
            sandbox_snapshot_id=self._build_develop_sandbox(task).snapshot_id(),
        )

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.AWAITING_APPROVAL,
            new_stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            source=EventSource.ORCHESTRATOR,
            message="Awaiting human approval after compile-repair cap exceeded.",
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_REQUESTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            tool_name="compile_repair.cap_exceeded",
            message="Approval requested after compile-repair cap exceeded.",
            payload={
                "approval_id": approval.id,
                "action_name": approval.action_name,
                "approver_role": approval.approver_role,
                "rounds_attempted": rounds_attempted,
                "residual_files": residual_files,
            },
        )
        self._workspace_append_audit(
            task,
            "compile_repair.cap_exceeded",
            {
                "approval_id": approval.id,
                "rounds_attempted": rounds_attempted,
                "residual_error_count": len(residual_errors),
            },
        )
        self._run_failure_diagnosis(task, failure_kind="compile_repair_cap_exceeded")
        commit_checkpoint(self.db, label="awaiting_approval_compile_repair_cap")

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
        self._run_failure_diagnosis(task, failure_kind="tool_failed_terminal")

    def _run_failure_diagnosis(self, task: Task, *, failure_kind: FailureKind) -> None:
        try:
            diagnosis = run_diagnosis(
                task=task,
                db=self.db,
                settings=self.tool_gateway.settings,
                failure_kind=failure_kind,
            )
            if diagnosis is not None:
                event = self.db.scalars(
                    select(Event)
                    .where(
                        Event.task_id == task.id,
                        Event.event_type == EventType.FAILURE_DIAGNOSIS_GENERATED,
                    )
                    .order_by(Event.created_at.desc())
                    .limit(1)
                ).first()
                if event is not None:
                    MemoryService(self.db, self.tool_gateway.settings).maybe_record_gate_event(
                        event=event,
                        task=task,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failure diagnosis hook failed",
                extra={"task_id": task.id, "error": str(exc)[:300]},
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
        self._write_task_checkpoint(
            task,
            stage="awaiting_approval",
            output_payload=self._task_checkpoint_payload(
                task,
                approval_id=approval_id,
                execution_id=execution_id,
                tool_name=tool_name,
            ),
        )
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
        # Hard kill switch: when OPS_AGENT_JIRA_WRITEBACK_DISABLED=true,
        # bail before any jira.add_comment / jira.transition_issue call.
        # User explicitly forbade Jira side-effects on 2026-05-07 after
        # v48 + v48b mis-classified continuations posted spurious comments.
        if str(getattr(self.tool_gateway.settings, "jira_writeback_disabled", False)).lower() in ("true", "1", "yes"):
            set_task_status(
                self.db,
                task=task,
                new_status=TaskStatus.COMPLETED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.ACTION,
                message="Jira writeback skipped (OPS_AGENT_JIRA_WRITEBACK_DISABLED).",
                payload={"reason": "writeback_disabled_by_config"},
            )
            record_event(
                self.db,
                task_id=task.id,
                event_type=EventType.EXECUTION_COMPLETED,
                source=EventSource.ORCHESTRATOR,
                stage=WorkflowStage.DONE,
                role=RoleName.ACTION,
                message="Jira writeback intentionally disabled — no comment/transition posted.",
                payload={"jira_writeback_disabled": True},
            )
            return
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
