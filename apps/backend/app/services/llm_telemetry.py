from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import EventSource, EventType
from app.models.event import Event
from app.services.cost_tracking import CostTracker


@dataclass
class LlmCall:
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    retry_count: int = 0
    fallback_step: int = 0
    error_type: str | None = None
    prompt_fingerprint: str | None = None
    task_id: str | None = None
    actor_name: str | None = None
    extra: dict[str, Any] | None = None


_TELEMETRY_FAILURE_COUNT = 0


def log_llm_cache_hit(*, provider: str, model: str, purpose: str, usage: dict[str, Any]) -> None:
    """Log prompt-cache hit ratio from an LLM API response's usage block.

    DeepSeek and OpenAI both expose `prompt_cache_hit_tokens` /
    `prompt_cache_miss_tokens` in their /chat/completions usage payload.
    This helper extracts them and emits a single structured log line so
    we can later compute aggregate cache hit-rate per provider/purpose
    without changing schemas or DB writes (P-1 Step 1a — instrumentation
    only, no behavior change).

    Anthropic uses different key names (cache_read_input_tokens /
    cache_creation_input_tokens) — pass usage directly and we'll detect
    whichever shape is present.
    """
    log = logging.getLogger("app.services.llm_telemetry.cache")
    if not isinstance(usage, dict):
        return
    hit = int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or 0
    )
    miss = int(
        usage.get("prompt_cache_miss_tokens")
        or usage.get("cache_creation_input_tokens")
        or 0
    )
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    if hit == 0 and miss == 0 and prompt == 0:
        return  # no cache info available from this provider
    denom = prompt or (hit + miss)
    ratio = (hit / denom) if denom > 0 else 0.0
    log.info(
        "llm_cache.hit_ratio",
        extra={
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "prompt_tokens": prompt,
            "hit_ratio": round(ratio, 3),
        },
    )


def record_llm_call(db: Session, call: LlmCall) -> None:
    """Record LLM accounting and diagnostics without breaking the caller.

    Opens a SIBLING session bound to the caller's engine, writes both rows,
    commits, and closes. This guarantees:

    1. Telemetry commits land regardless of whether the caller commits
       (FastAPI's get_db() yields then closes without commit, which would
       otherwise lose all flushed-but-not-committed telemetry rows).
    2. Telemetry failures cannot poison the caller's transaction
       (separate session = separate transaction state on the same engine).

    Tests using StaticPool sqlite see writes immediately because the pool
    shares one connection between sessions.
    """
    global _TELEMETRY_FAILURE_COUNT
    log = logging.getLogger("app.services.llm_telemetry")
    try:
        bind = db.get_bind()
        sibling = sessionmaker(bind=bind, autoflush=False, autocommit=False, expire_on_commit=False)()
    except Exception as exc:  # noqa: BLE001
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(
            "llm_telemetry.session_setup_failed",
            extra={"purpose": call.purpose, "error_type": type(exc).__name__, "error": str(exc)[:200]},
        )
        return

    try:
        _write_with_session(sibling, call, commit=True)
    finally:
        sibling.close()


def _write_with_session(db: Session, call: LlmCall, *, commit: bool) -> None:
    global _TELEMETRY_FAILURE_COUNT
    log = logging.getLogger("app.services.llm_telemetry")
    try:
        CostTracker(db).record_usage(
            task_id=call.task_id,
            actor_name=call.actor_name,
            provider_name=call.provider,
            model_name=call.model,
            input_tokens=max(0, int(call.input_tokens or 0)),
            output_tokens=max(0, int(call.output_tokens or 0)),
            purpose=call.purpose,
        )
    except Exception as exc:  # noqa: BLE001
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(
            "llm_telemetry.cost_tracking_failed",
            extra={
                "purpose": call.purpose,
                "provider": call.provider,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )

    payload: dict[str, Any] = {
        "purpose": call.purpose,
        "provider": call.provider,
        "model": call.model,
        "input_tokens": max(0, int(call.input_tokens or 0)),
        "output_tokens": max(0, int(call.output_tokens or 0)),
        "latency_ms": max(0, int(call.latency_ms or 0)),
        "success": bool(call.success),
        "retry_count": max(0, int(call.retry_count or 0)),
        "fallback_step": max(0, int(call.fallback_step or 0)),
        "error_type": call.error_type,
        "prompt_fingerprint": call.prompt_fingerprint,
    }
    if call.extra:
        payload.update(call.extra)

    try:
        db.add(
            Event(
                task_id=call.task_id,
                event_type=EventType.LLM_CALL,
                source=EventSource.SYSTEM,
                message=f"LLM call recorded for {call.purpose}.",
                payload_json=payload,
            )
        )
        db.flush()
    except Exception as exc:  # noqa: BLE001
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(
            "llm_telemetry.event_write_failed",
            extra={
                "purpose": call.purpose,
                "provider": call.provider,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )

    if commit:
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            _TELEMETRY_FAILURE_COUNT += 1
            log.warning(
                "llm_telemetry.commit_failed",
                extra={"purpose": call.purpose, "error_type": type(exc).__name__, "error": str(exc)[:200]},
            )
            db.rollback()


def telemetry_failure_count() -> int:
    return _TELEMETRY_FAILURE_COUNT


def reset_telemetry_failure_count_for_tests() -> None:
    global _TELEMETRY_FAILURE_COUNT
    _TELEMETRY_FAILURE_COUNT = 0
