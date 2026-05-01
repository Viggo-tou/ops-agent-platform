# T-JUDGE-HYBRID-V2 — Cross-family hybrid judge (deferred from V1)

<!-- Effort: medium-high -->
<!-- Executor: codex -->

**Status:** deferred (gated on T-JUDGE-AMBIG-CALIBRATION + Anthropic credit / OpenAI key)
**Priority:** P2 (Stage 20 follow-up; not required for V1)
**Created:** 2026-05-01
**Branch (future):** `feat/judge-hybrid-v2` based on whatever HEAD has the V1 default landed
**Linked decision:** `DECISIONS.md` D-010
**Linked predecessors:**
- `docs/ai/tasks/T-JUDGE-HYBRID-V1.md` (retired)
- `docs/ai/tasks/T-JUDGE-HYBRID-V1-FIX.md` (retired)
- `docs/ai/tasks/T-JUDGE-DEFAULT-MINIMAX-V1.md` (the V1 that ships instead)

## Why this exists

T-JUDGE-HYBRID-V1 implementation landed and was smoked. Manual inspection of 10 rule-vs-MiniMax disagreement cases found TP=2, FP=3, ambiguous=5. Hybrid was retired as V1 default but the scaffolding (rule + cited-evidence + LLM rungs, max-credit aggregation, summary aggregates) is preserved in code as `--judge-mode hybrid` (experimental). V2 picks this up when the gating prerequisites are met.

## Gating prerequisites (must be true before V2 starts)

1. **Second LLM judge family available** — Anthropic credit restored OR OpenAI configured in `apps/backend/.env`. Stage 20 verdict explicitly requires cross-family validation.
2. **`T-JUDGE-AMBIG-CALIBRATION` complete** — the 10 disagreement cases (5 ambiguous from V1 audit + 5 follow-up cases of MM-misses-rule-catches) have been adjudicated by 2+ LLM families and a human, producing a small calibration dataset. V2 weight tuning depends on this.
3. **Decision recorded** — DECISIONS entry confirming Stage 20A V2 should proceed (not skipped in favor of `T-JUDGE-RULE-THRESHOLD-AUDIT` or other paths).

If any of these are missing, V2 stays deferred.

## Goal (V2 design sketch — final spec written when prerequisites met)

Hybrid V2 needs to address the V1 audit findings:

- **Cross-family agreement requirement**: a keypoint earns LLM-rung credit only if 2+ LLM families agree (e.g. MiniMax AND Anthropic both say hit). Single-family hits go into a "weak signal" diagnostic field, not the score.
- **Stricter rule rung**: token-overlap threshold tightened from 75% to require either (a) full normalized substring match, OR (b) exact-token match on at least one symbol-class token (CamelCase / underscore_id / dot.path tokens). Eliminates rule false positives like "current/page/jobs" matching "exports current page via ExportReportButton".
- **Evidence rung re-evaluation**: V1-FIX scoped evidence to expected-citation card_text only, but the audit showed it still credits keypoints the answer never articulated when the expected card is comprehensive. V2 options:
  - (a) Drop evidence rung entirely (current V1 final has it scoped but the rung's credit weight is only used when LLM is unavailable).
  - (b) AND-gate evidence with an answer-engagement check (answer must mention some token from the same file). Likely degenerates to LLM-only.
  - (c) Lower evidence weight to 0.2 and document as a soft backstop.
  - Decide based on calibration data.
- **Disagreement set as primary diagnostic output**: the per-keypoint table where rule / evidence / LLM-A / LLM-B disagree is the highest-information signal for analysts. Make it a first-class field in artifacts, not just a count.

## Acceptance (placeholder — full spec when prerequisites met)

- `python -m compileall` clean
- 35+N tests pass (where N is the new V2 cross-family tests)
- Smoke run on handymanapp 26Q with `--judge-mode hybrid` produces:
  - `judge_family_count = 2`
  - `cross_family_validated = true`
  - `disagreement_set` field with per-record / per-keypoint family-level disagreement
- Cross-stack gap (dashboard − handymanapp) under V2 hybrid lands within 2 points of MM-only on aggregate, AND the per-tier deltas show no individual tier exceeding +5 over MM (the V1 problem on handymanapp C).
- Net delta `TP − FP > 3` on the calibration dataset's audit cases.

## Out of scope

- Replacing MM with another judge family entirely (V2 is hybrid; switching judges is a separate decision).
- Adding a third LLM family beyond two.
- Tuning prompt for any judge family (separate ticket if calibration shows judge-prompt is the bottleneck).

## Workflow (when ready)

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/judge-hybrid-v2" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-JUDGE-HYBRID-V2.md
```

Worktree from the post-V1 checkpoint HEAD.

## Why P2 not P1

V1 (MM-only) is sufficient for current cross-stack benchmarking. The 8.46-point cross-stack gap under MM is the working assumption for Stage 20C decisions. V2 is an accuracy-improvement project, not a blocker for any current Stage 20 work. It revisits when calibration data + 2nd LLM family are both ready.
