from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class MemoryItem(Base):
    __tablename__ = "memory_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(64), index=True, default="general")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MemorySettings(Base):
    __tablename__ = "memory_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_list: Mapped[str] = mapped_column(Text, default="")
    block_list: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    observation: Mapped[str] = mapped_column(String(2000), nullable=False)
    resolution: Mapped[str] = mapped_column(String(2000), nullable=False)
    provenance_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("event.id"),
        nullable=True,
    )
    provenance_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task.id"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        nullable=False,
    )
    last_used_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # T-LEARNING-LOOP-V1 (2026-05-12): split memory pool by kind so
    # failure observations and success facts can coexist with different
    # write disciplines. Existing rows are migrated as
    # memory_kind='success_fact' / trust_level='verified' (they passed
    # the recompile + symbol_graph + quote_verified gate by construction).
    memory_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="success_fact"
    )
    # Granular failure label set by failure_classifier.py. Populated for
    # memory_kind='failure_observation' only; NULL for success_fact.
    # Examples: 'must_touch_incomplete_diff',
    # 'contract_coverage_unverified', 'compile_repair_timeout',
    # 'provider_partial_diff'.
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Domain bucket the failure was observed in. Used by retrieval at
    # planner time to surface failures from the same task family
    # (android_map_location, python_refactor, ...).
    task_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Trust ladder. 'verified' = automated success gate cleared;
    # 'human_confirmed' = a human signed off (incident review, manual
    # backfill); 'auto_classified' = rule-based classifier wrote it
    # without human confirmation. Surfaces in prompt as a label so the
    # downstream agent never treats auto_classified as authoritative.
    trust_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default="verified"
    )
    # Whitelist of prompt-section contexts allowed to consume this row.
    # JSON array such as ["planner_warning", "codegen_warning",
    # "repair_hint"]. The retrieval layer filters out rows whose
    # allowed list does not include the requesting context, so a
    # failure_observation can never be quoted as a "fix here" hint.
    prompt_eligible: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Structured pointers to the audit trail (task_id, plan_id, diff
    # stats, missing_files, etc.) so a future review can rebuild the
    # full context the row was extracted from without trusting the
    # `observation` text alone.
    evidence_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_agent_memory_scope_kind", "scope", "kind"),
        Index("ix_agent_memory_kind_class", "memory_kind", "failure_class"),
        Index("ix_agent_memory_family", "task_family"),
    )
