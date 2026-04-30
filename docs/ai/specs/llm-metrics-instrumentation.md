# T-LLM-METRICS — Instrument every LLM call site (no new table; LlmUsage + Event.payload_json)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P2 — Phase 3.3 calibration prerequisite)
**Priority:** P2 (unblocks data-driven decisions on prompt cache, hybrid RAG fast-path, CLI pool)
**Created:** 2026-04-29

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Make every LLM call across the platform — synthesis, judge, planner,
codegen, semantic translator, CC agent provider, **cards generator** — produce two records:

1. A `LlmUsage` row (existing schema) for $$ accounting (tokens + cost).
2. An `Event` of type `LLM_CALL` whose `payload_json` carries diagnostic
   detail (latency, retry, fallback step, cache hit signal, error type).

We deliberately keep the **schema flat** — no new dedicated metrics
table. `event.payload_json` is JSON and free; aggregations run from
SQL `json_extract`.

## Background — what we have today

```
$ grep -rn "record_usage\|CostTracker(" apps/backend/app/
apps/backend/app/api/metrics.py:26: ...get_costs(group_by=...)
apps/backend/app/services/cost_tracking.py:13: def record_usage(...)
apps/backend/app/tools/gateway.py:656: CostTracker(self.db).record_usage(
```

Only **one** call site writes `LlmUsage`: `tools/gateway.py:656` for
tool runtime. The entire CC agent → synthesis → judge path is invisible
to cost accounting AND to latency observability. This is why we cannot
data-drive Phase 3.3 calibration decisions.

## Why no new table

A previous design proposed `llm_call_metrics` as a separate dedicated
table. Rejected because (a) it duplicates `LlmUsage` for tokens/cost;
(b) most diagnostic fields (retry_count, cache_hit_pct, error_type)
are sparse and structurally varying — JSON in `event.payload_json`
fits without migrations; (c) the existing event sink, ingest API, and
event-listing endpoints already cover it.

## Design

### A. New event type

```python
# apps/backend/app/core/enums.py — add to EventType
LLM_CALL = "llm_call"
```

### B. Tiny helper — single sink for LLM call records

```python
# apps/backend/app/services/llm_telemetry.py  (new file)

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from sqlalchemy.orm import Session

from app.core.enums import EventType, EventSource
from app.models.event import Event
from app.services.cost_tracking import CostTracker


@dataclass
class LlmCall:
    purpose: str            # synthesis | judge | planner | codegen | semantic_translator | cc_agent | cards
    provider: str           # minimax | claude_code | codex | anthropic | openai | deepseek
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    retry_count: int = 0           # 0 if first try succeeded
    fallback_step: int = 0          # 0 = primary, 1 = first fallback, etc.
    error_type: str | None = None   # "timeout" | "rate_limit" | "auth" | "stdin_utf8" | None
    prompt_fingerprint: str | None = None  # 10-char sha256 prefix; used for cache_hit detection
    task_id: str | None = None
    actor_name: str | None = None
    extra: dict[str, Any] | None = None  # provider-specific extras (e.g. judge_modes_used)


_TELEMETRY_FAILURE_COUNT = 0  # process-local counter; surfaced via /api/metrics/llm-calls


def record_llm_call(db: Session, call: LlmCall) -> None:
    """Write LlmUsage + Event in one shot. Failures are non-fatal but VISIBLE:
    incremented in a counter and logged at WARN. Never silent (codex Stage 12
    critique D4 — silent telemetry creates an invisible monitoring gap)."""
    global _TELEMETRY_FAILURE_COUNT
    log = logging.getLogger("app.services.llm_telemetry")

    try:
        CostTracker(db).record_usage(
            task_id=call.task_id,
            actor_name=call.actor_name,
            provider_name=call.provider,
            model_name=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            purpose=call.purpose,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break caller
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(
            "llm_telemetry.cost_tracking_failed",
            extra={"purpose": call.purpose, "provider": call.provider, "error_type": type(exc).__name__, "error": str(exc)[:200]},
        )

    payload = {
        "purpose": call.purpose,
        "provider": call.provider,
        "model": call.model,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "latency_ms": call.latency_ms,
        "success": call.success,
        "retry_count": call.retry_count,
        "fallback_step": call.fallback_step,
        "error_type": call.error_type,
        "prompt_fingerprint": call.prompt_fingerprint,
    }
    if call.extra:
        payload.update(call.extra)

    try:
        db.add(Event(
            task_id=call.task_id,
            event_type=EventType.LLM_CALL,
            event_source=EventSource.SYSTEM,  # confirm enum value exists; otherwise add
            payload_json=payload,
        ))
        db.flush()
    except Exception as exc:  # noqa: BLE001
        _TELEMETRY_FAILURE_COUNT += 1
        log.warning(
            "llm_telemetry.event_write_failed",
            extra={"purpose": call.purpose, "provider": call.provider, "error_type": type(exc).__name__, "error": str(exc)[:200]},
        )


def telemetry_failure_count() -> int:
    """Process-local counter exposed by /api/metrics/llm-calls so dashboards can
    alarm on telemetry-itself-broken (separate failure mode from the calls
    being measured failing)."""
    return _TELEMETRY_FAILURE_COUNT
```

