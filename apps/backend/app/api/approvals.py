from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import ActorRole, ApprovalStatus
from app.schemas.approval import ApprovalDecisionRequest, ApprovalRead
from app.services.approvals import ApprovalService

router = APIRouter(prefix="/approvals", tags=["approvals"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[ApprovalRead])
def list_approvals(
    db: DbSession,
    status: ApprovalStatus | None = None,
    approver_role: ActorRole | None = None,
    task_id: str | None = None,
) -> list[ApprovalRead]:
    service = ApprovalService(db)
    return service.list_approvals(status=status, approver_role=approver_role, task_id=task_id)


@router.post("/{approval_id}/grant", response_model=ApprovalRead)
def grant_approval(approval_id: str, payload: ApprovalDecisionRequest, db: DbSession) -> ApprovalRead:
    service = ApprovalService(db)
    try:
        return service.grant(approval_id=approval_id, payload=payload)
    except ValueError as exc:
        if str(exc) == "Approval not found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{approval_id}/reject", response_model=ApprovalRead)
def reject_approval(approval_id: str, payload: ApprovalDecisionRequest, db: DbSession) -> ApprovalRead:
    service = ApprovalService(db)
    try:
        return service.reject(approval_id=approval_id, payload=payload)
    except ValueError as exc:
        if str(exc) == "Approval not found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
