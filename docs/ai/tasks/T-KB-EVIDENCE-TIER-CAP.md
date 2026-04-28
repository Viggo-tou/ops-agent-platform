# T-KB-EVIDENCE-TIER-CAP — Tier-aware synthesis snippet cap (D=6000 / ABC=3000)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P1 — direct lift to D-tier; trade-off validated by 2026-04-28 baseline)
**Priority:** P1 (only thing standing between current 49.65 and 55+ overall mean)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Replace the global `knowledge_synthesis_max_snippet_chars` (single int) with a tier-aware policy: for multi-hop / cross-file / trace queries (current "D-tier" pattern), use a higher per-citation cap (default 6000); for simple-locate / single-file queries (A/B/C-tier shapes), keep the tighter cap (default 3000). Goal: recover the D-tier ceiling that the global cap cost us, without sacrificing A/B/C synthesis speed.

## Background

Stage 9 baseline (2026-04-28):
- Snippet cap was global at 6000; 4 citations × ~6000 chars each = 24KB synthesis prompt → ~80s synthesis time
- Lowering global cap to 3000 (via .env `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=3000`) cut prompt to 12KB → ~60s synthesis
- Mean improved 17.82 → 49.65 (+31.83 from cap alone, on top of CC mode benefit)
- BUT D-tier max went from 78 (first run, cap=6000) → 42 (second run, cap=3000). Multi-hop questions need more context.

The original Stage 9 user critique correctly proposed tier-aware cap:

> A/B 档：max evidence = 3-4
> C/D 档：max evidence = 5-6

We're applying the same idea to per-citation char cap instead of evidence count (count is already capped at 4 by `top_k`).

## Design

### A. Detect query "shape" without a real classifier

Don't build a new ML/LLM classifier — use simple keyword heuristics that map to query intent. Empirically derived from our 34-question dataset:

```python
# apps/backend/app/services/knowledge_synthesis.py (or a new helper)

_MULTI_HOP_KEYWORDS_EN = {
    "trace", "flow", "across", "lifecycle", "end-to-end", "from", "to",
    "between", "where does it go", "what happens when", "follow",
    "calls", "called by", "depends on", "depended on", "couples",
    "interaction", "integrate", "integration",
}

_MULTI_HOP_KEYWORDS_ZH = {
    "链路", "流程", "调用", "跨文件", "追踪", "从", "到",
    "之间", "依赖", "耦合", "整合", "联动", "传递",
    "lifecycle", "整体",  # mixed-language
}

def _detect_query_shape(query: str) -> Literal["multi_hop", "single_focus"]:
    q = query.lower()
    en_hits = sum(1 for k in _MULTI_HOP_KEYWORDS_EN if k in q)
    zh_hits = sum(1 for k in _MULTI_HOP_KEYWORDS_ZH if k in query)
    if en_hits + zh_hits >= 1:
        return "multi_hop"
    return "single_focus"
```

Conservative bias: when in doubt, classify as `single_focus` (safer because tighter cap). Multi_hop detection is opt-in via specific keywords.

### B. Two configs instead of one

`apps/backend/app/core/config.py`:

```python
# Replace:
# knowledge_synthesis_max_snippet_chars: int = 6000

# With:
knowledge_synthesis_snippet_cap_single: int = 3000  # A/B/C tier (focus)
knowledge_synthesis_snippet_cap_multi: int = 6000   # D tier (multi-hop)
# Preserve old setting as alias so existing .env keep working:
knowledge_synthesis_max_snippet_chars: int = 0      # 0 = use tier-aware caps; >0 = legacy override (apply to both)
```

Behavior:
- If legacy `knowledge_synthesis_max_snippet_chars > 0` is set → use it for both shapes (backward compat for old .env)
- Otherwise → use shape-aware caps

### C. Wire into `_format_evidence`

`apps/backend/app/services/knowledge_synthesis.py::KnowledgeSynthesizer._format_evidence`:

