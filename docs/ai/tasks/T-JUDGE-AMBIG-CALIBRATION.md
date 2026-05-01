# T-JUDGE-AMBIG-CALIBRATION — Build a small calibration dataset from rule-vs-MM disagreements

<!-- Effort: small (mostly human-in-loop) -->
<!-- Executor: human-in-loop with optional LLM assistance -->

**Status:** todo (P3 — V2 prerequisite, not blocking V1)
**Priority:** P3 (queues V2; not on V1 critical path)
**Created:** 2026-05-01
**Branch:** N/A (creates a calibration data file, no code changes)
**Linked:** `docs/ai/tasks/T-JUDGE-HYBRID-V2.md`, `docs/ai/specs/stage20-judge-verdict.md`

## Why this exists

The V1 hybrid audit (10 rule-vs-MiniMax disagreement cases) produced TP=2, FP=3, ambiguous=5 — directional but not statistically conclusive. To make V2 design decisions concrete (weights, gating rules, drop/keep evidence rung), we need a calibration dataset of:

- Cases where rule fires but MM doesn't (V1 audit found 10 of these)
- Cases where MM fires but rule doesn't (audit didn't enumerate these — opposite direction)
- Cases where evidence-rung over-credited under V1 (we have 4: A-14, C-11, D-07, plus B-12 which V1-FIX cleaned up)

The deliverable is a small JSONL dataset (~20-30 rows) where each row is hand-graded by a careful human reader, optionally cross-validated by 2+ LLM families, with structured fields for V2 to optimize against.

## Goal

Produce `apps/backend/tests/benchmarks/judge_calibration_dataset.jsonl` containing 20-30 rows, each with:

```json
{
  "case_id": "audit-001",
  "source_artifact": "qa-rejudge-handymanapp-hybrid-fixed.jsonl",
  "question_id": "A-14",
  "tier": "A",
  "expected_keypoint": "binds to a RecyclerView using `CustomerJobListAdapter`",
  "answer_excerpt_full": "<full answer text, no truncation>",
  "expected_citations_retrieved": ["handymanapp/.../JobListFragment.kt"],
  "rule_credit": 1.0,
  "rule_matched_tokens": ["recyclerview", "using", "customerjoblistadapter"],
  "rule_overlap_ratio": 0.75,
  "evidence_credit": 0.6,
  "evidence_card_text_excerpt": "<the card text from the matched expected citation>",
  "minimax_credit": 0.0,
  "minimax_response_excerpt": "<MM's reasoning if available>",
  "second_family_credit": null,
  "second_family_response_excerpt": null,
  "human_verdict": "tp" | "fp" | "ambiguous",
  "human_verdict_reasoning": "<1-2 sentence justification>",
  "v2_target": "tp" | "fp" | "no_credit"
}
```

## Sourcing approach

### Step 1: Enumerate audit cases

Re-derive from existing artifacts (no fresh bench needed):

- 10 rule-fires-LLM-misses cases (already enumerated in this session's chat — A-14, A-18, B-03, B-10×3, C-04, C-06, C-09, D-02). Pull full untruncated answers from each task via the backend `/api/tasks/{task_id}` endpoint (use `rejudge_run`'s extraction logic as a reference, but skip judging — just dump answer + citations).

- 5-10 LLM-fires-rule-misses cases — these are the inverse direction. Filter the same hybrid artifacts for `rule_credit == 0 AND llm_credit > 0`. Sample 5-10 across tiers.

- 4 evidence-rung over-credit cases from the V1 (pre-FIX) hybrid run: A-14, B-12, C-11, D-07. Three of these (A-14, C-11, D-07) still over-credit even after V1-FIX because expected card_text is comprehensive. These are concrete examples for the V2 evidence-rung redesign.

### Step 2: Hand grade

For each row, read the FULL answer and decide:

- `tp` — answer ACTUALLY covers the keypoint to a careful human reader's expectation. Whichever judge said "hit" was right.
- `fp` — answer does NOT cover the keypoint despite some surface-level token match. The "hit" judgment was wrong.
- `ambiguous` — partial coverage; reasonable judges could disagree.

Record reasoning in 1-2 sentences. Bias acknowledgment: the grader should declare priors before reading (per V1 audit precedent).

### Step 3: Cross-family LLM grading (optional, gated on credit availability)

If Anthropic credit restored or OpenAI key configured, ask both for a per-case verdict:

```
Question: <q>
Expected keypoint: <kp>
Answer text: <full_answer>

Does the answer convey the meaning of the expected keypoint? Reply yes/no with one sentence of reasoning.
```

Record both responses verbatim. Disagreements between human, MM, and second family are the highest-information rows for V2 calibration.

### Step 4: Set `v2_target`

For each row, decide what the IDEAL V2 hybrid output should be:

- `tp` if the case represents a true keypoint hit V2 should credit
- `fp` if V2 should NOT credit (hybrid weight tuning should produce 0 or near-0)
- `no_credit` if V1's behavior on this row is acceptable (e.g. ambiguous and current credit is partial/correct)

`v2_target` is what V2 spec acceptance tests will check against.

## Files to create

1. `apps/backend/tests/benchmarks/judge_calibration_dataset.jsonl` — 20-30 graded rows.
2. `docs/ai/specs/judge-calibration-method.md` — short doc (~½ page) describing the grading rubric, prior-disclosure rule, and how V2 should consume this dataset.

## Acceptance

- Calibration dataset has ≥20 rows
- Each row has all required fields populated except cross-family fields (which are nullable when 2nd LLM not available)
- Each row has a human verdict + reasoning sentence
- Tier distribution: at least 2 rows per tier (A/B/C/D); at least 5 rows per disagreement direction (rule-only-fires, LLM-only-fires)
- Dataset committed alongside a `data(bench): judge calibration dataset (V2 prereq)` commit

## Out of scope

- Using this dataset to tune V1 (V1 ships as MM-only without it)
- Auto-generating verdict via a single LLM (defeats the calibration purpose)
- Expanding to 100+ rows (small focused dataset is fine for V2 weight tuning; expand later if V2 reveals additional disagreement modes)

## Workflow

Pure human-in-loop with optional LLM-assist for the second-family grading. Total wall time: 1-3 hours.

## Why P3

V1 (T-JUDGE-DEFAULT-MINIMAX-V1) doesn't depend on this. V2 (T-JUDGE-HYBRID-V2) does, but V2 is itself P2-deferred until 2nd LLM family available. So calibration is a queued task — useful when picked up but not blocking any current path.
