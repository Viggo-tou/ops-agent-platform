from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.core.db import Base, SessionLocal, engine, ensure_local_schema  # noqa: E402
from app.core.enums import EventType, TaskStatus  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services.memory import GATE_MEMORY_KIND, MemoryService  # noqa: E402

BOOTSTRAP_CAP = 50
AUDIT_PATH = REPO_ROOT / "tmp" / "bootstrap_memory_audit.md"


@dataclass(frozen=True)
class BootstrapCandidate:
    event_id: str
    task_id: str
    scope: str
    observation: str
    resolution: str


def _event_type_value(event_type: EventType | str) -> str:
    return event_type.value if hasattr(event_type, "value") else str(event_type)


def _clean(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 1)] + "..."


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _task_resolution(task: Task) -> str:
    latest = task.latest_result_json if isinstance(task.latest_result_json, dict) else {}
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    summary = result.get("summary") or latest.get("message") or "Task reached a resolved state."
    files = result.get("files_changed") or latest.get("files_changed")
    file_text = ""
    if isinstance(files, list) and files:
        file_text = " Files changed: " + ", ".join(str(path) for path in files[:8]) + "."
    diff = str(result.get("diff") or latest.get("diff") or "")
    diff_text = f" Diff excerpt: {_clean(diff[:500], limit=500)}" if diff else ""
    return _clean(f"{summary}{file_text}{diff_text}", limit=2000)


def _event_observation(event: Event) -> str:
    payload = event.payload_json if isinstance(event.payload_json, dict) else {}
    payload_text = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
    return _clean(
        "\n".join(
            part
            for part in (
                f"{_event_type_value(event.event_type)} {event.tool_name or ''}".strip(),
                event.message or "",
                payload_text[:2500],
            )
            if part
        ),
        limit=2000,
    )


def collect_candidates(limit: int = BOOTSTRAP_CAP) -> list[BootstrapCandidate]:
    capped = max(0, min(int(limit), BOOTSTRAP_CAP))
    if capped == 0:
        return []
    failure_types = [
        EventType.REVIEW_FAILED,
        EventType.COMPILE_FAILED,
        EventType.FAILURE_DIAGNOSIS_GENERATED,
    ]
    candidates: list[BootstrapCandidate] = []
    with SessionLocal() as db:
        try:
            if "event" not in set(inspect(db.get_bind()).get_table_names()):
                return []
        except Exception:  # noqa: BLE001
            return []
        events = list(
            db.scalars(
                select(Event)
                .where(Event.event_type.in_(failure_types))
                .order_by(Event.created_at.asc())
            )
        )
        for event in events:
            if len(candidates) >= capped:
                break
            if not event.task_id:
                continue
            task = db.get(Task, event.task_id)
            if task is None:
                continue
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.AWAITING_APPROVAL}:
                continue
            event_at = _naive_utc(event.created_at)
            task_updated = _naive_utc(task.updated_at or task.created_at)
            if event_at and task_updated and task_updated > event_at + timedelta(hours=1):
                continue
            resolution = _task_resolution(task)
            observation = _event_observation(event)
            if len(observation) < 30 or len(resolution) < 30:
                continue
            candidates.append(
                BootstrapCandidate(
                    event_id=event.id,
                    task_id=task.id,
                    scope=MemoryService.scope_for_event(event),
                    observation=observation,
                    resolution=resolution,
                )
            )
    return candidates


def write_audit(candidates: list[BootstrapCandidate], *, promoted: bool) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Bootstrap Memory Audit",
        "",
        f"Mode: {'promote' if promoted else 'dry-run'}",
        f"Candidate count: {len(candidates)}",
        f"Cap: {BOOTSTRAP_CAP}",
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"## {index}. {candidate.scope}",
                "",
                f"- task_id: {candidate.task_id}",
                f"- event_id: {candidate.event_id}",
                f"- confidence: 0.5",
                f"- observation: {candidate.observation[:240]}",
                f"- resolution: {candidate.resolution[:240]}",
                "",
            ]
        )
    AUDIT_PATH.write_text("\n".join(lines), encoding="utf-8")


def promote_candidates(candidates: list[BootstrapCandidate]) -> int:
    now = datetime.now(timezone.utc)
    inserted = 0
    with SessionLocal() as db:
        service = MemoryService(db)
        for candidate in candidates:
            memory = service.maybe_record(
                observation_text=candidate.observation,
                resolution_text=candidate.resolution,
                scope=candidate.scope,
                kind=GATE_MEMORY_KIND,
                provenance_event_id=candidate.event_id,
                provenance_task_id=candidate.task_id,
                skip_judge=True,
                confidence=0.5,
            )
            if memory is None:
                continue
            memory.last_used_at = now
            service._upsert_fts(memory)
            inserted += 1
        db.commit()
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap gate-failure memory from historical events.")
    parser.add_argument("--promote", action="store_true", help="Insert approved bootstrap entries.")
    parser.add_argument("--limit", type=int, default=BOOTSTRAP_CAP, help="Maximum candidates to consider, capped at 50.")
    args = parser.parse_args(argv)

    if args.promote:
        Base.metadata.create_all(bind=engine)
        ensure_local_schema()
    candidates = collect_candidates(limit=args.limit)
    inserted = promote_candidates(candidates) if args.promote else 0
    write_audit(candidates, promoted=args.promote)
    print(
        json.dumps(
            {
                "candidates": len(candidates),
                "inserted": inserted,
                "promote": bool(args.promote),
                "audit_path": str(AUDIT_PATH),
                "cap": BOOTSTRAP_CAP,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
