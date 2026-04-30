from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import EventType
from app.models.base import utcnow
from app.models.event import Event
from app.services.cost_tracking import CostTracker
from app.services.llm_telemetry import telemetry_failure_count
from app.services.metrics import get_metrics_output

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint."""
    metrics_output = get_metrics_output()
    if metrics_output is None:
        raise HTTPException(status_code=501, detail="Prometheus_client is not installed.")
    body, content_type = metrics_output
    return Response(content=body, media_type=content_type)


@router.get("/api/admin/costs")
def get_costs(group_by: str = "task", db: Session = Depends(get_db)) -> list[dict]:
    """LLM cost aggregation."""
    return CostTracker(db).get_costs(group_by=group_by)


@router.get("/api/metrics/llm-calls")
def get_llm_call_metrics(
    purpose: str | None = None,
    since_minutes: int = 60,
    db: Session = Depends(get_db),
) -> dict:
    """Aggregate LLM call event diagnostics from Event.payload_json."""
    window_minutes = max(1, int(since_minutes))
    since = utcnow() - timedelta(minutes=window_minutes)
    rows = db.scalars(
        select(Event)
        .where(Event.event_type == EventType.LLM_CALL)
        .where(Event.created_at >= since)
        .order_by(Event.created_at.asc())
    ).all()

    events: list[Event] = []
    for event in rows:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        if purpose and payload.get("purpose") != purpose:
            continue
        events.append(event)

    by_purpose: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fallback_steps: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    cache_hit_event_ids = _probable_cache_hit_event_ids(events)

    for event in events:
        payload = dict(event.payload_json) if isinstance(event.payload_json, dict) else {}
        payload["_event_id"] = event.id
        event_purpose = str(payload.get("purpose") or "unknown")
        by_purpose[event_purpose].append(payload)
        fallback_steps[str(int(payload.get("fallback_step") or 0))] += 1
        error_type = payload.get("error_type")
        if error_type:
            errors[str(error_type)] += 1

    return {
        "since_minutes": window_minutes,
        "telemetry_failure_count": telemetry_failure_count(),
        "by_purpose": {
            key: _purpose_stats(payloads, cache_hit_event_ids=cache_hit_event_ids)
            for key, payloads in sorted(by_purpose.items())
        },
        "fallback_step_distribution": dict(sorted(fallback_steps.items())),
        "error_type_distribution": dict(sorted(errors.items())),
    }


def _purpose_stats(payloads: list[dict[str, Any]], *, cache_hit_event_ids: set[str]) -> dict[str, Any]:
    latencies = sorted(int(payload.get("latency_ms") or 0) for payload in payloads)
    successes = sum(1 for payload in payloads if payload.get("success") is True)
    indexed_hits = sum(1 for payload in payloads if str(payload.get("_event_id") or "") in cache_hit_event_ids)
    n = len(payloads)
    return {
        "n": n,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "success_rate": round(successes / n, 4) if n else 0.0,
        "cache_hit_pct": round(indexed_hits / n, 4) if n else 0.0,
    }


def _percentile(sorted_values: list[int], percentile: int) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (percentile / 100)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return int(round(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction))


def _probable_cache_hit_event_ids(events: list[Event]) -> set[str]:
    first_by_key: dict[tuple[str | None, str], int] = {}
    hits: set[str] = set()
    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        fingerprint = payload.get("prompt_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        latency = int(payload.get("latency_ms") or 0)
        key = (event.task_id, fingerprint)
        first_latency = first_by_key.get(key)
        if first_latency is None:
            first_by_key[key] = latency
            continue
        if latency < first_latency / 4:
            hits.add(event.id)
    return hits
