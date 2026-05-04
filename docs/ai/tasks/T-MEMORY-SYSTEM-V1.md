# T-MEMORY-SYSTEM-V1 — Phase 5.2 Gate-failure memory (production-shaped v1)

<!-- Effort: ~2-3 weeks -->
<!-- Executor: codex (xhigh) -->
<!-- Stage: 27 -->
<!-- Phase: 5.0-5.2 -->

**Status:** todo (P0 — moat-tier feature per STRATEGY R-3 + roadmap Phase 5.2)
**Priority:** S (project's governance moat depends on this)
**Created:** 2026-05-04
**Branch:** `feat/memory-system-v1` based on `checkpoint/pre-reclassify@be97b03`
**Linked:**
- User-provided design + Claude critique + Codex consult: `tmp/codex-memory-system-review.md`
- Roadmap Phase 5.2 (gate failure → resolution memory)
- STRATEGY R-3 (evidence bundle as central spine)

## Override

Skip session-boundary protocol. Do NOT create session-start tag, do NOT read CLAUDE.md / AGENTS.md / SESSION_HANDOFF / STAGE_LOG.

## Background

User submitted a 6-stage memory design (user-input-centric: heavy-model judge → light-model extract → staging dedup → async write → 3-store layered → reflection). Claude critiqued; Codex consulted; agreed pivot to **gate-failure-centric v1** per Phase 5.2 roadmap.

After reverse-checking for over-design, 3 components were trimmed:
- **DROP** `polarity` column (encode in observation text)
- **DROP** staging area (write-time dedup via FTS5 sufficient at current write rate)
- **DEFER** confidence as filter (record but don't filter in v1)
- **DEFER** stale/regression metrics (5.3 work)

What stays: schema with provenance + FTS5 index + pre-filter + structured-output single judge + promotion-on-resolved + memory-cannot-override-spec read constraint + bootstrap from event table + replay eval harness.

## Goal

When a gate fails (REVIEW_FAILED / COMPILE_FAILED / FAILURE_DIAGNOSIS_GENERATED) AND a subsequent commit fixes it, automatically extract `(observation, resolution)` and store. When codegen runs, retrieve relevant memories by scope, attach with provenance, do NOT let memory override evidence bundle or current spec.

## Design

### Schema (SQLite, additive only)

`apps/backend/app/models/memory.py` (new):

```python
from sqlalchemy import Column, String, Integer, DateTime, Float, ForeignKey, Index
from sqlalchemy.sql import func
from app.models.base import Base

class AgentMemory(Base):
    __tablename__ = "agent_memory"
    id = Column(String(36), primary_key=True)
    scope = Column(String(128), nullable=False, index=True)        # e.g. "gate:evidence_chain", "gate:compile_gate", "repo:handymanapp"
    key = Column(String(256), nullable=False)                       # content hash for natural-key dedup
    kind = Column(String(64), nullable=False)                       # "gate_failure_resolution" | "repo_context"
    observation = Column(String(2000), nullable=False)              # what failed / pattern observed
    resolution = Column(String(2000), nullable=False)               # what fixed it / advice
    provenance_event_id = Column(String(36), nullable=True)         # FK to event row that triggered this memory
    provenance_task_id = Column(String(36), nullable=True)          # FK to task row
    confidence = Column(Float, nullable=False, default=1.0)         # recorded but NOT used as filter in v1
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_agent_memory_scope_kind", "scope", "kind"),)
```

Plus FTS5 virtual table `agent_memory_fts` (mirroring observation + resolution).

Migration in `apps/backend/app/core/db.py:ensure_local_schema()`: add table if not exists. Match existing pattern (e.g., how `llm_usage` was added).

### Module

`apps/backend/app/services/memory.py` (new, ~250 lines):

```python
class MemoryService:
    def __init__(self, db, settings): ...

    # ----- WRITE PATH -----

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
    ) -> AgentMemory | None:
        """Single entry point. Pre-filters cheap junk, calls judge LLM,
        dedups via FTS5, writes if all pass."""
        if not self._cheap_prefilter(observation_text):
            return None
        if skip_judge:
            judged = JudgeResult(should_store=True, scores={"bootstrap": 1.0}, candidate=...)
        else:
            judged = self._call_judge(observation_text, resolution_text, scope)
        if not judged.should_store:
            return None
        # Dedup via FTS5 lookup
        similar = self._find_similar(judged.candidate.observation, scope, top_k=3)
        if similar and self._is_near_duplicate(similar[0], judged.candidate, threshold=0.85):
            return self._merge(similar[0], judged.candidate)
        # Insert
        return self._insert(judged.candidate, provenance_event_id, provenance_task_id)

    def _cheap_prefilter(self, text: str) -> bool:
        if len(text.strip()) < 30: return False
        if len(text.strip()) > 4000: return False
        # Add other regex/heuristic filters later
        return True

    def _call_judge(self, observation, resolution, scope) -> JudgeResult:
        """Single structured-output call. Returns judge decision +
        candidate memory (combined Stage 1+2 from user's design)."""
        # Use MM or DeepSeek per provider chain; structured JSON output
        # Schema: {should_store: bool, scores: {persistence, structure, personalization},
        #          decision_reason: str, candidate: {observation, resolution, kind}?}
        # Validation + 1 retry on JSON malformed
        ...

    # ----- READ PATH -----

    def query(
        self,
        *,
        scope: str,
        kind: str | None = None,
        text_hint: str | None = None,
        top_n: int = 5,
    ) -> list[AgentMemory]:
        """Read API for codegen / planner / reviewer. Returns ranked memories.
        DOES NOT inject into prompt (caller does). DOES NOT override spec."""
        # SQL: scope match + optional kind filter
        # If text_hint: FTS5 rank
        # Rerank by recency × usage_count (simple v1)
        ...

    def attach_provenance_lines(self, memories: list[AgentMemory]) -> str:
        """Render memories as prompt-ready lines with provenance.
        Format:
            [memory:gate_failure_resolution / scope:gate:evidence_chain / used 4x / confidence 1.0 / from task abc123]
            Observation: <text>
            Resolution: <text>
        """
        ...

    # ----- PROMOTION GUARD -----

    def promote_pending(self, max_age_hours: int = 24):
        """Promotes memories whose linked task transitioned to a resolved
        terminal state within window. Memories not linked or not resolved
        stay in 'pending' (NULL last_used_at). Run as scheduled job or
        on-demand after task completion."""
        # SELECT memories WHERE last_used_at IS NULL
        # JOIN task ON memory.provenance_task_id = task.id
        # WHERE task.status IN ('COMPLETED', 'AWAITING_APPROVAL')
        #   AND task.updated_at > memory.created_at - 1h
        #   AND task.updated_at < memory.created_at + max_age_hours
        # SET memory.last_used_at = NOW()  (mark as "promoted")
        ...
```

### Hook into orchestrator

`apps/backend/app/orchestrator/service.py`:

1. **At gate failure** (REVIEW_FAILED, COMPILE_FAILED, FAILURE_DIAGNOSIS_GENERATED events): call `memory_service.maybe_record(observation=event.message + payload, scope=f"gate:{gate_name}", kind="gate_failure_resolution", provenance_event_id=event.id, provenance_task_id=task.id)`. The resolution may not be known yet — store with empty resolution; later promotion fills it from the diff.

2. **At codegen prompt build**: call `memory_service.query(scope="gate:compile_gate", text_hint=current_task.summary, top_n=3)`. Append rendered provenance lines to the prompt with `===== Prior gate failure patterns ===== ... ===== End memory =====` delimiters. Cap memory section to N lines (configurable).

3. **At task COMPLETED**: call `memory_service.promote_pending()` to flag memories linked to this task as resolved.

### Bootstrap (one-time mining script)

`apps/backend/scripts/bootstrap_memory_from_events.py` (new):

- Query event table for `event_type IN ('REVIEW_FAILED', 'COMPILE_FAILED', 'FAILURE_DIAGNOSIS_GENERATED')`
- For each, find the task's subsequent commit (via task.id → file_changed paths) within 1h
- Build `(observation, resolution)` pairs
- Write to memory with `confidence=0.5` (bootstrap quality)
- **Cap 50 entries**
- **Mark all as pending** (NULL last_used_at), require `--promote` flag for explicit human approval before promotion to active

Output: `tmp/bootstrap_memory_audit.md` with per-entry summary; user reviews + approves before second run with `--promote`.

### Read-time constraint (memory cannot override spec)

In codegen prompt: memories appear in their own delimited section AFTER the must_touch_files list. Add a literal instruction in the prompt:

```
Memory section is informational background. If memory contradicts must_touch_files
or change_summary or evidence bundle citations, OBEY THE CURRENT SPEC. Memory
is past observation, not future authority.
```

(This is just prompt hygiene — no code logic to enforce. The constraint is documented.)

### Eval harness (replay)

`apps/backend/tests/services/test_memory_replay.py` (new):

- 5 scenarios from past dogfood failures (P69-19 v1/v2, P69-10, etc.) — synthetic recreation, not live API
- For each scenario:
  - Run codegen prompt construction WITH memory injection (3 memories matching scope)
  - Run codegen prompt construction WITHOUT memory injection
  - Compare prompt content; assert memory injects expected hint patterns
- Mock the actual LLM call; just verify prompt construction differs as expected
- This is a structural test — full LLM-loop replay deferred to Phase 5.3

### Configuration

`apps/backend/app/core/config.py`:

```python
memory_enabled: bool = True
memory_judge_provider: str = "minimax"  # or "deepseek" — uses provider chain
memory_judge_model: str = "MiniMax-M2.7"
memory_top_n_per_query: int = 3
memory_max_lines_in_prompt: int = 30
memory_dedup_threshold: float = 0.85
memory_judge_timeout_seconds: int = 30
```

Env: `OPS_AGENT_MEMORY_*`.

## Files to edit

| File | Change |
|---|---|
| `apps/backend/app/models/memory.py` | NEW (~50 lines) — AgentMemory model |
| `apps/backend/app/services/memory.py` | NEW (~250 lines) — MemoryService |
| `apps/backend/app/core/db.py` | Add table + FTS5 index in ensure_local_schema |
| `apps/backend/app/core/config.py` | Add 7 memory settings |
| `apps/backend/app/orchestrator/service.py` | Hook gate-failure write + codegen-prompt read + on-completion promotion |
| `apps/backend/scripts/bootstrap_memory_from_events.py` | NEW (~100 lines) — bootstrap script |
| `apps/backend/tests/services/test_memory.py` | NEW unit tests for MemoryService |
| `apps/backend/tests/services/test_memory_replay.py` | NEW eval harness |
| `apps/backend/tests/orchestrator/test_orchestrator_memory_*.py` | NEW integration tests |

## Acceptance

1. `compileall` clean
2. Unit tests for MemoryService (≥10 tests covering write/read/dedup/promote/cheap_prefilter)
3. Integration tests for orchestrator hooks (gate failure writes; codegen reads; promotion on completion)
4. Bootstrap script: dry-run on existing event table reports ≤50 candidates with audit doc
5. Eval harness: 5 scenarios pass structural test (memory injection produces different prompt)
6. **Replay benchmark** (manual after merge): rerun P69-19 with memory ON vs OFF; report repair-rounds reduction. Target ≥30% reduction in repair rounds OR ≥10% gate-pass rate increase.
7. `OPS_AGENT_MEMORY_ENABLED=false` cleanly disables (test verified)

## Halt conditions

- Memory write rate exceeds 100/day on dogfood (means cheap_prefilter too lax → halt + tighten)
- Codegen prompt size grows >20% on average (memory section uncontrolled → halt + cap stricter)
- Replay benchmark shows regression (repair rounds INCREASE) → halt + revert

## Out of scope

- Polarity (positive vs negative memory) — DEFERRED, encode in observation text
- Vector DB / graph DB — DEFERRED to 5.5/5.6
- Reflection / contradiction resolution — DEFERRED to 5.7
- User-personalization memory — DEFERRED to 5.4 (after gate-failure proves value)
- Cross-session global vs per-session — single global v1; scope filter handles per-context
- Confidence as filter — recorded only, used in 5.7 reflection
- Stale rate / regression rate metrics — DEFERRED to 5.3

## Workflow

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/memory-system-v1" \
  -c model_reasoning_effort=xhigh \
  - < /tmp/codex-stage27-prompt.txt
```

## Why this scope

- Smallest path to a USEFUL memory (not just stored)
- Roadmap-backed (Phase 5.2 explicit)
- Gate-failure pivot per Codex consult
- 3 over-design points trimmed
- Reversible via env flag + cheap schema migration if v1 unsuitable
