# Stage 20 Verdict: Rule Judge Bias and Hybrid Judge Priority

**Date:** 2026-05-01
**Status:** Decision record (ADR-style)
**Linked decision:** DECISIONS.md → D-010

> Semantic rejudge shows most of the dashboard→handymanapp gap was judge artifact, not retrieval/cards failure.

## TL;DR

Re-judging the Stage 19 handymanapp 26Q artifact with a cross-family LLM judge (MiniMax) collapsed the dashboard→handymanapp gap from **+25.12** to **+8.46**. Stage 20A (hybrid judge) is promoted to PRIMARY; Stage 20B (answer prompt) is deprioritized; Stage 20C (cards-v2) is narrowed to C/D multi-file synthesis only — pending more data.

## Executive conclusion

- **Stage 20A (hybrid judge) = PRIMARY.** Without it, every cross-stack benchmark systematically under-scores Android/Kotlin. This blocks all future cross-stack evaluation, not just Stage 19.
- **Stage 20B (answer prompt rewrite) = DEPRIORITIZED.** Answers are already comprehensive and citation-grounded when judge is fair (handymanapp MiniMax 51.78 ≈ dashboard MiniMax 60.24).
- **Stage 20C (cards-v2) = NARROW SCOPE, conditional.** Only targets the residual ~8 point gap on C/D multi-file synthesis, and only after 20A lands and re-bench at n≥40 valid records confirms the residual is real, not sample variance.

## Apples-to-apples (same valid records under both judges)

| Dataset | n valid in both | Rule mean | MiniMax mean | Delta |
|---|---|---|---|---|
| Dashboard (React/JS) | 25 | 59.32 | 60.24 | **+0.92** |
| Handymanapp (Kotlin/Android) | 17 | 34.20 | 51.78 | **+17.59** |

| Tier | Dashboard delta | Handymanapp delta |
|---|---|---|
| A | +5.00 (n=4) | +25.71 (n=7) |
| B | +3.60 (n=10) | +17.00 (n=6) |
| C | **−4.12** (n=8) | +1.67 (n=3) |
| D | 0 (n=3) | excluded (n=1) |

## The headline number

- **Rule judge gap**: dashboard − handymanapp = **+25.12**
- **MiniMax judge gap**: dashboard − handymanapp = **+8.46**
- **17 of 25 gap points** were rule judge artifact.

The "Stage 19: cards don't generalize to Android" reading was largely a judge artifact. Real residual cross-stack gap is at most ~8 points and concentrated on multi-file synthesis (C/D-tier).

## Why MiniMax is fair, not blanket-lenient

1. **Dashboard total delta near zero (+0.92).** MiniMax matches rule on aligned vocabulary, ruling out global inflation.
2. **Dashboard C-tier delta is NEGATIVE (−4.12).** MiniMax is *stricter* than rule on cases where answers don't truly cover keypoints despite passing exact-match.
3. **Real misses are preserved.** B-12, C-11, B-17, D-09, D-10 stayed at 0 in both judges. C-09 dropped 50 → 35.
4. **Asymmetric recovery.** Handymanapp A-tier +25.71 vs dashboard A-tier +5.00. Same model, same answers' style, same judge — the only differing variable is the dataset's keypoint vocabulary. This is rule judge's Android-paraphrasing penalty, not MiniMax's Android-leniency.

The clearest single proof point: **A-17 rule 13.3 → MiniMax 73.3 (+60).** Manual inspection confirmed all 3 expected keypoints are literally in the answer; rule judge missed them on synonym mismatches (`implemented` vs `defined`, `writes` vs `persists`, `submit` vs `submission`).

## Hybrid judge sketch (for Stage 20A implementation)

Per-keypoint hit credit:

```
For each expected keypoint:
  rule_credit       = 1.0 if rule judge matches (exact lexical / 75% token overlap)
  evidence_credit   = 0.6 if any retrieved card_text contains keypoint substring
  semantic_credit   = 0.7 if cross-family LLM judge (MiniMax/Anthropic) confirms semantic match
  hit_score         = max(rule_credit, evidence_credit, semantic_credit)
```

Aggregate per-Q: `kp_coverage = mean(hit_scores)`. Final score: `kp_coverage * 60 + citation_precision * 40` (unchanged).

