from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Callable, Literal, cast

from fastapi import Header, HTTPException, status

from app.core.enums import ActorRole

Permission = Literal[
    "task:create",
    "task:create_high_risk",
    "knowledge:upload",
    "knowledge:delete",
    "memory:edit",
    "settings:view",
    "settings:model_config",
    "approval:decide",
]

AppRole = Literal["admin", "operator", "member", "viewer"]

# Keep in sync with apps/web/src/lib/auth.tsx::rolePermissions.
PERMISSION_MAP: dict[AppRole, frozenset[Permission]] = {
    "admin": frozenset(
        {
            "task:create",
            "task:create_high_risk",
            "knowledge:upload",
            "knowledge:delete",
            "memory:edit",
            "settings:view",
            "settings:model_config",
            "approval:decide",
        }
    ),
    "operator": frozenset(
        {
            "task:create",
            "task:create_high_risk",
            "knowledge:upload",
            "memory:edit",
            "settings:view",
            "settings:model_config",
            "approval:decide",
        }
    ),
    "member": frozenset({"task:create", "memory:edit"}),
    "viewer": frozenset(),
}

ACTOR_ROLE_TO_APP_ROLE: dict[ActorRole, AppRole] = {
    ActorRole.ADMIN: "admin",
    ActorRole.TEAM_LEAD: "operator",
    ActorRole.MANAGER: "operator",
    ActorRole.EMPLOYEE: "member",
    ActorRole.SYSTEM: "admin",
}


@dataclass(frozen=True)
class ActorContext:
    app_role: AppRole
    actor_role: ActorRole


def get_actor_context(
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
    x_actor_app_role: Annotated[str | None, Header(alias="X-Actor-App-Role")] = None,
) -> ActorContext:
    if not x_actor_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Actor-Role header",
        )

    try:
        actor_role = ActorRole(x_actor_role)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unknown actor role: {x_actor_role}",
        ) from exc

    if x_actor_app_role and x_actor_app_role in PERMISSION_MAP:
        app_role = cast(AppRole, x_actor_app_role)
    else:
        app_role = ACTOR_ROLE_TO_APP_ROLE.get(actor_role, "viewer")

    return ActorContext(app_role=app_role, actor_role=actor_role)


def require_permission(*permissions: Permission) -> Callable[..., ActorContext]:
    def dependency(
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_actor_app_role: Annotated[str | None, Header(alias="X-Actor-App-Role")] = None,
    ) -> ActorContext:
        actor_context = get_actor_context(
            x_actor_role=x_actor_role,
            x_actor_app_role=x_actor_app_role,
        )
        granted = PERMISSION_MAP[actor_context.app_role]
        missing = [permission for permission in permissions if permission not in granted]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permissions: {', '.join(missing)}",
            )
        return actor_context

    return dependency


__all__ = [
    "ACTOR_ROLE_TO_APP_ROLE",
    "PERMISSION_MAP",
    "ActorContext",
    "AppRole",
    "Permission",
    "get_actor_context",
    "require_permission",
]
