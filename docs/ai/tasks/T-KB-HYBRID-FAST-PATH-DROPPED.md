# T-KB-HYBRID-FAST-PATH — Latency reduction on simple-locate queries (cards quality preserved)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P0 — Stage 18; per Stage 16 codex critique D5 restored to P0)
**Priority:** P0 (Stage 18; latency lever, not accuracy push — quality must be preserved)
**Created:** 2026-04-30
**Branch:** `feat/kb-hybrid-fast-path` based on `checkpoint/pre-reclassify` HEAD `58eb0d2` (T-LLM-METRICS merged)

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Add a **fast retrieval path** that simple-locate queries use to skip the CC agent round and go straight to FTS5 + card-aware synthesis. Goal: cut per-Q wall-clock on the matching subset by 60-80% (from ~110s to ~25-35s) **without losing the Stage 16 cards quality (mean 58.82, B 56.40, D 45.67)**.

This is a **latency lever**, not an accuracy lever. Quality preservation is the hard constraint.

## Background — what we have today

After Stage 16 (cards) and Stage 17 (telemetry):
- Every search hits CC agent (~5-15s per round) + FTS5 + synthesis (~15-25s)
- Total per-Q wall-clock from cards bench: 73.6 min / 34 = ~130s/Q average
- T-LLM-METRICS records per-call latency, so we can verify routing precision empirically

The synthesis-vs-cc_agent breakdown from Stage 17 smoke (1 search):
- cc_agent: 1 round, p50=5812ms
- synthesis: 1 call, p50=17044ms
- Plus orchestrator overhead, polling, planner — total per-Q ~80-130s

For "Which file defines the admin login screen?" (A-01 style), the CC agent round is mostly redundant — FTS5 BM25 + cards already nail the answer. Skipping CC agent for these queries saves the 5-15s round.

## Design

### A. Routing classifier — see USER QUERY only (Stage 13 lesson)

In Stage 13 we discovered that v2's tier-cap classifier failed because it saw the planner-rewritten query, not the user's original. Same trap here. Routing decision must use `request_text` from the task (or the literal query parameter on `/api/knowledge/search`), NEVER any downstream rewrite.

```python
# apps/backend/app/services/hybrid_router.py  (new)

_FAST_PATH_PATTERNS_EN = [
    # "Which file defines/has/contains/implements X" (locate)
    r"^\s*which\s+file\s+(defines|has|contains|implements|holds|stores)\s+\w+",
    # "Where is X defined/implemented/located/configured" (locate)
    r"\bwhere\s+is\s+\w+\s+(defined|implemented|located|configured)\b",
    # "Find the X for Y" (locate)
    r"^\s*find\s+the\s+(file|function|class|definition|component)\s+(of|for)\s+\w+",
    # "X is defined in" (locate, declarative)
    r"\b(file|function|class|component)\s+\w+\s+is\s+(defined|located)\s+",
    # Bare locate
    r"^\s*locate\s+\w+\s+(file|function|class)?",
]

_FAST_PATH_PATTERNS_ZH = [
    r"^\s*\w+\s*在哪个?文件",
    r"^\s*\w+\s*在哪里(?:定义|实现|配置)",
    r"^\s*找到\s*\w+\s*的?文件",
    r"^\s*哪个文件(?:有|包含|定义|实现)\s*\w+",
]

# Behavioral verbs that DISQUALIFY fast-path even if locate pattern matches.
# Catches "Where is X validated/processed/handled" — those need agentic.
_BEHAVIORAL_DISQUALIFIERS = {
    "validated", "processed", "handled", "transformed", "called",
    "used", "consumed", "rendered", "wired", "routed", "triggered",
    "checked", "enforced", "validated", "verified", "confirmed",
    "传递", "调用", "处理", "校验", "渲染",
}


def _detect_fast_path(query: str) -> tuple[bool, str | None]:
    """Return (use_fast_path, matched_pattern). Conservative bias: when
    uncertain, return (False, None) — full agentic path is the safe default."""
    q = query.strip()
    # Length gate: real fast-path queries are short
    if not (8 <= len(q) <= 90):
        return False, None
    q_lower = q.lower()
    # Behavioral disqualifier short-circuit
    for verb in _BEHAVIORAL_DISQUALIFIERS:
        if verb in q_lower:
            return False, None
    for pattern in _FAST_PATH_PATTERNS_EN:
        if re.search(pattern, q_lower):
            return True, pattern
    for pattern in _FAST_PATH_PATTERNS_ZH:
        if re.search(pattern, q):
            return True, pattern
    return False, None
```

