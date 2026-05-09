from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.db import SessionLocal
from app.core.enums import (
    ActorRole,
    ApprovalStatus,
    EventSource,
    EventType,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    WorkflowStage,
)
from app.core.jira import extract_jira_issue_reference
from app.core.pipeline_executor import submit_pipeline_job
from app.models.event import Event
from app.models.task import Task
from app.models.tool_execution import ToolExecution
from app.orchestrator.service import PrimaryOrchestrator, classify_request
from app.schemas.task import TaskCreateRequest, TaskRollbackRequest
from app.services import task_cancel
from app.services.events import record_event, set_task_status
from app.services.rollback import RollbackExecutor
from app.services.task_cancel import TaskCancelledError

logger = logging.getLogger("app.services.tasks")


def _external_timeout_payload(exc: Exception) -> dict[str, str] | None:
    error_name = type(exc).__name__
    error_text = str(exc)
    if error_name in {
        "ConnectTimeout",
        "PoolTimeout",
        "ReadTimeout",
        "TimeoutException",
        "TimeoutExpired",
    } or "timed out" in error_text.lower() or "timeout" in error_text.lower():
        payload = {
            "reason": "external_api_timeout",
            "error_type": error_name,
            "error": error_text,
        }
        provider_name = getattr(exc, "provider_name", None)
        if isinstance(provider_name, str) and provider_name:
            payload["provider_name"] = provider_name
        return payload
    return None


