# T-JUDGE-HYBRID-V2-CLI — Cross-family conservative semantic judge (V2 via Codex CLI)

<!-- Effort: medium -->
<!-- Executor: codex (xhigh) -->

**Status:** todo (P1 — Stage 20A V2)
**Priority:** P1 (cross-family validation of V1 MM-only baseline; conditional promotion to official)
**Created:** 2026-05-01
**Branch:** `feat/judge-hybrid-v2-cli` based on `checkpoint/pre-reclassify` HEAD `15830bd`
**Linked:**
- `docs/ai/specs/stage20-judge-verdict.md` (V1 landing addendum)
- `docs/ai/tasks/T-JUDGE-HYBRID-V2.md` (this supersedes the deferred V2 placeholder — no API budget needed; we use Codex CLI subscription)
- `DECISIONS.md` D-010 amendment
- `docs/ai/tasks/T-JUDGE-DEFAULT-MINIMAX-V1.md` (V1 ship spec)
- `docs/ai/tasks/T-JUDGE-HYBRID-V1.md` + `T-JUDGE-HYBRID-V1-FIX.md` (V1 retired hybrid; V2-CLI replaces with conservative design)

## Why this exists (and why now)

V1 ships as MM-only because the V1 hybrid (max-of-rule+evidence+MM) over-credited via rule false positives and evidence comprehensive-card credit. The Stage 20 verdict required a second LLM judge family for cross-family validation, but I assumed that needed Anthropic credit or OpenAI key (no budget). **Codex CLI uses ChatGPT subscription — no marginal cost — and is OpenAI-family vs MiniMax**. Same for Claude Code CLI vs MiniMax. So cross-family validation is achievable today.

V2-CLI delivers cross-family validation as a **conservative diagnostic layer first**, NOT as an automatic V1 replacement. Promotion to official is gated on quantitative thresholds (see "V2 promotion criteria" below).

## Design principles (must hold)

1. **MM and Codex are co-primary semantic judges.** Equal voting weight.
2. **AND-gate full credit.** Per-keypoint `hit_score = 1.0` IFF both MM and Codex say hit. Disagreement → `0.0` + diagnostic flag. **Never partial credit on disagreement** — partial credit is the V1 over-credit trap repeated in a different shape.
3. **Rule and evidence are diagnostic-only in V2.** Their `*_credit` fields stay in artifacts for analysis, but **do not enter the score formula**. Rule cannot independently rescue a keypoint; evidence cannot either.
4. **V2 is mathematically conservative.** `V2_mean ≤ V1_MM_only_mean` always (because MM-yes-Codex-no cases that V1 credited 1.0 now score 0). This is intentional: the gain from V2 is *trustworthiness*, not score inflation.
5. **Codex judge is locked-down.** No repo reading, no search, no agentic exploration. Single-shot prompt with answer + keypoints; strict JSON output.

## Per-keypoint classification

```python
def classify_v2(rule_hit, evidence_hit, mm_hit, codex_hit) -> tuple[float, str]:
    """Returns (hit_score, primary_label). rule_hit and evidence_hit
    become sub-flags carried in the per-keypoint record."""
    if mm_hit and codex_hit:
        return 1.0, "both_yes"
    if not mm_hit and not codex_hit:
        return 0.0, "both_no"
    if mm_hit and not codex_hit:
        return 0.0, "mm_yes_codex_no"
    return 0.0, "codex_yes_mm_no"
```

The richer 8-cell diagnostic taxonomy lives in `summary["v2_disagreement_taxonomy"]`:

| Cell | Primary | Sub-flag | Interpretation |
|---|---|---|---|
| `both_yes_rule_yes_evidence_yes` | 1.0 | rule+ev | Highest-confidence hit |
| `both_yes_rule_no_evidence_yes` | 1.0 | ev only | Paraphrased answer over correct evidence (Stage 20A target case) |
| `both_yes_rule_no_evidence_no` | 1.0 | none | **Warning**: LLM consensus without local evidence — possible hallucination |
| `mm_yes_codex_no` | 0.0 | varies | Disputed; MM might over-credit OR Codex might miss paraphrase |
| `codex_yes_mm_no` | 0.0 | varies | Disputed; same |
| `both_no_rule_yes` | 0.0 | rule | **Rule false-positive candidate** — directly answers Stage 20A audit pattern |
| `both_no_evidence_yes` (rule_no) | 0.0 | ev | **Retrieval-grounded but answer didn't articulate** — Stage 20C cards-v2 / answer-prompt signal |
| `both_no_rule_no_evidence_no` | 0.0 | none | True miss |

