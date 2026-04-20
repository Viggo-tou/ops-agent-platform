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
from app.services.events import record_event, set_task_status
from app.services.rollback import RollbackExecutor

logger = logging.getLogger("app.services.tasks")


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
        except Exception as exc:
            db.rollback()
            logger.exception("Background pipeline job crashed for task %s", task_id)
            db2 = SessionLocal()
            try:
                failed_task = db2.get(Task, task_id)
                if failed_task is None:
                    logger.warning("Pipeline crash handler could not find task %s", task_id)
                    return

                error_payload = {"error_type": type(exc).__name__, "error": str(exc)}
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


class TaskService:
    def __init__(self, db: Session):
        self.db = db

    def create_task(self, payload: TaskCreateRequest) -> Task:
        intent_text = self._extract_user_intent_text(payload.request)
        scenario = classify_request(intent_text)
        risk_level = self._infer_risk_level(intent_text)
        risk_category = self._infer_risk_category(intent_text, scenario=scenario)
        task = Task(
            session_id=payload.session_id or str(uuid4()),
            actor_name=payload.actor_name,
            actor_role=payload.actor_role,
            title=payload.title or self._build_title(intent_text),
            request_text=payload.request,
            scenario=scenario,
            status=TaskStatus.CREATED,
            workflow_stage=WorkflowStage.INTAKE,
            current_role=RoleName.PRIMARY,
            risk_level=risk_level,
            risk_category=risk_category,
            governance_json={
                "actor": {
                    "name": payload.actor_name,
                    "role": payload.actor_role.value,
                },
                "risk": {
                    "level": risk_level.value,
                    "category": risk_category.value,
                },
                "policy_state": "not_evaluated",
            },
        )
        self.db.add(task)
        self.db.flush()

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
                "request": payload.request,
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