def run_pipeline_job(task_id: str, actor_name: str) -> None:
    """Runs inside the thread pool. Owns its own SessionLocal."""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            logger.warning("Pipeline job could not find task %s", task_id)
            return

        try:
            orchestrator = PrimaryOrchestrator(db)
            orchestrator.bootstrap_task(task, actor_name=actor_name)
            db.commit()
        except TaskCancelledError as cancel_exc:
            # Watchdog already set status=STALE_FAILED + recorded the
            # EXECUTION_FAILED event before requesting cancel, so do NOT
            # touch the task row here — just unwind the worker thread
            # cleanly so the executor slot frees up.
            db.rollback()
            task_cancel.clear_cancel(task_id)
            logger.info(
                "Pipeline job exited via cooperative cancel for task %s (reason=%s)",
                task_id,
                cancel_exc.reason,
            )
            return
        except Exception as exc:
            db.rollback()
            logger.exception("Background pipeline job crashed for task %s", task_id)
            db2 = SessionLocal()
            try:
                failed_task = db2.get(Task, task_id)
                if failed_task is None:
                    logger.warning("Pipeline crash handler could not find task %s", task_id)
                    return

                error_payload = _external_timeout_payload(exc) or {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                set_task_status(
                    db2,
                    task=failed_task,
                    new_status=TaskStatus.FAILED,
                    new_stage=WorkflowStage.DONE,
                    role=RoleName.SYSTEM,
                    message=f"Pipeline crashed: {type(exc).__name__}: {exc}",
                    payload=error_payload,
                )
                record_event(
                    db2,
                    task_id=failed_task.id,
                    event_type=EventType.EXECUTION_FAILED,
                    source=EventSource.SYSTEM,
                    stage=WorkflowStage.DONE,
                    role=RoleName.SYSTEM,
                    message="Background pipeline job raised an unhandled exception.",
                    payload=error_payload,
                )
                db2.commit()
            finally:
                db2.close()
    finally:
        db.close()


def resume_pipeline_job(task_id: str) -> None:
    """Resume an interrupted task from its latest DB checkpoint."""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            logger.warning("Resume job could not find task %s", task_id)
            return
        try:
            orchestrator = PrimaryOrchestrator(db)
            resumed = orchestrator.resume_task(task=task, actor_name=task.actor_name)
            if resumed:
                db.commit()
        except TaskCancelledError as cancel_exc:
            db.rollback()
            task_cancel.clear_cancel(task_id)
            logger.info(
                "Resume job exited via cooperative cancel for task %s (reason=%s)",
                task_id,
                cancel_exc.reason,
            )
            return
        except Exception as exc:
            db.rollback()
            logger.exception("Resume job crashed for task %s", task_id)
            failed_task = db.get(Task, task_id)
            if failed_task is None:
                return
            error_payload = _external_timeout_payload(exc) or {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            set_task_status(
                db,
                task=failed_task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.SYSTEM,
                message=f"Resume crashed: {type(exc).__name__}: {exc}",
                payload=error_payload,
            )
            record_event(
                db,
                task_id=failed_task.id,
                event_type=EventType.EXECUTION_FAILED,
                source=EventSource.SYSTEM,
                stage=WorkflowStage.DONE,
                role=RoleName.SYSTEM,
                message="Checkpoint resume raised an unhandled exception.",
                payload=error_payload,
            )
            db.commit()
    finally:
        db.close()


class TaskService:
    def __init__(self, db: Session):
        self.db = db

    def create_task(self, payload: TaskCreateRequest) -> Task:
        effective_request = payload.request
        effective_session_id = payload.session_id
        continuation_meta: dict | None = None

        if payload.previous_task_id:
            parent = self.db.get(Task, payload.previous_task_id)
            if parent is None:
                raise ValueError(f"Parent task {payload.previous_task_id} not found")

            parent_status = parent.status.value if parent.status else "unknown"
            parent_plan_json = parent.plan_json if isinstance(parent.plan_json, dict) else None
            parent_result_json = (
                parent.latest_result_json if isinstance(parent.latest_result_json, dict) else None
            )
            effective_request = self._build_continuation_request(
                parent_request=parent.request_text,
                parent_plan_json=parent_plan_json,
                parent_result_json=parent_result_json,
                parent_id=parent.id,
                parent_status=parent_status,
                user_followup=payload.request,
            )
            effective_session_id = effective_session_id or parent.session_id
            continuation_meta = {
                "previous_task_id": parent.id,
                "parent_status": parent_status,
                "parent_scenario": parent.scenario,
            }

        intent_text = self._extract_user_intent_text(effective_request)
        # Caller may force scenario explicitly (SWE-bench harness uses
        # this — long problem statements contain "complete" / "done" /
        # "fix" verbs that always trip the writeback classifier). Forced
        # scenario takes priority over both the regex classifier and any
        # parent-scenario inheritance.
        forced_scenario = (getattr(payload, "scenario_override", None) or "").strip()
        if forced_scenario:
            scenario = forced_scenario
        else:
            scenario = classify_request(intent_text)
            # Continuation should inherit the parent task's scenario by default.
            # Empirical (v48 P69-17): augmented continuation preamble contains
            # the parent's status string ("completed") which trips the writeback
            # classifier ("complete" keyword) and skips the develop pipeline
            # entirely. The parent's scenario is the truer intent signal.
            if continuation_meta and continuation_meta.get("parent_scenario"):
                scenario = continuation_meta["parent_scenario"]
        risk_level = self._infer_risk_level(intent_text)
        risk_category = self._infer_risk_category(intent_text, scenario=scenario)
        governance_json = {
            "actor": {
                "name": payload.actor_name,
                "role": payload.actor_role.value,
            },
            "risk": {
                "level": risk_level.value,
                "category": risk_category.value,
            },
            "policy_state": "not_evaluated",
        }
        if continuation_meta is not None:
            governance_json["continuation"] = continuation_meta
        if getattr(payload, "skip_jira_prefetch", False):
            # Sticky on the task so the orchestrator can read it any time
            # planning runs (resume, retry, etc.) without going back to
            # the original request payload.
            governance_json["skip_jira_prefetch"] = True

        task = Task(
            session_id=effective_session_id or str(uuid4()),
            actor_name=payload.actor_name,
            actor_role=payload.actor_role,
            title=payload.title or self._build_title(intent_text),
            request_text=effective_request,
            scenario=scenario,
            status=TaskStatus.CREATED,
            workflow_stage=WorkflowStage.INTAKE,
            current_role=RoleName.PRIMARY,
            risk_level=risk_level,
            risk_category=risk_category,
            governance_json=governance_json,
            # Optional override; None = orchestrator falls back to env defaults.
            source_name=getattr(payload, "source_name", None),
        )
        # Retry-on-locked: under parallel benchmark load (e.g. SWE-bench
        # harness with parallel=4) concurrent pipeline workers all hold
        # SQLite write tickets and a fresh POST /api/tasks loses the
        # race. record_event already handles this; mirror the strategy
        # for the Task INSERT itself.
        import random as _random
        import time as _time
        for _attempt in range(5):
            try:
                self.db.add(task)
                self.db.flush()
                break
            except Exception as _exc:  # noqa: BLE001
                self.db.rollback()
                if "database is locked" not in str(_exc).lower() or _attempt == 4:
                    raise
                _time.sleep(0.05 * (2 ** _attempt) + _random.uniform(0.0, 0.05))

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.TASK_CREATED,
            source=EventSource.API,
            stage=WorkflowStage.INTAKE,
            role=RoleName.PRIMARY,
            message="Task created from user request.",
            payload={
                "title": task.title,
                "scenario": scenario,
                "session_id": task.session_id,
                "actor_role": task.actor_role.value,
                "risk_category": task.risk_category.value,
                "continuation_of": (
                    continuation_meta.get("previous_task_id") if continuation_meta else None
                ),
            },
        )
        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.USER_REQUEST_RECEIVED,
            source=EventSource.API,
            stage=WorkflowStage.INTAKE,
            role=RoleName.PRIMARY,
            message="User request received by the API.",
            payload={
                "actor_name": payload.actor_name,
                "actor_role": payload.actor_role.value,
                "request": effective_request,
                "user_followup": payload.request,
                "augmented": continuation_meta is not None,
                "session_id": task.session_id,
            },
        )

        self.db.commit()
        future = submit_pipeline_job(run_pipeline_job, task.id, payload.actor_name)
        if future.done() and getattr(future, "_pipeline_executor_override", False):
            self.db.expire_all()

        return self.get_task(task.id, raise_if_missing=True)

    def list_tasks(
        self,
        *,
        search: str | None = None,
        session_id: str | None = None,
        status: TaskStatus | None = None,
        provider: str | None = None,
        actor_role: ActorRole | None = None,
        risk_category: RiskCategory | None = None,
    ) -> list[Task]:
        stmt = select(Task).order_by(Task.created_at.desc())
        # chat_tool_call rows are synthetic Task records created by
        # _execute_chat_tool_blocking solely so the gateway can write a
        # proper ToolExecution audit row (FK to Task). They are not user
        # tasks and would otherwise spam the chat sidebar.
        stmt = stmt.where(Task.scenario != "chat_tool_call")
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if actor_role is not None:
            stmt = stmt.where(Task.actor_role == actor_role)
        if risk_category is not None:
            stmt = stmt.where(Task.risk_category == risk_category)

        tasks = list(self.db.scalars(stmt))

        normalized_search = (search or "").strip().lower()
        normalized_session_id = (session_id or "").strip().lower()
        normalized_provider = (provider or "").strip().lower()

        filtered_tasks: list[Task] = []
        for task in tasks:
            task_provider = (task.plan_provider_name or "").strip().lower()

            if normalized_session_id and normalized_session_id not in (task.session_id or "").lower():
                continue

            if normalized_provider:
                if normalized_provider == "unknown":
                    if task_provider:
                        continue
                elif task_provider != normalized_provider:
                    continue

            if normalized_search:
                haystack = " ".join(
                    [
                        task.title,
                        task.id,
                        task.scenario,
                        task.status.value,
                        task.workflow_stage.value,
                        task.actor_name,
                        task.actor_role.value,
                        task.risk_category.value,
                        task.session_id or "",
                        task.plan_provider_name or "",
                        task.plan_model_name or "",
                        task.review_verdict or "",
                        task.review_summary or "",
                    ]
                ).lower()
                if normalized_search not in haystack:
                    continue

            filtered_tasks.append(task)

        return filtered_tasks

    def get_task(self, task_id: str, *, raise_if_missing: bool = False) -> Task | None:
        stmt = (
            select(Task)
            .options(selectinload(Task.approvals))
            .where(Task.id == task_id)
        )
        task = self.db.scalars(stmt).first()
        if task is None and raise_if_missing:
            raise ValueError("Task not found")
        return task

    def task_exists(self, task_id: str) -> bool:
        stmt = select(Task.id).where(Task.id == task_id)
        return self.db.execute(stmt).scalar_one_or_none() is not None

    def list_events(self, task_id: str) -> list[Event]:
        stmt = select(Event).where(Event.task_id == task_id).order_by(Event.created_at.asc())
        return list(self.db.scalars(stmt))

    def list_tool_executions(self, task_id: str) -> list[ToolExecution]:
        stmt = (
            select(ToolExecution)
            .where(ToolExecution.task_id == task_id)
            .order_by(ToolExecution.started_at.desc())
        )
        return list(self.db.scalars(stmt))

    def rollback_task(self, *, task_id: str, payload: TaskRollbackRequest) -> Task:
        task = self.get_task(task_id, raise_if_missing=True)
        if task.status == TaskStatus.ROLLED_BACK:
            raise ValueError("Task is already rolled back")

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.ROLLBACK_REQUESTED,
            source=EventSource.API,
            stage=task.workflow_stage,
            role=RoleName.SYSTEM,
            message="Rollback requested for task state.",
            payload={"actor_name": payload.actor_name, "reason": payload.reason},
        )

        pending_approvals = [
            approval for approval in task.approvals if approval.status == ApprovalStatus.PENDING
        ]
        for approval in pending_approvals:
            approval.status = ApprovalStatus.CANCELLED
            approval.decided_by_actor_name = payload.actor_name
            approval.decision_payload_json = {
                "actor_name": payload.actor_name,
                "reason": payload.reason,
                "outcome": "cancelled_by_rollback",
            }

        task.pending_approval = False
        rollback_result = RollbackExecutor(self.db).execute_rollback(task_id=task.id)
        rollback_steps = [
            {
                "execution_id": step.execution_id,
                "tool_name": step.tool_name,
                "inverse_type": step.inverse_type,
                "success": step.success,
                "message": step.message,
            }
            for step in rollback_result.steps
        ]
        task.latest_result_json = {
            "status": TaskStatus.ROLLED_BACK.value,
            "message": (
                "Rollback completed: "
                f"{rollback_result.succeeded_count}/{rollback_result.total_steps} inverses executed."
            ),
            "rollback": {
                "total_steps": rollback_result.total_steps,
                "succeeded": rollback_result.succeeded_count,
                "failed": rollback_result.failed_count,
                "skipped": rollback_result.skipped_count,
                "steps": rollback_steps,
            },
            "reason": payload.reason,
        }

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.ROLLED_BACK,
            new_stage=WorkflowStage.DONE,
            role=RoleName.SYSTEM,
            source=EventSource.SYSTEM,
            message="Task marked as rolled back.",
            payload={"actor_name": payload.actor_name},
        )

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_CANCELLED,
            source=EventSource.SYSTEM,
            stage=WorkflowStage.DONE,
            role=RoleName.SYSTEM,
            message="Pending approvals were cancelled during rollback.",
            payload={"cancelled_approvals": len(pending_approvals)},
        )

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.ROLLBACK_COMPLETED,
            source=EventSource.SYSTEM,
            stage=WorkflowStage.DONE,
            role=RoleName.SYSTEM,
            message="Rollback completed.",
            payload={
                "cancelled_approvals": len(pending_approvals),
                "total_steps": rollback_result.total_steps,
                "succeeded": rollback_result.succeeded_count,
                "failed": rollback_result.failed_count,
                "skipped": rollback_result.skipped_count,
            },
        )

        self.db.commit()
        return self.get_task(task.id, raise_if_missing=True)

    @staticmethod
    def _extract_user_intent_text(request_text: str) -> str:
        marker = "\n\nFollow-up request:\n"
        marker_index = request_text.rfind(marker)
        if marker_index == -1:
            return request_text
        follow_up = request_text[marker_index + len(marker) :].strip()
        return follow_up or request_text

    @staticmethod
    def _build_continuation_request(
        parent_request: str,
        parent_plan_json: dict | None,
        parent_result_json: dict | None,
        parent_id: str,
        parent_status: str,
        user_followup: str,
    ) -> str:
        plan_json = parent_plan_json or {}
        result_json = parent_result_json or {}
        plan_objective = str(plan_json.get("objective", "") or "")[:500]
        failure_reason = str(result_json.get("reason", "") or parent_status or "unknown")
        failure_message = str(result_json.get("message", "") or "")[:600]

        # Pull richer diagnostic context from the parent task's workspace
        # so DeepSeek (or claude_code) sees specific compile errors and
        # the rejected diff text — not just the truncated reason string.
        # Empirical: today's continuation runs with only reason+message
        # could not see WHY the parent's diff was malformed. Adding the
        # full file:line:error list and the rejected diff content gives
        # a much clearer signal of "don't repeat that mistake."
        compile_errors_block = ""
        rejected_diff_block = ""
        try:
            from pathlib import Path as _P
            import json as _json
            backend_root = _P(__file__).resolve().parents[2]
            workspace_dir = backend_root / "data" / "agent_workspace" / str(parent_id)
            attempts_dir = workspace_dir / "attempts"
            if attempts_dir.is_dir():
                # Latest numeric attempt for compile.json
                numeric_attempts = sorted(
                    [d for d in attempts_dir.iterdir() if d.is_dir() and d.name.isdigit()],
                    key=lambda p: p.name,
                )
                if numeric_attempts:
                    latest = numeric_attempts[-1]
                    compile_path = latest / "compile.json"
                    if compile_path.is_file():
                        try:
                            cdata = _json.loads(compile_path.read_text(encoding="utf-8"))
                            errs = cdata.get("errors") or []
                            if errs:
                                lines: list[str] = []
                                for e in errs[:8]:
                                    if isinstance(e, dict):
                                        lines.append(
                                            f"  - {e.get('file','')}:{e.get('line','')}: "
                                            f"{str(e.get('error') or e.get('message') or '')[:180]}"
                                        )
                                if lines:
                                    compile_errors_block = (
                                        "\n\nParent compile errors (avoid these specifically):\n"
                                        + "\n".join(lines)
                                    )
                        except Exception:  # noqa: BLE001
                            pass
                # Latest rejected diff (self_validate dumps)
                rejected_dir = attempts_dir / "rejected"
                if rejected_dir.is_dir():
                    rejected_files = sorted(
                        rejected_dir.glob("rejected_*.patch"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    if rejected_files:
                        try:
                            text = rejected_files[-1].read_text(encoding="utf-8", errors="replace")
                            # Strip leading "# rejected ..." header lines so
                            # the LLM sees the diff body directly. Keep the
                            # diff portion truncated to keep prompt size sane.
                            body_lines: list[str] = []
                            in_body = False
                            for line in text.splitlines():
                                if not in_body and line.startswith("# ---- raw diff"):
                                    in_body = True
                                    continue
                                if in_body:
                                    body_lines.append(line)
                            diff_text = "\n".join(body_lines).strip() or text
                            rejected_diff_block = (
                                "\n\nParent rejected diff (this exact output was REJECTED — do not "
                                "produce something with the same hunk-line-count or symbol-existence issues):\n"
                                "```diff\n"
                                + diff_text[:3000]
                                + ("\n... [truncated]" if len(diff_text) > 3000 else "")
                                + "\n```"
                            )
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001 — never break task creation on workspace issues
            pass

        # Wrap parent UUID in spaces around dash-separated segments so the
        # Jira-issue reference regex doesn't match BD4F-4139 from a UUID
        # like 13598c9f-bd4f-4139-9c12-... and try to fetch a non-existent
        # Jira issue. Empirical: v47 failed at intake with HTTP 404 from
        # Jira because the continuation preamble exposed the raw UUID.
        safe_parent_id = parent_id.replace("-", " ")
        return (
            f"[CONTINUATION FROM PARENT TASK uuid={safe_parent_id}]\n\n"
            "Parent original request:\n"
            f"{(parent_request or '')[:1500]}\n\n"
            f"Parent objective: {plan_objective}\n\n"
            f"Parent failure reason: {failure_reason}\n"
            f"Parent failure message: {failure_message}"
            f"{compile_errors_block}"
            f"{rejected_diff_block}"
            "\n\n[USER FOLLOWUP / NEW REQUEST]:\n"
            f"{user_followup}"
        )

    @staticmethod
    def _build_title(request_text: str) -> str:
        issue_reference = extract_jira_issue_reference(request_text)
        lowered = request_text.lower()
        if issue_reference and any(keyword in lowered for keyword in ("plan", "implement", "rollout", "scope", "jira")):
            return f"Plan Jira issue {issue_reference.issue_key}"

        normalized = " ".join(request_text.strip().split())
        if len(normalized) <= 60:
            return normalized
        return f"{normalized[:57]}..."

    @staticmethod
    def _infer_risk_level(request_text: str) -> RiskLevel:
        lowered = request_text.lower()
        if any(keyword in lowered for keyword in ("delete", "production", "finance", "payroll", "admin")):
            return RiskLevel.HIGH
        if any(keyword in lowered for keyword in ("approval", "notify", "update", "ticket")):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _infer_risk_category(request_text: str, *, scenario: str) -> RiskCategory:
        lowered = request_text.lower()
        if scenario == "process_question":
            if any(keyword in lowered for keyword in ("export", "download", "dump", "share secret", "leak")):
                return RiskCategory.KNOWLEDGE_EXFILTRATION
            return RiskCategory.KNOWLEDGE_LOOKUP
        if scenario == "slack_message":
            return RiskCategory.EXTERNAL_BROADCAST
        if scenario in {"jira_issue_create", "jira_issue_plan", "jira_issue_writeback"}:
            return RiskCategory.CHANGE_MANAGEMENT
        if scenario in {"internal_api_request", "action_with_approval"}:
            return RiskCategory.CONFIGURATION_CHANGE
        if scenario == "internal_db_query":
            if any(keyword in lowered for keyword in ("insert", "update", "delete", "drop", "alter")):
                return RiskCategory.PRODUCTION_WRITE
            return RiskCategory.PRIVILEGED_DATA_ACCESS
        return RiskCategory.GENERAL