`both_no_evidence_yes` count is the **most actionable Stage 20C input**: it directly measures "answer synthesis missed retrievable facts".

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py`
   - argparse `--judge-mode`: add `hybrid_v2` to choices (keep `hybrid` for V1 backward-compat experimental flag).
   - `KeypointJudge._judge_with_hybrid_v2` new method: invokes rule rung, evidence rung, MM (`_judge_with_minimax`), Codex (`_judge_with_codex`); classifies per `classify_v2`; populates per-keypoint `KeypointHitDetail` with all 4 rung credits + primary label.
   - `KeypointJudge.judge` dispatch: route `requested_mode == "hybrid_v2"` to the new method.
   - `_judge_family_metadata`: when mode is `hybrid_v2` AND both rungs succeeded, return `(2, True, [])`. If Codex rung failed for ≥1 record, return `(2, False, ["Codex rung failed on N/M records — partial cross-family validation"])`.
   - `build_summary` adds:
     - `v2_disagreement_taxonomy: dict[str, int]` — count per 8 cells (zeros when not in V2 mode).
     - `v2_codex_failure_count: int` — records where Codex rung failed.
     - `v2_disagreement_rate: float` — `(mm_yes_codex_no + codex_yes_mm_no) / total_keypoints`.
2. `apps/backend/scripts/rejudge_run.py`
   - argparse mirror.
   - Mirror summary fields.
3. `apps/backend/tests/scripts/test_run_qa_benchmark.py`
   - 5 new tests:
     - `test_hybrid_v2_credits_only_when_both_llms_agree` — MM=yes, Codex=yes → 1.0; one says no → 0.0.
     - `test_hybrid_v2_does_not_use_rule_for_scoring` — rule=hit, MM=miss, Codex=miss → 0.0 with `primary="both_no"` and sub-flag `rule_hit=true`.
     - `test_hybrid_v2_does_not_use_evidence_for_scoring` — evidence=hit alone → 0.0 with sub-flag `evidence_hit=true`.
     - `test_hybrid_v2_taxonomy_categorizes_correctly` — covers all 8 cells with synthetic inputs.
     - `test_hybrid_v2_codex_failure_records_partial_validation` — when Codex raises, MM result alone is insufficient; record marked, mean is unaffected by that keypoint (treated as `both_no`).

## Codex CLI judge constraints (mandatory in implementation)

If `_judge_with_codex` already implements these, leave it alone. If not, V2 implementation MUST tighten it:

1. **Sandbox**: invoke `codex exec --sandbox read-only` (NOT `workspace-write`). The judge must not be able to mutate state.
2. **Prompt template** (locked):
   ```
   You are a strict benchmark judge. For each expected keypoint, decide
   whether the answer text below explicitly conveys the keypoint's meaning.

   Question: <q>
   Answer: <a>
   Expected keypoints (numbered):
     1. <kp1>
     2. <kp2>
     ...

   Reply with JSON only, exactly this shape:
   {"hits": [true, false, ...]}

   The hits array must have exactly N booleans, one per keypoint, in order.
   Do NOT search for, read, or reference any files. Do NOT use any tools.
   Judge ONLY based on the answer text provided.
   ```
3. **Per-Q batching**: ONE codex CLI invocation per question (not per keypoint). Returns list of bools. Critical for rate-limit compliance — 26 + 34 = 60 invocations for full re-rejudge, well under ChatGPT Plus subscription limits.
4. **Timeout**: 30s hard. On timeout: record `judge_status_codex_rung="timeout"`, all keypoints for that record treated as `codex_hit=False`.
5. **JSON parse**: strict. Malformed response → `judge_status_codex_rung="parse_error"`, treat as Codex-miss for the record.
6. **Rate-limit handling**: if Codex CLI returns rate-limit error, sleep 30s and retry once; second failure → record-level Codex rung failure.

## V2 promotion criteria (must all be true to make V2 official default)

```
Promote V2 → official default IFF:
  agreement_fraction = (both_yes + both_no) / total_keypoints   ≥ 0.70
  |V2_mean_handymanapp − V1_MM_handymanapp|                     ≤ 3.0
  |V2_mean_dashboard   − V1_MM_dashboard|                       ≤ 3.0
  |V2_cross_stack_gap  − V1_MM_cross_stack_gap (+8.46)|         ≤ 3.0
  v2_codex_failure_count / total_records                        < 0.10
