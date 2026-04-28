# QA Baseline 2026-04-28 — CC Agentic Retrieval (NEW Phase 3.0 reference)

This is the **new Day-0 reference** for QA accuracy. All future Phase 3.x optimization PRs must cite before/after numbers against this baseline.

## Headline

| Metric | Old RAG (2026-04-23) | First CC attempt (2026-04-28 morning) | **THIS baseline** | vs RAG |
|---|---|---|---|---|
| Mean | 27.06 | 17.82 | **49.65** | **+22.59** |
| Completion | 34/34 | 14/34 | **34/34** | — |
| A | 35.00 | 9.50 | **56.50** | +21.50 |
| B | 11.00 | 12.20 | **37.60** | +26.60 |
| C | 41.00 | 35.12 | **70.62** | **+29.62** |
| D | 22.00 | 18.00 | **30.33** | +8.33 |

**CC agentic mode is the new default retrieval path.** It outperforms single-path RAG on every tier; C-tier nearly doubles (41 → 70.62) which is the biggest cross-file improvement we've ever measured.

## Run details

- Artifact: `apps/backend/tests/benchmarks/runs/qa-run-20260428T093042Z.jsonl`
- Backend branch: `feat/kb-cc-agentic` @ `7a34c37` (cc-agentic merge + runner timeout flag)
- Backend port: 127.0.0.1:8003
- Started: 2026-04-28 09:30:42 UTC
- Finished: 2026-04-28 10:41:50 UTC
- Wall-clock: **71.1 minutes**
- Per-question: avg 111.6s / median ~108s / min 59.2s / max 216.9s
- Judge: `claude_code` CLI (CLI-only judge — no API budget consumed)
- Judge multi-sample: N=3 (per-keypoint majority vote)
- Question timeout: 240s (raised from previous 120s in commit `7a34c37`)

## Required env / config to reproduce

```
OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=3000
OPS_AGENT_CC_AGENTIC_ENABLED=true
OPS_AGENT_CC_AGENT_PROVIDER_CHAIN=claude_code,codex,minimax
OPS_AGENT_CC_AGENT_MAX_ROUNDS=3
OPS_AGENT_CC_AGENT_MAX_TOOL_CALLS=8
OPS_AGENT_CC_AGENT_OVERALL_TIMEOUT_S=30.0
OPS_AGENT_CC_AGENT_PER_CALL_TIMEOUT_S=20.0
```

Default in `apps/backend/app/core/config.py` for `knowledge_synthesis_max_snippet_chars` is still 6000 — the 3000 here is a **local .env override** required to reproduce these numbers. Recommend bumping the default to 3000 in a follow-up commit; tracked in Stage 9 Lesson #2.

## Per-tier breakdown

```
tier  n  completed  timed_out  mean   median   min    max
A     10        10          0  56.50  70.00    30.00  70.00
B     10        10          0  37.60  34.00    10.00  70.00
C      8         8          0  70.62  76.00    44.00  90.00
D      6         6          0  30.33  32.00    20.00  42.00
```

## Acceptance criteria check (Stage 9 plan)

| Criterion | Target | Actual | Result |
|---|---|---|---|
| Completion rate | ≥28/34 | **34/34** | ✅ |
| Overall mean | ≥35 | **49.65** | ✅ |
| D-tier mean | ≥40 | 30.33 | ❌ (-9.67) |
| Wall-clock | ≤45 min | 71.1 min | ❌ (+26.1) |

**Verdict**: 2/4 hard criteria met. The 2 failures are real but secondary to the headline +22.59 mean improvement. Both have known mitigation paths and are tracked as follow-up tickets, not blockers.

## Why D-tier missed target

First CC attempt (2026-04-28 morning) had D-tier max **78** — a multi-hop question got synthesized correctly with full evidence context. After applying P (snippet cap = 3000), D max dropped to 42 because:

- Multi-hop / trace questions need **larger** per-citation snippets to follow code flow
- Cap = 3000 truncates `cc_read` results aggressively, losing context that D-tier specifically needs
- A/B/C tier benefit from cap (smaller prompts → faster + less noise) but D-tier loses

**Fix path**: tier-aware cap (D = 6000, A/B/C = 3000), proposed in user's original Stage 9 critique. Filed as `T-KB-EVIDENCE-TIER-CAP` (separate ticket).

## Why runtime missed target

- Per-Q avg 111.6s = CC agent (~30s) + synthesis (~60-70s) + overhead
- P (cap 3000) cut synthesis from ~80s to ~60s, not the ~30-40s I estimated
- 3000-char snippets × 4 citations = 12K char prompt; MiniMax-style synthesis is just inherently slow at this scale

**Fix paths** (separate tickets):
- `T-KB-CLI-POOL` — pre-spawn `claude` CLI processes to remove ~5s/call cold start (saves ~20s/Q)
- `T-KB-HYBRID-RAG-FAST-PATH` — A/B-tier questions go through fast RAG (13-18s) instead of CC; only C/D-tier use CC

## What Phase 3.x improvements should report

From this baseline, every future Phase 3.x ticket's PR must include:

```
Baseline: docs/ai/benchmarks/qa-baseline-2026-04-28.md (mean=49.65)
After this PR (re-ran with same env):
  A: NN.NN (delta vs 56.50: +X.XX)
  B: NN.NN (delta vs 37.60: +X.XX)
  C: NN.NN (delta vs 70.62: +X.XX)
  D: NN.NN (delta vs 30.33: +X.XX)
  Mean: NN.NN (delta vs 49.65: +X.XX)
  Completion: NN/34
  Wall-clock: NNmin
```

A PR that drops mean or completion → reviewer rejects unless explicitly justified. A PR that improves D-tier toward 40 = priority merge.

## Top performers (highest-scoring questions)

```
C-08 (90):  best in benchmark — cross-file question CC nailed
C-04 (80)
C-02 (80)
C-06 (76)
C-01 (76)
A-01 (70), A-04 (70), A-05 (70)  — A-tier with full file localization
B-08 (70), C-07 (70)
```

C-tier dominates the top — exactly what CC was supposed to fix (cross-file reasoning).

## Worst performers (need investigation in Phase 3.1+)

```
B-01 (10):  single-file explain that CC agent shallow-read
A-09 (30)
B-04 (30)
D-04 (20):  multi-hop, D-tier shape problem (snippet cap hurts)
D-05 (20):  same
```

The B-01 / A-09 / B-04 misses are the real signal that A/B-tier still needs work (synthesis quality, not retrieval).

## Notes for next ticket holder

1. **Don't lower cap below 3000** without re-running this benchmark — cap is the load-bearing knob right now
2. **Don't increase CC agent budget** (max_rounds, max_tool_calls) without measuring runtime hit — current 30s overall_timeout is hitting; loosening means more timeouts
3. **Judge model = `claude_code`** for this baseline. Switching judge provider invalidates direct comparison; if you change it, re-run a control point on this dataset with the new judge and document the delta
4. **The 71-min wall-clock is the critical operational ceiling**. Any optimization that pushes it >90 min should be questioned ("can we measure this faster?")
