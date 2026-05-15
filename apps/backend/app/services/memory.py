from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

import httpx
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import EventType, TaskStatus
from app.models.event import Event
from app.models.memory import AgentMemory, MemoryItem, MemorySettings
from app.models.task import Task
from app.schemas.memory import MemoryItemCreate, MemoryItemUpdate, MemorySettingsUpdate

MEMORY_SETTINGS_ID = "default"
GATE_MEMORY_KIND = "gate_failure_resolution"
PENDING_RESOLUTION = "Pending resolution: linked task has not reached a resolved state yet."


def _engine_is_sqlite(db: Session) -> bool:
    """Return True when the session's bound engine is SQLite.

    Used by T-LEARNING-LOOP-V1 paths that index into the SQLite-only
    FTS5 virtual table; the same paths must short-circuit on Postgres
    to avoid aborting the outer transaction.
    """
    try:
        return db.bind.dialect.name == "sqlite"  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class MemoryCandidate:
    scope: str
    kind: str
    observation: str
    resolution: str
    confidence: float = 1.0


@dataclass(frozen=True)
class JudgeResult:
    should_store: bool
    scores: dict[str, float]
    decision_reason: str
    candidate: MemoryCandidate


def list_memory_items(db: Session, search: str | None = None) -> list[MemoryItem]:
    stmt = select(MemoryItem)
    normalized = (search or "").strip()
    if normalized:
        pattern = f"%{normalized.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(MemoryItem.title).like(pattern),
                func.lower(MemoryItem.body).like(pattern),
                func.lower(MemoryItem.topic).like(pattern),
            )
        )
    stmt = stmt.order_by(MemoryItem.updated_at.desc())
    return list(db.scalars(stmt))