**Visible-error pattern rules** (codex critique D4 incorporated):
- No bare `except Exception: pass` in telemetry. Every catch increments the process-local counter AND logs at WARN with `error_type` + truncated error string.
- The metrics endpoint `/api/metrics/llm-calls` MUST surface `telemetry_failure_count` so dashboards can alarm on "telemetry itself is broken" as a separate failure mode from "the calls being measured failed".
- Tests MUST assert: (a) telemetry failure does not raise to caller; (b) telemetry failure increments the counter; (c) telemetry failure produces a WARN log line. Three small tests, not optional.

### C. Wire all 6 call sites

For each, the pattern is: time the call, catch exceptions for
error_type, build `LlmCall`, hand to `record_llm_call`. Concrete sites
to update:

| Purpose             | File                                                  | Notes                                |
|---------------------|-------------------------------------------------------|--------------------------------------|
| synthesis           | `app/services/knowledge_synthesis.py:KnowledgeSynthesizer.synthesize()` | wrap MiniMax httpx call             |
| judge               | `apps/backend/scripts/run_qa_benchmark.py` judge path | already prints; add db write via REST or skip (CLI script) |
| planner             | `app/services/agent_provider.py` (or equivalent)      | wrap each provider call             |
| codegen             | same provider chain                                   | provider chain step → `fallback_step` |
| semantic_translator | `app/services/semantic_translator.py`                 | MiniMax httpx call                  |
| cc_agent            | `app/services/cc_agentic.py` per-tool-call            | tool calls (Glob/Grep/Read) — log per round at minimum |
| cards               | `app/services/cards.py:CardGenerator.generate()` (added Stage 16) | wrap MiniMax httpx call; one event per card built — useful for "build_cards run took N tokens" reporting |

**Out of scope for this ticket**: judge wiring inside `run_qa_benchmark.py`
because it has no DB session; tracked separately in T-BENCH-JUDGE-METRICS.

### D. Cache-hit detection (cheap heuristic)

`prompt_fingerprint` = `hashlib.sha256(prompt).hexdigest()[:10]`. Two
events within the same `task_id` with identical fingerprints AND the
second has `latency_ms < first/4` is a probable cache hit. Compute
this in the metrics endpoint, NOT at write time.

### E. Aggregation endpoint

```python
# apps/backend/app/api/metrics.py — add

@router.get("/llm-calls")
def get_llm_call_metrics(
    db: Session = Depends(get_db),
    purpose: str | None = None,
    since_minutes: int = 60,
) -> dict:
    """Return per-purpose stats: count, p50/p95 latency, success_rate,
    cache_hit_pct, error_type distribution, fallback_step distribution."""
    ...
```

