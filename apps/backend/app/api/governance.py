from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import ActorRole, PolicyDecision
from app.schemas.governance import PolicyRuleRead, RbacRoleRead
from app.services.governance import GovernanceService

router = APIRouter(prefix="/governance", tags=["governance"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/roles", response_model=list[RbacRoleRead])
def list_roles(
    db: DbSession,
    active_only: bool = True,
) -> list[RbacRoleRead]:
    service = GovernanceService(db)
    return service.list_roles(active_only=active_only)


@router.get("/policy-rules", response_model=list[PolicyRuleRead])
def list_policy_rules(
    db: DbSession,
    subject_role: ActorRole | None = None,
    resource_type: str | None = None,
    decision: PolicyDecision | None = None,
    active_only: bool = True,
) -> list[PolicyRuleRead]:
    service = GovernanceService(db)
    return service.list_policy_rules(
        subject_role=subject_role,
        resource_type=resource_type,
        decision=decision,
        active_only=active_only,
    )