```python
def _format_evidence(self, query: str, citations: list[KnowledgeCitation]) -> str:
    legacy = self.settings.knowledge_synthesis_max_snippet_chars
    if legacy and legacy > 0:
        limit = legacy
    else:
        shape = _detect_query_shape(query)
        limit = (
            self.settings.knowledge_synthesis_snippet_cap_multi
            if shape == "multi_hop"
            else self.settings.knowledge_synthesis_snippet_cap_single
        )
    parts: list[str] = []
    for index, citation in enumerate(citations, start=1):
        snippet = citation.snippet or ""
        if len(snippet) > limit:
            snippet = snippet[:limit] + "\n(truncated)"
        parts.append(...)  # existing format
    return "\n\n".join(parts)
```

Note: `_format_evidence` currently doesn't receive `query` — caller has it. Plumb through.

### D. Surface in answer trace

Add `synthesis_snippet_cap_used: int` and `synthesis_query_shape: str` to `KnowledgeAnswerTrace` so PR reviewers can see which cap fired per-question, and benchmark report can break down by shape.

## Files to edit

1. `apps/backend/app/core/config.py` — add 2 new settings + keep legacy
2. `apps/backend/app/services/knowledge_synthesis.py` — add `_detect_query_shape` + plumb cap selection through `_format_evidence`
3. `apps/backend/app/schemas/knowledge.py` — add 2 trace fields to `KnowledgeAnswerTrace`
4. `apps/backend/app/services/knowledge.py` — pass query into synthesis call (probably already does); record trace fields
5. `apps/backend/tests/services/test_knowledge_synthesis.py` — new tests (see below)

## Files to create

None.

## Tests

### Unit tests (test_knowledge_synthesis.py)

1. `test_detect_query_shape_simple_locate_returns_single_focus` — "Where is Login defined?" → single_focus
2. `test_detect_query_shape_trace_returns_multi_hop_en` — "Trace login from form to dashboard" → multi_hop
3. `test_detect_query_shape_trace_returns_multi_hop_zh` — "登录链路从表单到 dashboard 是怎么走的" → multi_hop
4. `test_detect_query_shape_kb_specific_words` — "Which components use AuthProvider" (cross-file but no trace keyword) → single_focus (intentional — undecidable from words alone, default conservative)
5. `test_format_evidence_uses_single_cap_for_focus_query` — given focus query + citation with 5000-char snippet, evidence string truncates at 3000
6. `test_format_evidence_uses_multi_cap_for_multi_hop_query` — given trace query + citation with 5000-char snippet, evidence keeps full 5000 (under 6000 cap)
7. `test_legacy_setting_overrides_tier_caps` — set `knowledge_synthesis_max_snippet_chars=2000`, both shapes use 2000
8. `test_truncation_marker_present_when_snippet_capped` — assert "(truncated)" appears in output

### Integration

9. `test_knowledge_search_records_query_shape_in_trace` — call retrieve() with multi_hop query; result.answer_trace.synthesis_query_shape == "multi_hop", synthesis_snippet_cap_used == 6000

## Acceptance criteria

- `python -m compileall app` exits 0
- 9 new tests pass
- Re-run benchmark with `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=0` (or unset; defaults to tier-aware):
  - Total mean ≥ 49.65 (no regression)
  - **D-tier mean ≥ 40** (recover the lost ceiling)
  - A/B/C-tier each ≥ current value (no regression)
  - Wall-clock ≤ 80 min (D path is slower, accept slight regression)

## Out of scope

- Adding a real ML query classifier — hard-keyword heuristic is deliberate (predictable, debuggable, no eval drift)
- Changing the citation count cap (top_k) — separate ticket
- Hybrid RAG fast-path — separate ticket (T-KB-HYBRID-RAG-FAST-PATH)
- Auto-tune cap based on actual prompt size — defer

## Workflow

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-KB-EVIDENCE-TIER-CAP.md
```

Worktree: branch `feat/kb-evidence-tier-cap` off `checkpoint/pre-reclassify` (after T-MERGE-CC-AGENTIC-INTO-MAIN lands). If merge hasn't landed yet, branch off `feat/kb-cc-agentic` instead.