Keep the SQL simple: load events with `event_type='llm_call'` and
created_at within window, then aggregate in Python. We're not
optimising for huge volumes — benchmark runs produce <1000 events.

## Files to edit

1. `apps/backend/app/core/enums.py` — add `EventType.LLM_CALL`
2. `apps/backend/app/services/llm_telemetry.py` — NEW (~80 LOC)
3. `apps/backend/app/services/knowledge_synthesis.py` — wire synthesis path
4. `apps/backend/app/services/semantic_translator.py` — wire translator path
5. `apps/backend/app/services/agent_provider.py` (or wherever planner/codegen lives) — wire provider chain
6. `apps/backend/app/services/cc_agentic.py` — wire per-call tool log
6b. `apps/backend/app/services/cards.py` — wire CardGenerator.generate() with purpose="cards"
7. `apps/backend/app/api/metrics.py` — add `/llm-calls` endpoint
8. `apps/backend/tests/services/test_llm_telemetry.py` — NEW (5+ tests)
9. `apps/backend/tests/api/test_metrics_llm_calls.py` — NEW (3+ tests)

## Tests

1. `test_record_llm_call_writes_both_llm_usage_and_event` — sanity
2. `test_record_llm_call_swallows_db_errors` — telemetry never throws
3. `test_synthesis_path_records_one_event_per_call` — integration
4. `test_provider_fallback_records_separate_events_with_fallback_step` — chain semantics
5. `test_metrics_llm_calls_aggregates_by_purpose` — endpoint
6. `test_metrics_llm_calls_computes_p95_latency` — endpoint
7. `test_metrics_llm_calls_filters_by_since_minutes` — endpoint

## Acceptance criteria

- `python -m compileall app` clean
- 7+ new tests pass
- Existing tests still green
- Run a smoke benchmark (`--limit 5`); verify `LlmUsage` row count
  jumps from 0 → ≥15 (synthesis + cc_agent + judge per question), and
  `Event` rows with `event_type='llm_call'` appear with non-empty
  `latency_ms` and `provider`.
- `GET /api/metrics/llm-calls?since_minutes=60` returns shape:
  ```json
  {
    "since_minutes": 60,
    "by_purpose": {
      "synthesis": {"n": 5, "p50_ms": 6234, "p95_ms": 9120, "success_rate": 1.0, "cache_hit_pct": 0.0},
      "cc_agent": {"n": 32, "p50_ms": 1180, ...},
      ...
    },
    "fallback_step_distribution": {"0": 35, "1": 2}
  }
  ```

## Out of scope

- Token-level streaming metrics (only request-level)
- Distributed tracing / OpenTelemetry export — events ARE our sink
- Persisting prompt body to event.payload_json (only the 10-char
  fingerprint; PII/cost concern)
- Per-user-quota enforcement — separate ticket
- Judge instrumentation in `run_qa_benchmark.py` (CLI script, no
  DB session) — tracked in T-BENCH-JUDGE-METRICS

## Workflow

```
codex exec --full-auto -C "<worktree>" - < docs/ai/specs/llm-metrics-instrumentation.md
```

Worktree: spawn fresh `D:/项目/ops-worktrees/llm-metrics` on
`feat/llm-metrics`. Independent of evidence-tier-cap; can land in
parallel.

## Why this matters

After this lands we can answer questions we currently guess at:
- "Is the prompt cache actually hitting?" → cache_hit_pct over time
- "Which provider in the chain is the bottleneck?" → fallback_step distribution
- "Should we keep claude_code first in the chain?" → success_rate per provider
- "Is synthesis the bottleneck or CC agent?" → p95 latency by purpose
- "What's our $$ rate per benchmark run?" → existing LlmUsage now actually reflects everything

Until we have these numbers, every Phase 3.x optimization PR is
guessing about what to optimize. This ticket is the prerequisite for
T-PROMPT-CACHE-KEY, T-KB-CLI-POOL, and T-KB-HYBRID-RAG-FAST-PATH.
