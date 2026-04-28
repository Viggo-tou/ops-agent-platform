# T-KB-EVIDENCE-TIER-CAP-V2 — Invert default policy (wide-default, narrow-on-strong-signal)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P1 — iterates v1 commit `9be1ccf`)
**Priority:** P1 (Stage 12; blocks accepting tier-aware cap as Phase 3.0 closure)
**Created:** 2026-04-29
**Iterates:** [T-KB-EVIDENCE-TIER-CAP](T-KB-EVIDENCE-TIER-CAP.md)

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Replace v1's "narrow-default, wide-on-multi-hop-keyword" policy with the inverted "wide-default, narrow-on-strong-locate-signal" policy. Pin benchmark judge + synthesis provider settings to remove run-to-run variance from baseline comparison. Goal: D-tier ≥ 40 (already hit in v1) AND no C-tier regression vs 49.65 baseline.

## Background

Stage 11 v1 (commit `9be1ccf`) implemented tier-aware cap with classifier `_detect_query_shape() → "multi_hop" | "single_focus"`:
- multi_hop keywords (trace, flow, lifecycle, depends, ...) → cap 6000
- everything else → cap 3000

Stage 11 baseline result vs prior baseline 49.65:
- D-tier: 30.33 → 41.00 (+10.67) ✅ target hit
- C-tier: 70.62 → **54.88 (-15.75)** ★ root cause: C-tier "which components use X" / "where is X referenced" queries don't contain multi_hop keywords → classified single_focus → cap 3000 → underweighted
- Total mean: 49.65 → 47.29 (-2.36) — also confounded by judge mode mixing

Two separate issues to fix:

1. **Classifier is fundamentally wrong-default**. Keyword lists are inherently incomplete for code QA. The conservative bias is to **default wide and narrow only on strong locate signal**.

2. **Benchmark is non-reproducible**: v1 used `--judge-mode auto` which silently fell from claude_code to codex to minimax mid-run. Compared against 49.65 (also `auto` but happened to stay on claude_code). Need pinned judge + pinned synthesis for any baseline-vs-baseline comparison.

## Design

### A. Invert default — narrow only on strong locate signal

Replace `_detect_query_shape()` returning `multi_hop | single_focus` with `_detect_locate_signal()` returning `bool`. The default IS multi (wide cap); only return True for strong locate.

```python
# apps/backend/app/services/knowledge_synthesis.py

_STRONG_LOCATE_PATTERNS_EN = [
    r"\bwhere\s+is\s+\w+\s+(defined|implemented|located)\b",
    r"\bwhich\s+file\s+(has|contains|defines)\s+\w+",
    r"\bfind\s+the\s+(file|function|class|definition)\s+(of|for)\s+\w+",
    r"\b(file|function|class)\s+\w+\s+is\s+(defined|located)\s+",
    r"^\s*locate\s+\w+",
]

_STRONG_LOCATE_PATTERNS_ZH = [
    r"^\s*\w+\s*在哪个?文件",
    r"^\s*\w+\s*在哪里(?:定义|实现)",
    r"^\s*找到\s*\w+\s*的?文件",
    r"^\s*哪个文件(?:有|包含|定义)\s*\w+",
]


def _detect_locate_signal(query: str) -> bool:
    """
    Return True only if query is a STRONG simple-locate query.
    Default is False (treat as wide-cap).

    Conservative bias: wrong-False (treat locate as multi) → slightly slower synthesis,
    but correct evidence. Wrong-True (treat multi as locate) → starves cross-file
    questions, score regression. The asymmetry favors False.
    """
    q = query.strip()
    # Length gate: real locate queries are short
    if len(q) > 60:
        return False
    if any(re.search(p, q.lower()) for p in _STRONG_LOCATE_PATTERNS_EN):
        return True
    if any(re.search(p, q) for p in _STRONG_LOCATE_PATTERNS_ZH):
        return True
    return False
```

### B. Two cleaner settings

```python
# apps/backend/app/core/config.py

# Replace:
# knowledge_synthesis_snippet_cap_single: int = 3000
# knowledge_synthesis_snippet_cap_multi: int = 6000

# With:
knowledge_synthesis_snippet_cap_default: int = 6000   # was: snippet_cap_multi
knowledge_synthesis_snippet_cap_narrow: int = 3000    # was: snippet_cap_single (only fires on strong locate signal)

# Keep legacy for back-compat:
knowledge_synthesis_max_snippet_chars: int = 0  # >0 overrides both, applies to all
```

### C. Update `_format_evidence`