This is a **sketch**, not a full spec. Full spec is the Stage 20A implementation ticket. Key invariants the spec must preserve:
- Cross-family validation: never let the same model family that generated the answer be the sole judge.
- Auditability: each keypoint's hit_score must record which rung credited it.
- Disagreement set is a first-class output: keypoints where rule/evidence/semantic disagree are the highest-information per-Q signal for diagnostics.

## Caveats (must read before acting on this verdict)

1. **MiniMax is NOT ground truth.** Single LLM judge with no cross-family validation (Anthropic credit zero blocked the secondary judge during this analysis). Stage 20A implementation MUST add a second LLM family before claims are tightened.
2. **D-tier is excluded from cross-tier conclusions.** Dashboard D n=3, handymanapp D n=1. 1-vs-3 cannot be compared. Any future "Android D-tier weak/strong" claim requires n≥10 valid handymanapp D-tier records.
3. **Residual 8.46 may be sample variance, not real C/D synthesis weakness.** At n=17 valid handymanapp records, the 95% CI on the mean spans roughly ±5 points. Stage 20C scope must wait for re-bench at n≥40 valid before committing to "real residual gap".
4. **Rule judge stays in the toolkit, not retired.** Rule judge measures *strict lexical/keyword conformance*; LLM judge measures *semantic coverage*. Both are legitimate and complementary metrics. Stage 19's rule-judge artifact remains the dashboard-comparable strict-lexical baseline. Hybrid judge does not replace rule judge — it adds rungs.
5. **Bench `question_timeout_seconds=240` is too short for handymanapp.** Rejudge rescued 4 records (A-15, B-16, C-12, D-08) where backend completed the task but bench polling deadline expired. Independent Stage 19 follow-up: raise timeout to 360-480s for handymanapp, or add a backend-completed-after-deadline counter.

## Stage 20 action plan

1. **T-JUDGE-HYBRID-V1** — implement hybrid judge in `apps/backend/scripts/run_qa_benchmark.py::KeypointJudge` per the sketch above. Add second LLM judge family (restore Anthropic credit OR add OpenAI as fallback). Acceptance: re-rejudge handymanapp + dashboard with hybrid; verify hybrid mean is between rule mean and MiniMax mean (sanity), and that disagreement set is non-trivial.
2. **T-BENCH-TIMEOUT-HANDYMANAPP** — raise question_timeout for handymanapp run to 480s OR add `task_completed_after_deadline` to summary. Re-bench to convert timeout-rescue records into legitimate valid records.
3. **T-STAGE19-REBENCH-N40** — after T-JUDGE-HYBRID-V1 + T-BENCH-TIMEOUT-HANDYMANAPP land, run handymanapp at n≥40 valid records (extend dataset or repeat-sample). This is the only data that can confirm/falsify the residual 8.46 gap.
4. **Stage 20C decision deferred** — hold cards-v2 design until the n≥40 residual is measured. If <5 points: drop 20C entirely. If >10 points: open T-CARDS-V2-MULTIFILE-SYNTHESIS narrowly scoped to C/D synthesis.

## Artifacts

- Rule-judge official: `apps/backend/tests/benchmarks/runs/qa-run-20260430T155831Z.jsonl` (dashboard, mean 59.32 valid 25/34) and `qa-run-20260501T000245Z.jsonl` (handymanapp, mean 34.20 valid 17/26).
- MiniMax rejudge diagnostic: `qa-rejudge-dashboard-minimax.jsonl` (mean 60.24 over same 25) and `qa-rejudge-handymanapp-minimax.jsonl` (mean 51.78 over same 17, plus 4 rescues).
- Per-Q delta analysis: regenerable from `apps/backend/scripts/analyze_stage19_diagnostic.py` against the rule-judge artifact.
- Patches landed in this stage:
  - `run_qa_benchmark.py` V3 smoke loosen + `retrieval_empty_no_source` bucket + systematic-empty guardrail.
  - `rejudge_run.py` V3 5-tuple extractor unpack fix.

## What this verdict does NOT claim

- Does NOT claim handymanapp accuracy is equal to dashboard accuracy.
- Does NOT claim MiniMax score is the new official Stage 19 number — rule-judge artifact remains the dashboard-comparable baseline.
- Does NOT claim cards generalize perfectly to Kotlin/Android. Real residual gap may exist; it cannot be confirmed at current sample size.
- Does NOT claim Stage 20A alone closes all cross-stack quality gaps. C/D synthesis has a real but unmeasured-magnitude residual.