```

Otherwise V2 stays as `--judge-mode hybrid_v2` (cross-family diagnostic), V1 (`minimax`) stays as official default. Promotion is a separate ticket (`T-JUDGE-V2-PROMOTE-TO-DEFAULT` if criteria pass).

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py scripts/rejudge_run.py tests/scripts/test_run_qa_benchmark.py` clean.
- All 36 existing tests pass + 5 new V2 tests pass = 41 total.
- `python -m scripts.rejudge_run --in-run tests/benchmarks/runs/qa-run-20260501T000245Z.jsonl --backend-url http://127.0.0.1:8000 --judge-mode hybrid_v2 --out-run tests/benchmarks/runs/qa-rejudge-handymanapp-hybrid-v2.jsonl` completes successfully (single sample mode; multi-sample is not in V2-CLI scope).
- Same for dashboard.
- Resulting artifacts contain:
  - `judge_family_count == 2`, `cross_family_validated` true if no codex failures.
  - `v2_disagreement_taxonomy` populated with all 8 cells (zeros allowed).
  - `v2_disagreement_rate` populated.
  - Mean ≤ V1 MM-only mean per dataset (mathematical invariant; spec test verifies this).

## Sanity invariants

1. **Conservative invariant**: `V2_mean ≤ V1_MM_only_mean` per dataset (spec test verifies on synthetic data).
2. **AND-gate invariant**: per keypoint, `hit_score == 1.0 IFF (mm_hit AND codex_hit)`.
3. **Rule isolation invariant**: per keypoint, `hit_score` does not depend on `rule_credit` (verifiable: hit_score with rule_hit=True equals hit_score with rule_hit=False, holding mm_hit and codex_hit fixed).
4. **Evidence isolation invariant**: same for `evidence_credit`.
5. **Codex failure resilience**: if Codex rung errors on a record, the record gets `codex_hit=False` for all keypoints + `judge_status_codex_rung` field set; bench does NOT abort.

## Out of scope

- Replacing V1 default (separate promote ticket if criteria pass).
- Modifying or expanding the V1 hybrid mode (V1 hybrid stays unchanged for audit reproducibility).
- Calibration dataset T-JUDGE-AMBIG-CALIBRATION (V2 with Codex CLI doesn't need it for shipping; calibration is for V3 weight tuning if ever).
- Tunable rung weights — V2 has no weights, just AND-gate.
- Multi-sample voting in V2 (Q × samples × 2 LLM families is rate-limit-risky; defer to V2.5).

## Workflow

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/judge-hybrid-v2-cli" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-JUDGE-HYBRID-V2-CLI.md
```

Worktree NEW from `checkpoint/pre-reclassify` HEAD `15830bd` on branch `feat/judge-hybrid-v2-cli`.

**Important sandbox note**: Codex sandbox cannot load `apps/backend/.env` from outside its writable area. After codex commits its changes, the parent session (Tomonkyo bash) runs the smoke against handymanapp + dashboard with real Codex CLI access — codex sandbox can only run unit tests, not the live LLM judge calls.

## Why P1 not P2

V1 ships with explicit single-family caveat. V2-CLI removes that caveat at zero cost. It also generates the disagreement taxonomy that directly answers Stage 20C's "is the gap real or judge artifact" question. Without V2 cross-family validation, every Stage 20+ benchmark on a non-React stack continues to be reported with the single-LLM caveat.