### B. Fast-path execution

When `_detect_fast_path(user_query)` returns True, the search:
1. Skip CC agent loop entirely (no `_try_cc_agentic_retrieval`)
2. Use FTS5 + existing `_score_document` rerank
3. Pass top-K citations (with cards) directly to synthesis
4. Set `synthesis_max_snippet_chars` lower (default 3000) since fast queries don't need huge context

```python
# apps/backend/app/services/knowledge.py — search_repositories
def search_repositories(self, *, query: str, ...) -> KnowledgeSearchResult:
    ...
    use_fast, fast_pattern = _detect_fast_path(query)
    if use_fast and self.settings.knowledge_hybrid_fast_path_enabled:
        # Skip CC agent; FTS5 + cards + synthesis only
        cc_result = None
    else:
        cc_result = self._try_cc_agentic_retrieval(...)
    ...
```

### C. Trace fields (record both classified-query AND chosen-route)

```python
class KnowledgeAnswerTrace(BaseModel):
    ...
    fast_path_eligible: bool | None       # detector verdict
    fast_path_pattern_matched: str | None # which regex/disqualifier fired
    routing_decision: str | None          # "fast" | "full" | "fast_disabled"
    routing_query: str | None             # the EXACT string detector saw (audit)
```

Stage 13 lesson encoded: `routing_query` must equal `request_text` of the task (or the literal `query` param on /api/knowledge/search), NOT planner-rewritten output.

### D. Settings

```python
# apps/backend/app/core/config.py
knowledge_hybrid_fast_path_enabled: bool = True  # rollback flag
knowledge_hybrid_fast_path_min_score: float = 5.0  # FTS5 BM25 floor: if no candidate above this, fall through to full path
```

The min_score floor is a safety net: if FTS5 returns weak candidates, fast-path is probably wrong — fall through to full agentic path.

### E. Acceptance gate from Stage 17 metrics

After bench, query `/api/metrics/llm-calls` to verify:
- Fast-path queries should show 0 cc_agent events (not 1+)
- Full-path queries should show 1+ cc_agent events
- Mean synthesis latency on fast-path should be lower than full-path (cards skip the heavy CC context blob)

This gives us empirical "did routing actually do what spec says" verification, not just "trust the score".

## Files to edit

1. `apps/backend/app/services/hybrid_router.py` — NEW (~80 LOC: detector + tests of regex)
2. `apps/backend/app/services/knowledge.py` — gate `_try_cc_agentic_retrieval` on `_detect_fast_path` + record trace fields
3. `apps/backend/app/core/config.py` — 2 new settings
4. `apps/backend/app/schemas/knowledge.py` — 4 new trace fields
5. `apps/backend/tests/services/test_hybrid_router.py` — NEW (10+ regex coverage tests)
6. `apps/backend/tests/services/test_knowledge_hybrid.py` — NEW (4+ integration tests: fast-path skips CC, full-path keeps CC, settings gate, behavioral-disqualifier wins over locate match)

## Tests

1. `test_fast_path_detects_which_file_defines` — "Which file defines AdminProfileHeader?" → True
2. `test_fast_path_detects_where_is_defined_zh` — Chinese locate → True
3. `test_fast_path_rejects_behavioral_verb` — "Where is the auth token validated?" → False (validated disqualifier)
4. `test_fast_path_rejects_long_query` — query > 90 chars → False
5. `test_fast_path_rejects_short_query` — query < 8 chars → False
6. `test_fast_path_rejects_explain_query` — "How does login authentication work?" → False
7. `test_fast_path_rejects_cross_file` — "Which pages reuse ConfirmModal?" → False (no locate verb match)
8. `test_search_skips_cc_agent_when_fast_path_active` — integration: mock CC agent, verify it's not called
9. `test_search_uses_full_path_when_disabled_setting` — knowledge_hybrid_fast_path_enabled=False
10. `test_trace_records_routing_decision_and_query` — both fields populated
11. `test_routing_query_equals_request_text_not_rewrite` — Stage 13 lesson: integration test that uses /api/knowledge/search with a query, asserts trace.routing_query == that exact string