```python
def _format_evidence(self, query: str, citations: list[KnowledgeCitation]) -> str:
    legacy = self.settings.knowledge_synthesis_max_snippet_chars
    if legacy and legacy > 0:
        limit = legacy
    elif _detect_locate_signal(query):
        limit = self.settings.knowledge_synthesis_snippet_cap_narrow  # 3000
    else:
        limit = self.settings.knowledge_synthesis_snippet_cap_default  # 6000  ← DEFAULT
    ...
```

### D. Update trace fields

Rename for clarity:

```python
# In KnowledgeAnswerTrace
# Was:
synthesis_query_shape: str | None  # "multi_hop" | "single_focus"
synthesis_snippet_cap_used: int | None
# To:
synthesis_locate_detected: bool | None  # True = narrow cap fired
synthesis_snippet_cap_used: int | None
```

### E. Pin judge AND synthesis in baseline runs (documentation + spec test)

Not a code change — a documentation + test contract:

1. Update `docs/ai/benchmarks/qa-baseline-2026-04-28.md` "Required env to reproduce" section to add:
   ```
   OPS_AGENT_PRIMARY_AGENT_PROVIDER=minimax
   OPS_AGENT_PRIMARY_AGENT_MODEL=MiniMax-M2.7
   ```
   Already required: `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=0` (now means "use tier defaults" since v2 default is 6000).

2. Add a new test `test_format_evidence_default_is_wide_cap`:
   ```python
   def test_format_evidence_default_is_wide_cap():
       # Generic explain query — no strong locate signal
       result = synth._format_evidence(
           "explain how authentication works",
           [make_citation(snippet="x" * 10000)]
       )
       # snippet should be cut at 6000, not 3000
       assert "x" * 6000 in result
       assert "x" * 6001 not in result
   ```

## Files to edit

1. `apps/backend/app/services/knowledge_synthesis.py` — replace classifier, invert default
2. `apps/backend/app/core/config.py` — rename 2 settings
3. `apps/backend/app/schemas/knowledge.py` — rename trace field
4. `apps/backend/app/services/knowledge.py` — update trace recording
5. `apps/backend/tests/services/test_knowledge_synthesis.py` — replace tests for new policy
6. `docs/ai/benchmarks/qa-baseline-2026-04-28.md` — add pinned synthesis env var to "reproduce" section

## Tests

Adapt v1's 17 tests to new policy. Key cases:

1. `test_detect_locate_signal_short_simple_query_returns_true` — "Where is Login defined?" → True
2. `test_detect_locate_signal_cross_file_query_returns_false` — "which components use AuthProvider" → False (was incorrectly classified as single_focus in v1)
3. `test_detect_locate_signal_explain_query_returns_false` — "explain how login works" → False
4. `test_detect_locate_signal_long_query_returns_false` — query > 60 chars → False (length gate)
5. `test_format_evidence_default_uses_wide_cap` — no signal → cap 6000
6. `test_format_evidence_strong_locate_uses_narrow_cap` — signal True → cap 3000
7. `test_legacy_setting_overrides_both_caps` — legacy > 0 → that value wins (back-compat)
8. `test_trace_records_locate_detected_flag` — boolean flag in trace
9. `test_trace_records_actual_cap_used` — int in trace

## Acceptance criteria

- `python -m compileall app` clean
- 9+ new tests pass
- **Re-run benchmark with `--judge-mode claude_code` (PINNED)**:
  - Total mean ≥ 49.65 (no regression vs reference baseline)
  - **D-tier mean ≥ 40** (preserve v1's win)
  - **C-tier mean ≥ 65** (recover from v1's -15.75)
  - A/B-tier each ≥ current value − 3 (noise tolerance)
  - Wall-clock ≤ 100min (more than v1 because cap=6000 default = slower synthesis; soft cap)
- baseline 2026-04-28 doc updated with pinned synthesis provider in env

## Out of scope

- Adding 3rd tier cap (3000/4500/6000) — over-engineering until binary data proves insufficient
- Per-query LLM classifier — keyword + length rule is plenty for code QA
- Hybrid RAG fast-path — separate ticket (T-KB-HYBRID-RAG-FAST-PATH)
- Pinning model VERSION exactly (e.g. M2.7 vs M2.8) at runtime — env var sufficient

## Workflow

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-KB-EVIDENCE-TIER-CAP-V2.md
```

Worktree: re-use existing `D:/项目/ops-worktrees/evidence-tier-cap` on `feat/kb-evidence-tier-cap` (commit `9be1ccf` v1). v2 builds on v1.

## Sequencing note

v2 commits ON TOP of v1. After v2 lands and benchmark passes, the combined branch can be merged to checkpoint. v1 commit stays in history as "tried this, reverted policy".