def create_memory_item(db: Session, payload: MemoryItemCreate) -> MemoryItem:
    item = MemoryItem(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_memory_item(db: Session, item_id: str, payload: MemoryItemUpdate) -> MemoryItem:
    item = db.get(MemoryItem, item_id)
    if item is None:
        raise LookupError(f"Memory item not found: {item_id}")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return item


def delete_memory_item(db: Session, item_id: str) -> None:
    item = db.get(MemoryItem, item_id)
    if item is None:
        raise LookupError(f"Memory item not found: {item_id}")

    db.delete(item)
    db.commit()


def get_memory_settings(db: Session) -> MemorySettings:
    memory_settings = db.get(MemorySettings, MEMORY_SETTINGS_ID)
    if memory_settings is None:
        memory_settings = MemorySettings(id=MEMORY_SETTINGS_ID)
        db.add(memory_settings)
        db.commit()
        db.refresh(memory_settings)
    return memory_settings


def update_memory_settings(db: Session, payload: MemorySettingsUpdate) -> MemorySettings:
    memory_settings = get_memory_settings(db)

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(memory_settings, key, value)

    db.commit()
    db.refresh(memory_settings)
    return memory_settings


class MemoryService:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        self._ensure_fts_table()

    # ----- WRITE PATH -----------------------------------------------------

    def maybe_record(
        self,
        *,
        observation_text: str,
        resolution_text: str | None,
        scope: str,
        kind: str,
        provenance_event_id: str | None = None,
        provenance_task_id: str | None = None,
        skip_judge: bool = False,
        confidence: float = 1.0,
    ) -> AgentMemory | None:
        if not bool(getattr(self.settings, "memory_enabled", True)):
            return None

        observation = _clean_text(observation_text, limit=4000)
        resolution = _clean_text(resolution_text or PENDING_RESOLUTION, limit=2000)
        if not self._cheap_prefilter(observation):
            return None

        if skip_judge:
            judged = JudgeResult(
                should_store=True,
                scores={"bootstrap": 1.0},
                decision_reason="judge skipped",
                candidate=MemoryCandidate(
                    scope=scope,
                    kind=kind,
                    observation=_clean_text(observation, limit=2000),
                    resolution=resolution,
                    confidence=confidence,
                ),
            )
        else:
            judged = self._call_judge(
                observation=observation,
                resolution=resolution,
                scope=scope,
                kind=kind,
                confidence=confidence,
            )
        if not judged.should_store:
            return None

        candidate = judged.candidate
        similar = self._find_similar(candidate.observation, scope=scope, top_k=3)
        threshold = float(getattr(self.settings, "memory_dedup_threshold", 0.85))
        if similar and self._is_near_duplicate(similar[0], candidate, threshold=threshold):
            return self._merge(similar[0], candidate)
        return self._insert(
            candidate,
            provenance_event_id=provenance_event_id,
            provenance_task_id=provenance_task_id,
        )

    def record_semantic_review_findings(
        self,
        *,
        task: Task,
        review_payload: dict,
        provenance_event_id: str | None = None,
    ) -> int:
        """Persist grounded high/medium semantic-review findings.

        Semantic review findings are failure observations, not success
        facts. Store them in the same Learning Loop pool as compile,
        acceptance, and must-touch failures so later planner/codegen
        prompts can retrieve them for the same task family.

        Returns the number of memory entries created. Drops low-severity
        findings so advisory noise does not dominate future prompts.
        """
        if not bool(getattr(self.settings, "memory_enabled", True)):
            return 0
        from app.services.failure_classifier import detect_task_family

        findings = review_payload.get("findings") or []
        if not findings:
            return 0
        plan_json = task.plan_json if isinstance(getattr(task, "plan_json", None), dict) else {}
        task_family = detect_task_family(
            request_text=getattr(task, "request_text", "") or "",
            plan_json=plan_json,
        )
        completeness = int(review_payload.get("completeness_pct") or 0)
        high_count = int(review_payload.get("high_severity_count") or 0)
        recorded = 0
        for f in findings:
            severity = (f.get("severity") or "").lower()
            if severity not in ("high", "medium"):
                continue  # low = advisory, don't pollute memory
            file = (f.get("file") or "").strip()
            description = (f.get("description") or "").strip()
            suggested = (f.get("suggested_fix") or "").strip()
            category = (f.get("category") or "general").strip()
            if not description:
                continue
            line_start = f.get("line_start", 0)
            line_end = f.get("line_end", 0)
            observation = (
                f"semantic_review {severity}/{category} in {file}:"
                f"{line_start}-{line_end}: "
                f"{description}"
            )
            lesson = (
                "A previous same-family patch passed earlier deterministic "
                "gates but semantic_review found an implementation gap. "
                f"Finding: {description}. "
                + (f"Reviewer suggested: {suggested}. " if suggested else "")
                + "Future planner/codegen should treat this as a quality "
                "checklist item and implement the behavior before approval."
            )
            failure_class = f"semantic_review_{_slug(category)}"[:64]
            mem = self.write_failure_observation(
                failure_class=failure_class or "semantic_review_quality_gap",
                scope="review:semantic",
                observation_text=observation,
                lesson=lesson,
                task_family=task_family,
                provenance_event_id=provenance_event_id,
                provenance_task_id=task.id,
                # Skip judge for high-severity — they're always worth
                # remembering; medium goes through the normal judge.
                trust_level="auto_classified",
                prompt_eligible=["planner_warning", "codegen_warning"],
                evidence_refs={
                    "task_id": task.id,
                    "task_family": task_family,
                    "semantic_review": {
                        "completeness_pct": completeness,
                        "high_severity_count": high_count,
                        "provider_name": review_payload.get("provider_name"),
                        "status": review_payload.get("status"),
                    },
                    "finding": {
                        "file": file,
                        "line_start": line_start,
                        "line_end": line_end,
                        "severity": severity,
                        "category": category,
                        "description": description,
                        "evidence_quote": (f.get("evidence_quote") or "")[:1000],
                        "suggested_fix": suggested,
                    },
                },
                confidence=0.9 if severity == "high" else 0.75,
            )
            if mem is not None:
                recorded += 1
        return recorded

    def maybe_record_gate_event(self, *, event: Event, task: Task | None = None) -> AgentMemory | None:
        event_type = event.event_type
        if event_type not in {
            EventType.REVIEW_FAILED,
            EventType.COMPILE_FAILED,
            EventType.FAILURE_DIAGNOSIS_GENERATED,
            EventType.TOOL_FAILED,
            EventType.TOOL_TIMED_OUT,
        }:
            return None

        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        payload_text = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
        observation = "\n".join(
            part
            for part in (
                f"{_event_type_value(event_type)} {event.tool_name or ''}".strip(),
                event.message or "",
                payload_text[:2500],
            )
            if part
        )
        resolution = None
        if event_type == EventType.FAILURE_DIAGNOSIS_GENERATED:
            resolution = str(payload.get("likely_fix") or payload.get("root_cause") or "").strip() or None
        return self.maybe_record(
            observation_text=observation,
            resolution_text=resolution,
            scope=self.scope_for_event(event),
            kind=GATE_MEMORY_KIND,
            provenance_event_id=event.id,
            provenance_task_id=task.id if task is not None else event.task_id,
        )

    def _cheap_prefilter(self, text_value: str) -> bool:
        normalized = text_value.strip()
        if len(normalized) < 30:
            return False
        if len(normalized) > 4000:
            return False
        return True

    def _call_judge(
        self,
        *,
        observation: str,
        resolution: str,
        scope: str,
        kind: str,
        confidence: float,
    ) -> JudgeResult:
        fallback = self._heuristic_judge(
            observation=observation,
            resolution=resolution,
            scope=scope,
            kind=kind,
            confidence=confidence,
        )
        provider = str(getattr(self.settings, "memory_judge_provider", "minimax") or "").lower()
        if provider in {"", "mock", "none"}:
            return fallback
        if provider == "minimax" and not getattr(self.settings, "minimax_api_key", None):
            return fallback
        if provider == "deepseek" and not getattr(self.settings, "deepseek_api_key", None):
            return fallback

        prompt = _build_judge_prompt(
            observation=observation,
            resolution=resolution,
            scope=scope,
            kind=kind,
        )
        last_error = ""
        for attempt in range(2):
            try:
                content = self._call_judge_provider(provider=provider, prompt=prompt)
                return self._parse_judge_result(
                    content,
                    scope=scope,
                    kind=kind,
                    fallback=fallback,
                    confidence=confidence,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                prompt += "\n\nReturn valid JSON only. No markdown."
                continue
        return JudgeResult(
            should_store=fallback.should_store,
            scores={**fallback.scores, "judge_error": 1.0},
            decision_reason=f"judge fallback after error: {last_error[:120]}",
            candidate=fallback.candidate,
        )

    # ----- FAILURE-OBSERVATION WRITE PATH (T-LEARNING-LOOP-V1) -----------
    #
    # Failure observations bypass the LLM judge and the verified-success
    # promote gate so the failure pool can grow on every terminal pipeline
    # failure. Hard rules enforced here:
    #   1. memory_kind is locked to 'failure_observation' for this path —
    #      callers cannot mislabel a failure as success_fact.
    #   2. The row is immediately usable (last_used_at = now, usage_count
    #      stays at 0) so `query()` returns it without waiting for the
    #      `promote_pending()` gate.
    #   3. Trust level defaults to 'auto_classified' — surfaces in
    #      `attach_provenance_lines` so the downstream agent knows it's
    #      a rule-classified observation, not a human-confirmed lesson.
    #   4. `prompt_eligible` is a whitelist of section contexts allowed
    #      to consume this row. Retrieval layer must respect it.
    #
    # No judge call — judges are for filtering noise out of success
    # observations. A failure that actually fired a gate is by
    # construction signal, not noise.

    def write_failure_observation(
        self,
        *,
        failure_class: str,
        scope: str,
        observation_text: str,
        lesson: str,
        task_family: str | None = None,
        provenance_task_id: str | None = None,
        provenance_event_id: str | None = None,
        trust_level: str = "auto_classified",
        prompt_eligible: list[str] | None = None,
        evidence_refs: dict | None = None,
        confidence: float = 1.0,
    ) -> AgentMemory | None:
        """Write a failure_observation row directly to agent_memory.

        Returns the persisted row, or None if memory writes are
        globally disabled. Bypasses LLM judge and promote-pending
        gate — failure facts go in immediately and are queryable on
        the next read.
        """
        if not bool(getattr(self.settings, "memory_enabled", True)):
            return None

        if not failure_class.strip():
            raise ValueError("failure_class is required for failure_observation rows")
        if trust_level not in ("verified", "human_confirmed", "auto_classified"):
            raise ValueError(
                f"trust_level must be one of "
                f"verified|human_confirmed|auto_classified; got {trust_level!r}"
            )

        observation = _clean_text(observation_text, limit=2000)
        # Resolution is intentionally the lesson text. The retrieval
        # layer renders both `observation` (what happened) and
        # `resolution` (the lesson learned) in `attach_provenance_lines`.
        # We deliberately store the lesson as plain warning prose, not
        # as a fix recipe — see classifier docstrings for the discipline.
        resolution = _clean_text(lesson, limit=2000) or "(no lesson recorded)"

        row = AgentMemory(
            scope=scope,
            key=_slug(failure_class)[:256] or failure_class[:256],
            kind=GATE_MEMORY_KIND,
            memory_kind="failure_observation",
            failure_class=failure_class[:64],
            task_family=(task_family or "")[:64] or None,
            trust_level=trust_level,
            prompt_eligible=list(prompt_eligible or ["planner_warning"]),
            evidence_refs=dict(evidence_refs or {}),
            observation=observation,
            resolution=resolution,
            provenance_event_id=provenance_event_id,
            provenance_task_id=provenance_task_id,
            confidence=float(confidence),
            last_used_at=datetime.now(timezone.utc),  # bypass promote gate
            usage_count=0,
        )
        self.db.add(row)
        self.db.flush()
        # Index into FTS5 so the text-hint query path can find the row.
        # The FTS5 virtual table is SQLite-only; on Postgres the
        # query path falls back to structured (scope/kind/family)
        # filters and the to_tsvector trigger on the main table.
        if _engine_is_sqlite(self.db):
            # Wrap in a SAVEPOINT so an FTS5 failure cannot abort the
            # outer transaction (which would prevent the row from
            # being committed).
            try:
                with self.db.begin_nested():
                    self.db.execute(
                        text(
                            "INSERT INTO agent_memory_fts(rowid, observation, resolution, scope) "
                            "VALUES (:rowid, :observation, :resolution, :scope)"
                        ),
                        {
                            "rowid": row.id,
                            "observation": observation,
                            "resolution": resolution,
                            "scope": scope,
                        },
                    )
            except Exception:
                # FTS index errors are non-fatal — the row is still
                # queryable via the structured fallback path.
                pass
        return row

    # ----- READ PATH ------------------------------------------------------

    def query(
        self,
        *,
        scope: str,
        kind: str | None = None,
        text_hint: str | None = None,
        top_n: int = 5,
        memory_kind: str | None = None,
        task_family: str | None = None,
        prompt_context: str | None = None,
    ) -> list[AgentMemory]:
        """Read agent_memory rows.

        Backward-compat: when ``memory_kind`` is None, this method
        behaves exactly like before — all kinds returned, FTS5 first,
        last_used_at fallback. v16.2.1+ callers that want failure
        observations specifically pass ``memory_kind='failure_observation'``.

        Filtering:
          - ``scope``: required (every memory row is scoped).
          - ``kind``: legacy GATE_MEMORY_KIND filter, still honored.
          - ``memory_kind``: new T-LEARNING-LOOP-V1 axis
            ('success_fact' | 'failure_observation' | ...).
          - ``task_family``: new — only rows tagged for this family.
          - ``prompt_context``: new — drop rows whose
            ``prompt_eligible`` whitelist excludes this context.
          - ``text_hint``: FTS5 ranking signal.
        """
        if not bool(getattr(self.settings, "memory_enabled", True)):
            return []
        top_n = max(1, min(int(top_n or 5), 20))
        memories = self._query_fts(scope=scope, kind=kind, text_hint=text_hint, top_n=top_n * 3)
        if not memories:
            stmt = select(AgentMemory).where(
                AgentMemory.scope == scope,
                AgentMemory.last_used_at.is_not(None),
            )
            if kind:
                stmt = stmt.where(AgentMemory.kind == kind)
            stmt = stmt.order_by(
                AgentMemory.usage_count.desc(),
                AgentMemory.last_used_at.desc(),
                AgentMemory.created_at.desc(),
            ).limit(top_n * 3)
            memories = list(self.db.scalars(stmt))

        # T-LEARNING-LOOP-V1 post-filter on the new axes. Done in Python
        # rather than SQL because (a) the new columns are nullable on
        # legacy rows, (b) FTS5 doesn't index them, and (c) the
        # candidate list is already small.
        if memory_kind is not None:
            memories = [m for m in memories if (m.memory_kind or "success_fact") == memory_kind]
        if task_family is not None:
            memories = [m for m in memories if (m.task_family or "") == task_family]
        if prompt_context is not None:
            def _allowed(m: AgentMemory) -> bool:
                allowed_list = m.prompt_eligible
                # Rows with no whitelist default-allow only the broadest
                # planner_warning context — never codegen_warning. This
                # prevents legacy rows (which predate the whitelist)
                # from leaking into codegen prompts.
                if not allowed_list:
                    return prompt_context == "planner_warning"
                return prompt_context in allowed_list
            memories = [m for m in memories if _allowed(m)]

        memories = memories[:top_n]
        now = datetime.now(timezone.utc)
        for memory in memories:
            memory.usage_count = int(memory.usage_count or 0) + 1
            memory.last_used_at = now
        if memories:
            self.db.flush()
        return memories

    def attach_provenance_lines(self, memories: list[AgentMemory]) -> str:
        blocks: list[str] = []
        for memory in memories:
            task = memory.provenance_task_id or "unknown"
            blocks.extend(
                [
                    (
                        f"[memory:{memory.kind} / scope:{memory.scope} / "
                        f"used {int(memory.usage_count or 0)}x / "
                        f"confidence {float(memory.confidence or 0):.1f} / from task {task}]"
                    ),
                    f"Observation: {_single_line(memory.observation)}",
                    f"Resolution: {_single_line(memory.resolution)}",
                ]
            )
        return "\n".join(blocks)

    # ----- PROMOTION GUARD -----------------------------------------------

    def promote_pending(self, max_age_hours: int = 24, task_id: str | None = None) -> int:
        if not bool(getattr(self.settings, "memory_enabled", True)):
            return 0
        stmt = select(AgentMemory).where(AgentMemory.last_used_at.is_(None))
        if task_id:
            stmt = stmt.where(AgentMemory.provenance_task_id == task_id)
        promoted = 0
        now = datetime.now(timezone.utc)
        max_age = timedelta(hours=max_age_hours)
        early_slop = timedelta(hours=1)
        for memory in self.db.scalars(stmt).all():
            if not memory.provenance_task_id:
                continue
            task = self.db.get(Task, memory.provenance_task_id)
            if task is None:
                continue
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.AWAITING_APPROVAL}:
                continue
            created_at = _naive_utc(memory.created_at)
            updated_at = _naive_utc(task.updated_at or task.created_at)
            if created_at and updated_at:
                if updated_at < created_at - early_slop:
                    continue
                if updated_at > created_at + max_age:
                    continue
            if _is_pending_resolution(memory.resolution):
                memory.resolution = self._resolution_from_task(task)
            memory.last_used_at = now
            promoted += 1
            self._upsert_fts(memory)
        if promoted:
            self.db.flush()
        return promoted

    # ----- Internals ------------------------------------------------------

    @staticmethod
    def scope_for_event(event: Event) -> str:
        event_type = event.event_type
        if event_type == EventType.COMPILE_FAILED:
            return "gate:compile_gate"
        if event_type == EventType.FAILURE_DIAGNOSIS_GENERATED:
            return "gate:failure_diagnosis"
        tool = (event.tool_name or "").strip().split(".")[0]
        if event_type == EventType.REVIEW_FAILED and tool:
            return f"gate:{_slug(tool)}"
        if event_type in (EventType.TOOL_FAILED, EventType.TOOL_TIMED_OUT) and tool:
            return f"tool:{_slug(tool)}"
        return f"gate:{_slug(_event_type_value(event_type))}"

    def _heuristic_judge(
        self,
        *,
        observation: str,
        resolution: str,
        scope: str,
        kind: str,
        confidence: float,
    ) -> JudgeResult:
        candidate = MemoryCandidate(
            scope=scope,
            kind=kind,
            observation=_clean_text(observation, limit=2000),
            resolution=_clean_text(resolution, limit=2000),
            confidence=confidence,
        )
        return JudgeResult(
            should_store=True,
            scores={"persistence": 0.7, "structure": 0.7, "personalization": 0.0},
            decision_reason="heuristic fallback",
            candidate=candidate,
        )

    def _call_judge_provider(self, *, provider: str, prompt: str) -> str:
        timeout = float(getattr(self.settings, "memory_judge_timeout_seconds", 30) or 30)
        model = str(getattr(self.settings, "memory_judge_model", "MiniMax-M2.7") or "MiniMax-M2.7")
        if provider == "minimax":
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                    headers={
                        "Authorization": f"Bearer {self.settings.minimax_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "Return only JSON for memory triage."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                return _extract_chat_content(response.json())
        if provider == "deepseek":
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.deepseek_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "Return only JSON for memory triage."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                return _extract_chat_content(response.json())
        raise ValueError(f"Unsupported memory judge provider: {provider}")

    def _parse_judge_result(
        self,
        content: str,
        *,
        scope: str,
        kind: str,
        fallback: JudgeResult,
        confidence: float,
    ) -> JudgeResult:
        data = _json_object_from_text(content)
        should_store = bool(data.get("should_store"))
        raw_scores = data.get("scores")
        scores: dict[str, float] = {}
        if isinstance(raw_scores, dict):
            for key, value in raw_scores.items():
                try:
                    scores[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        candidate_payload = data.get("candidate")
        if not isinstance(candidate_payload, dict):
            candidate_payload = {}
        observation = str(candidate_payload.get("observation") or fallback.candidate.observation)
        resolution = str(candidate_payload.get("resolution") or fallback.candidate.resolution)
        candidate = MemoryCandidate(
            scope=scope,
            kind=str(candidate_payload.get("kind") or kind),
            observation=_clean_text(observation, limit=2000),
            resolution=_clean_text(resolution, limit=2000),
            confidence=confidence,
        )
        return JudgeResult(
            should_store=should_store,
            scores=scores,
            decision_reason=str(data.get("decision_reason") or ""),
            candidate=candidate,
        )

    def _find_similar(self, observation: str, *, scope: str, top_k: int) -> list[AgentMemory]:
        tokens = _fts_tokens(observation)
        if not tokens:
            return []
        # Phase B (2026-05-11): dialect dispatch + savepoint. Postgres
        # poisons the parent transaction on any SQL error, so a failing
        # FTS5 MATCH (FTS5 is SQLite-only syntax) would cascade-abort
        # every subsequent INSERT in the same task. Wrap in SAVEPOINT
        # and use ILIKE on Postgres.
        bind = getattr(self.db, "bind", None) or self.db.get_bind()
        dialect_name = bind.dialect.name if bind and hasattr(bind, "dialect") else ""
        if dialect_name == "postgresql":
            like_pattern = "%" + observation[:100].replace("%", "").replace("_", "") + "%"
            try:
                rows = self.db.execute(
                    text(
                        """
                        SELECT m.*
                        FROM agent_memory AS m
                        WHERE m.scope = :scope
                          AND (m.observation ILIKE :q OR m.resolution ILIKE :q)
                        LIMIT :limit
                        """
                    ),
                    {"q": like_pattern, "scope": scope, "limit": int(top_k)},
                ).mappings().all()
            except Exception:  # noqa: BLE001
                return []
            out: list[AgentMemory] = []
            for row in rows:
                memory = self.db.get(AgentMemory, row["id"])
                if memory is not None:
                    out.append(memory)
            return out
        savepoint = None
        try:
            savepoint = self.db.begin_nested()
            rows = self.db.execute(
                text(
                    """
                    SELECT m.*
                    FROM agent_memory_fts
                    JOIN agent_memory AS m ON m.id = agent_memory_fts.memory_id
                    WHERE agent_memory_fts MATCH :query
                      AND m.scope = :scope
                    ORDER BY bm25(agent_memory_fts)
                    LIMIT :limit
                    """
                ),
                {"query": " OR ".join(tokens[:8]), "scope": scope, "limit": int(top_k)},
            ).mappings().all()
            savepoint.commit()
        except Exception:  # noqa: BLE001
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
            return []
        out: list[AgentMemory] = []
        for row in rows:
            memory = self.db.get(AgentMemory, row["id"])
            if memory is not None:
                out.append(memory)
        return out

    def _is_near_duplicate(
        self,
        existing: AgentMemory,
        candidate: MemoryCandidate,
        *,
        threshold: float,
    ) -> bool:
        left = f"{existing.observation}\n{existing.resolution}".lower()
        right = f"{candidate.observation}\n{candidate.resolution}".lower()
        return SequenceMatcher(None, left, right).ratio() >= threshold

    def _merge(self, existing: AgentMemory, candidate: MemoryCandidate) -> AgentMemory:
        if _is_pending_resolution(existing.resolution) and not _is_pending_resolution(candidate.resolution):
            existing.resolution = candidate.resolution
        existing.confidence = max(float(existing.confidence or 0), float(candidate.confidence or 0))
        existing.key = self._memory_key(candidate)
        self.db.flush()
        self._upsert_fts(existing)
        return existing

    def _insert(
        self,
        candidate: MemoryCandidate,
        *,
        provenance_event_id: str | None,
        provenance_task_id: str | None,
    ) -> AgentMemory:
        memory = AgentMemory(
            scope=candidate.scope,
            key=self._memory_key(candidate),
            kind=candidate.kind,
            observation=candidate.observation,
            resolution=candidate.resolution,
            provenance_event_id=provenance_event_id,
            provenance_task_id=provenance_task_id,
            confidence=candidate.confidence,
        )
        self.db.add(memory)
        self.db.flush()
        self._upsert_fts(memory)
        return memory

    def _query_fts(
        self,
        *,
        scope: str,
        kind: str | None,
        text_hint: str | None,
        top_n: int,
    ) -> list[AgentMemory]:
        tokens = _fts_tokens(text_hint or "")
        if not tokens:
            return []
        kind_sql = "AND m.kind = :kind" if kind else ""
        params: dict[str, Any] = {
            "query": " OR ".join(tokens[:10]),
            "scope": scope,
            "limit": int(top_n),
        }
        if kind:
            params["kind"] = kind
        # Phase B (2026-05-11): same dialect dispatch as _find_similar.
        bind = getattr(self.db, "bind", None) or self.db.get_bind()
        dialect_name = bind.dialect.name if bind and hasattr(bind, "dialect") else ""
        if dialect_name == "postgresql":
            like_pattern = "%" + (text_hint or "")[:100].replace("%", "").replace("_", "") + "%"
            pg_params: dict[str, Any] = {
                "q": like_pattern,
                "scope": scope,
                "limit": int(top_n),
            }
            kind_sql_pg = "AND m.kind = :kind" if kind else ""
            if kind:
                pg_params["kind"] = kind
            try:
                rows = self.db.execute(
                    text(
                        f"""
                        SELECT m.id
                        FROM agent_memory AS m
                        WHERE m.scope = :scope
                          AND m.last_used_at IS NOT NULL
                          AND (m.observation ILIKE :q OR m.resolution ILIKE :q)
                          {kind_sql_pg}
                        ORDER BY m.usage_count DESC, m.last_used_at DESC
                        LIMIT :limit
                        """
                    ),
                    pg_params,
                ).mappings().all()
            except Exception:  # noqa: BLE001
                return []
            memories: list[AgentMemory] = []
            for row in rows:
                memory = self.db.get(AgentMemory, row["id"])
                if memory is not None:
                    memories.append(memory)
            return memories
        savepoint = None
        try:
            savepoint = self.db.begin_nested()
            rows = self.db.execute(
                text(
                    f"""
                    SELECT m.id
                    FROM agent_memory_fts
                    JOIN agent_memory AS m ON m.id = agent_memory_fts.memory_id
                    WHERE agent_memory_fts MATCH :query
                      AND m.scope = :scope
                      AND m.last_used_at IS NOT NULL
                      {kind_sql}
                    ORDER BY bm25(agent_memory_fts), m.usage_count DESC, m.last_used_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
            savepoint.commit()
        except Exception:  # noqa: BLE001
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
            return []
        memories: list[AgentMemory] = []
        for row in rows:
            memory = self.db.get(AgentMemory, row["id"])
            if memory is not None:
                memories.append(memory)
        return memories

    def _resolution_from_task(self, task: Task) -> str:
        latest = task.latest_result_json if isinstance(task.latest_result_json, dict) else {}
        result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
        summary = (
            result.get("summary")
            or latest.get("message")
            or latest.get("summary")
            or "Task reached a resolved state after this gate failure."
        )
        files = result.get("files_changed") or latest.get("files_changed")
        file_text = ""
        if isinstance(files, list) and files:
            file_text = " Files changed: " + ", ".join(str(path) for path in files[:8]) + "."
        diff = str(result.get("diff") or latest.get("diff") or "")
        diff_text = f" Diff excerpt: {_single_line(diff[:500])}" if diff else ""
        return _clean_text(f"{summary}{file_text}{diff_text}", limit=2000)

    def _memory_key(self, candidate: MemoryCandidate) -> str:
        normalized = "\n".join(
            [
                candidate.scope.strip().lower(),
                candidate.kind.strip().lower(),
                _single_line(candidate.observation).lower(),
                _single_line(candidate.resolution).lower(),
            ]
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _ensure_fts_table(self) -> None:
        if not self._is_sqlite():
            return
        try:
            self.db.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                        memory_id UNINDEXED,
                        scope UNINDEXED,
                        kind UNINDEXED,
                        observation,
                        resolution,
                        tokenize = 'porter unicode61 remove_diacritics 2'
                    )
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return

    def _upsert_fts(self, memory: AgentMemory) -> None:
        if not self._is_sqlite():
            return
        try:
            self.db.execute(
                text("DELETE FROM agent_memory_fts WHERE memory_id = :id"),
                {"id": memory.id},
            )
            self.db.execute(
                text(
                    """
                    INSERT INTO agent_memory_fts (
                        memory_id, scope, kind, observation, resolution
                    ) VALUES (
                        :id, :scope, :kind, :observation, :resolution
                    )
                    """
                ),
                {
                    "id": memory.id,
                    "scope": memory.scope,
                    "kind": memory.kind,
                    "observation": memory.observation,
                    "resolution": memory.resolution,
                },
            )
        except Exception:  # noqa: BLE001
            return

    def _is_sqlite(self) -> bool:
        try:
            bind = self.db.get_bind()
            return str(bind.dialect.name).lower() == "sqlite"
        except Exception:  # noqa: BLE001
            return False


def _build_judge_prompt(*, observation: str, resolution: str, scope: str, kind: str) -> str:
    return (
        "Decide whether this gate-failure/resolution pair should be stored as "
        "durable agent memory. Store only reusable engineering patterns, not "
        "one-off noise. Return JSON only with this schema:\n"
        "{"
        "\"should_store\": true|false, "
        "\"scores\": {\"persistence\": 0-1, \"structure\": 0-1, \"personalization\": 0-1}, "
        "\"decision_reason\": \"...\", "
        "\"candidate\": {\"observation\": \"...\", \"resolution\": \"...\", \"kind\": \"...\"}"
        "}\n\n"
        f"scope: {scope}\nkind: {kind}\n"
        f"observation:\n{observation}\n\nresolution:\n{resolution}"
    )


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def _json_object_from_text(content: str) -> dict[str, Any]:
    text_value = (content or "").strip()
    text_value = re.sub(r"^```(?:json)?\s*", "", text_value)
    text_value = re.sub(r"\s*```$", "", text_value).strip()
    match = re.search(r"\{.*\}", text_value, flags=re.DOTALL)
    if match is not None:
        text_value = match.group(0)
    parsed = json.loads(text_value)
    if not isinstance(parsed, dict):
        raise ValueError("judge response was not a JSON object")
    return parsed


def _fts_tokens(value: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_]{3,}", value.lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 12:
            break
    return tokens


def _clean_text(value: object, *, limit: int) -> str:
    text_value = " ".join(str(value or "").strip().split())
    if len(text_value) <= limit:
        return text_value
    return text_value[: max(limit - 3, 1)] + "..."


def _single_line(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _event_type_value(event_type: EventType | str) -> str:
    return event_type.value if hasattr(event_type, "value") else str(event_type)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return slug or "unknown"


def _is_pending_resolution(value: str | None) -> bool:
    normalized = (value or "").strip()
    return not normalized or normalized == PENDING_RESOLUTION


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