## Acceptance criteria

- `python -m compileall app` clean
- 11+ tests pass
- All existing tests still pass (regression sanity)
- Manual smoke from Tomonkyo bash:
  - Backend starts; `knowledge_hybrid_fast_path_enabled=True` by default
  - "Which file defines the admin login screen?" returns answer with `routing_decision="fast"` in trace, NO cc_agent metrics event
  - "How does the support ticket reply pipeline work?" returns answer with `routing_decision="full"`, 1+ cc_agent metrics events
- **Bench from Tomonkyo bash via run_qa_benchmark (PINNED claude_code, samples=3, full 34Q) shows ALL OF:**
  - Mean ≥ **57** (cards baseline 58.82; ≤ 1.82 drop from cards is acceptable noise)
  - D-tier ≥ **43** (cards 45.67; ≤ 2.67 drop)
  - B-tier ≥ **52** (cards 56.40; ≤ 4.4 drop)
  - C-tier ≥ **65** (cards 70.25; ≤ 5.25 drop)
  - A-tier ≥ **55** (cards 60.00; ≤ 5 drop — A is what fast-path targets, biggest variance OK)
  - Wall-clock ≤ **60 min** (cards 73.6 min; ≥ 13.6 min savings)
  - Completion 34/34, judge_modes_used = ["claude_code"], pinned_judge_run_intact = True
- **Routing precision check from /api/metrics/llm-calls after bench:**
  - cc_agent event count should drop ~30-50% vs cards stage (proves fast-path actually skipped cc_agent on routed queries)
  - Per-purpose synthesis p50 latency on fast-path queries should be ≤ 70% of full-path queries

## Failure modes to avoid (Stage 13 lessons baked in)

- **Routing classifier sees rewritten query**: tests must verify routing_query == request_text exactly. Per Stage 13, this was the silent killer of v2 tier-cap.
- **Behavioral disqualifier fails to fire**: test 3+ behavioral cases ("Where is X validated", "Where is Y called from") to confirm full-path wins.
- **Bench overfits to 34Q routing distribution**: Stage 19 expand-benchmark will validate generalization. For this stage, the gate is per-tier not just mean.
- **Telemetry gap masks regression**: per Stage 17, /api/metrics/llm-calls must show real cc_agent skip count post-bench. If telemetry shows fast-path didn't actually skip cc_agent for any Q, the routing didn't do anything and the score numbers are coincidence.

## Out of scope

- Multi-tier routing (locate / explain / trace each going different ways) — binary first
- LLM-based query classifier — keyword + length + verb-blacklist is plenty for v1
- Caching of fast-path results — separate ticket
- Adapting routing decision based on FTS5 confidence at runtime (instead of just regex) — speculative; ship binary first
- Touching cc_agentic loop internals
- Touching cards generation

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/kb-hybrid-fast-path" -c model_reasoning_effort=medium - < docs/ai/tasks/T-KB-HYBRID-FAST-PATH.md
```

Worktree: NEW `D:/项目/ops-worktrees/kb-hybrid-fast-path` on `feat/kb-hybrid-fast-path` based on `checkpoint/pre-reclassify` HEAD `58eb0d2`.

## D-009 contract

This ticket touches benchmark scores. Per D-009:
- Commit 1 = `feat(kb): T-KB-HYBRID-FAST-PATH — simple-locate fast retrieval path` — implementation only
- Commit 2 = `bench(kb): record hybrid fast-path stage 18 results — mean preserved, runtime down N%` — bench artifact + per-tier deltas + routing precision data from /api/metrics/llm-calls

Do NOT commit either half until full 34Q bench (PINNED claude_code) shows ALL acceptance criteria above AND routing precision check passes.
