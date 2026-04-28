# T-KB-HYBRID-RAG-FAST-PATH — Use RAG fast-path for simple queries, CC for complex

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P2)
**Priority:** P2 (large runtime improvement; lower-priority because correctness boundary changes — risk of routing wrong)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Route simple-locate / single-file-explain queries through the (much faster) single-path RAG retrieval, and only invoke CC agentic retrieval for cross-file / multi-hop questions where CC's accuracy benefit outweighs its 30s+ overhead. Target: cut overall benchmark wall-clock from 71min → ~35-40min while keeping mean ≥ current 49.65.

## Background

QA baseline 2026-04-28 wall-clock distribution:
- A-tier (10 questions): each ~75-90s (simple locate, RAG would do this in ~15s = saves ~60s × 10 = 10 min)
- B-tier (10 questions): each ~90-110s (single-file explain — RAG might handle 5/10 well = saves 5 × 60s = 5 min)
- C-tier (8 questions): each ~110-130s (cross-file, NEED CC, keep)
- D-tier (6 questions): each ~150-200s (multi-hop, NEED CC, keep)

Total potential savings: ~15 min off 71min = ~21% wall-clock reduction. With CLI pool (T-KB-CLI-POOL): could compound to ~30 min.

But correctness risk: if the router misclassifies a C-tier question as A-tier and sends it through RAG, score drops dramatically (from CC's 70+ to RAG's ~40).

## Design

### A. Router decision — strict heuristics, conservative bias

Route to RAG fast-path ONLY if the query strongly indicates simple intent:

```python
# apps/backend/app/services/knowledge.py (or new helper)

_SIMPLE_LOCATE_PATTERNS_EN = [
    r"\bwhere\s+is\b.*\b(defined|located|implemented|found)\b",
    r"\bwhich\s+file\s+(has|contains|defines)\b",
    r"\bfind\s+the\s+(file|function|class)\b",
    r"\b(file|function|class)\s+\w+\s+(is|defined|located)\b",
]
_SIMPLE_LOCATE_PATTERNS_ZH = [
    r"在?哪个?文件",
    r"\w+\s*在哪",
    r"\w+\s*的位置",
    r"找到\s*\w+",
]

def _route_query_to_path(query: str) -> Literal["rag_fast", "cc_agentic"]:
    """Conservative router: default to CC, only opt into RAG when query smells obviously simple."""
    q = query.strip()
    if len(q) < 60 and any(re.search(p, q.lower()) for p in _SIMPLE_LOCATE_PATTERNS_EN):
        return "rag_fast"
    if len(q) < 30 and any(re.search(p, q) for p in _SIMPLE_LOCATE_PATTERNS_ZH):
        return "rag_fast"
    return "cc_agentic"
```

Conservative bias: when in doubt, use CC. Only opt-in to RAG with strong signals AND short query.

### B. Wire into `KnowledgeService.retrieve()`

```python
def retrieve(self, query: str, ...) -> KnowledgeSearchResult:
    if not self.settings.cc_agentic_enabled:
        return self._rag_retrieve(...)  # existing fallback

    path = _route_query_to_path(query) if self.settings.kb_hybrid_router_enabled else "cc_agentic"
    if path == "rag_fast":
        result = self._rag_retrieve(...)
        result.answer_trace.strategy = "rag_fast_path_routed"
        return result

    # CC path (existing Phase 3.0 implementation)
    cc_result = self._try_cc_agentic(...)
    if cc_result is None:
        return self._rag_retrieve(...)
    return cc_result
```

### C. Configuration

```python
kb_hybrid_router_enabled: bool = True
kb_hybrid_router_min_query_len_en: int = 60
kb_hybrid_router_min_query_len_zh: int = 30
```

Disable via `OPS_AGENT_KB_HYBRID_ROUTER_ENABLED=false` for A/B benchmarking.

### D. Surface in answer trace

Add `routed_path: "rag_fast" | "cc_agentic"` to `KnowledgeAnswerTrace` for diagnostics.

## Files to edit

1. `apps/backend/app/services/knowledge.py` — `_route_query_to_path` + branching in `retrieve()`
2. `apps/backend/app/core/config.py` — 3 new settings
3. `apps/backend/app/schemas/knowledge.py` — add `routed_path` to trace

## Files to create

1. `apps/backend/tests/services/test_kb_hybrid_router.py` — router tests

## Tests

### Router unit tests (test_kb_hybrid_router.py)

1. `test_route_where_is_x_defined_to_rag_fast` — "Where is Login defined?" → rag_fast
2. `test_route_which_file_has_y_to_rag_fast` — "Which file has the AuthProvider?" → rag_fast
3. `test_route_trace_question_to_cc_agentic` — "Trace login from form to dashboard" → cc_agentic
4. `test_route_long_simple_question_to_cc_agentic` — locate-style query but >60 chars → cc_agentic (conservative)
5. `test_route_simple_zh_query_to_rag_fast` — "Login.js 在哪个文件?" → rag_fast
6. `test_route_unrecognized_to_cc_agentic` — "Explain how the verification flow works" → cc_agentic (default)

### Integration

7. `test_retrieve_uses_rag_when_router_picks_fast_path` — mock `_route_query_to_path` → "rag_fast"; assert `_rag_retrieve` called, `_try_cc_agentic` NOT called
8. `test_retrieve_uses_cc_when_router_picks_cc` — opposite path
9. `test_router_disabled_always_uses_cc` — `kb_hybrid_router_enabled=False`; route_to_path never called; CC always used
10. `test_routed_path_recorded_in_trace` — answer_trace.routed_path matches actual path

## Acceptance criteria

- 10 tests pass
- Re-run benchmark (after merge):
  - **Wall-clock ≤ 50min** (vs 71 baseline)
  - Mean ≥ 49.65 (no regression)
  - A-tier mean ≥ 56.50 (RAG should match or exceed CC for simple-locate)
  - C-tier mean ≥ 70.62 (still on CC path)
  - D-tier mean ≥ 30.33 (still on CC path)
- Per-question routing decision logged: `kb_router decision=rag_fast|cc_agentic for question {qid}`

## Risks

| Risk | Mitigation |
|---|---|
| Router misclassifies cross-file question as simple → score regression | Conservative bias (default to CC); strict short-query heuristic |
| RAG fast-path actually scores worse than CC even on simple-locate (didn't measure this carefully — Phase AD baseline 27.06 was multi-sample) | Acceptance criteria explicitly checks A-tier ≥ 56.50; if RAG underperforms, abort and revert |
| Heuristic patterns drift as dataset grows | Track router decisions in trace; periodically review false-routings |

## Out of scope

- ML-based query classifier — keyword heuristics only
- Per-query cost-based routing (e.g. "if RAG confidence > X, skip CC") — defer
- Combining RAG + CC evidence in same query — separate ticket

## Workflow

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-KB-HYBRID-RAG-FAST-PATH.md
```

Worktree: branch `feat/kb-hybrid-router` off `checkpoint/pre-reclassify` (after T-MERGE-CC-AGENTIC-INTO-MAIN).

## Sequencing note

Recommend doing **T-KB-EVIDENCE-TIER-CAP first** (P1, 1 day), then this (P2, 1-2 days). Reason: tier-cap is pure quality lift, no router risk. This ticket changes routing semantics and needs A/B benchmark validation.
