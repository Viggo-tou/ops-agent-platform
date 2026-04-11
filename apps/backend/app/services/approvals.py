from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.enums import ActorRole, ApprovalStatus, EventSource, EventType, RoleName, TaskStatus, WorkflowStage
from app.models.approval import Approval
from app.orchestrator.service import PrimaryOrchestrator
from app.schemas.approval import ApprovalDecisionRequest
from app.services.events import record_event, set_task_status


class ApprovalService:
    def __init__(self, db: Session):
        self.db = db
        self.orchestrator = PrimaryOrchestrator(db)

    def list_approvals(
        self,
        *,
        status: ApprovalStatus | None = None,
        approver_role: ActorRole | None = None,
        task_id: str | None = None,
    ) -> list[Approval]:
        stmt = select(Approval).options(selectinload(Approval.task)).order_by(Approval.requested_at.desc())
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        if approver_role is not None:
            stmt = stmt.where(Approval.approver_role == approver_role.value)
        if task_id:
            stmt = stmt.where(Approval.task_id == task_id)
        return list(self.db.scalars(stmt))

    def grant(self, *, approval_id: str, payload: ApprovalDecisionRequest) -> Approval:
        approval = self._get_approval(approval_id, raise_if_missing=True)
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("Approval is not pending")

        task = approval.task
        approval.status = ApprovalStatus.GRANTED
        approval.decided_at = datetime.now(timezone.utc)
        approval.decided_by_actor_name = payload.actor_name
        approval.decision_payload_json = {
            "actor_name": payload.actor_name,
            "actor_role": payload.actor_role.value,
            "notes": payload.notes,
            "decision": ApprovalStatus.GRANTED.value,
        }
        task.pending_approval = False

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_GRANTED,
            source=EventSource.APPROVAL,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Approval granted for pending action.",
            payload={
                "approval_id": approval.id,
                "actor_name": payload.actor_name,
                "actor_role": payload.actor_role.value,
            },
        )
        self.orchestrator.resume_after_approval(task=task, actor_name=payload.actor_name, approval_id=approval.id)

        self.db.commit()
        return self._get_approval(approval.id, raise_if_missing=True)

    def reject(self, *, approval_id: str, payload: ApprovalDecisionRequest) -> Approval:
        approval = self._get_approval(approval_id, raise_if_missing=True)
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("Approval is not pending")

        task = approval.task
        approval.status = ApprovalStatus.REJECTED
        approval.decided_at = datetime.now(timezone.utc)
        approval.decided_by_actor_name = payload.actor_name
        approval.decision_payload_json = {
            "actor_name": payload.actor_name,
            "actor_role": payload.actor_role.value,
            "notes": payload.notes,
            "decision": ApprovalStatus.REJECTED.value,
        }
        task.pending_approval = False
        task.latest_result_json = {
            "status": TaskStatus.FAILED.value,
            "message": "Approval rejected. No action was executed.",
            "approval_id": approval.id,
        }

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.APPROVAL_REJECTED,
            source=EventSource.APPROVAL,
            stage=WorkflowStage.REVIEW,
            role=RoleName.REVIEWER,
            message="Approval rejected for pending action.",
            payload={
                "approval_id": approval.id,
                "actor_name": payload.actor_name,
                "actor_role": payload.actor_role.value,
            },
        )

        set_task_status(
            self.db,
            task=task,
            new_status=TaskStatus.FAILED,
            new_stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            source=EventSource.ORCHESTRATOR,
            message="Task failed because approval was rejected.",
        )

        record_event(
            self.db,
            task_id=task.id,
            event_type=EventType.FINAL_RESPONSE_EMITTED,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.DONE,
            role=RoleName.PRIMARY,
            message="Final response emitted after approval rejection.",
            payload={"approval_id": approval.id},
        )

        self.db.commit()
        return self._get_approval(approval.id, raise_if_missing=True)

    def _get_approval(self, approval_id: str, *, raise_if_missing: bool = False) -> Approval | None:
        stmt = (
            select(Approval)
            .options(selectinload(Approval.task))
            .where(Approval.id == approval_id)
        )
        approval = self.db.scalars(stmt).first()
        if approval is None and raise_if_missing:
            raise ValueError("Approval not found")
        return approval
