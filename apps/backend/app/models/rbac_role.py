from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ActorRole
from app.models.base import Base, utcnow


class RbacRole(Base):
    __tablename__ = "rbac_role"

    role_key: Mapped[ActorRole] = mapped_column(
        SqlEnum(ActorRole, native_enum=False),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    is_human: Mapped[bool] = mapped_column(Boolean, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
