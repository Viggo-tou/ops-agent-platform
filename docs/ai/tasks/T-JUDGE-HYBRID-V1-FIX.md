# T-JUDGE-HYBRID-V1-FIX — Scope evidence rung to expected-citation card_text only

<!-- Effort: small -->
<!-- Executor: codex -->

**Status:** todo (P1 — blocks merge of T-JUDGE-HYBRID-V1)
**Priority:** P1 (Stage 20A correctness gate)
**Created:** 2026-05-01
**Branch:** continue on `feat/judge-hybrid-v1` (the worktree at `D:/项目/ops-worktrees/judge-hybrid-v1`)
**Linked:** `docs/ai/tasks/T-JUDGE-HYBRID-V1.md`, `docs/ai/specs/stage20-judge-verdict.md`

## Background

T-JUDGE-HYBRID-V1 is implemented (33 tests green). Sanity smoke against the handymanapp 26Q artifact revealed an evidence-rung over-credit pattern: 4 records (A-14, B-12, C-11, D-07) all with `citation_precision ≤ 0.25` (retrieval got mostly wrong files) gained +26-27 hybrid points purely from the evidence rung matching keypoint substrings in **non-expected** files' card_text.

**Per-rung hit rates from the smoke run**:

| Rung | Keypoint hit rate |
|---|---|
| rule | 24% |
| evidence | **76%** |
| llm | 45% |

Evidence firing on 76% of keypoints is implausibly high — the existing `expected_fact_in_card_rate` (which scopes to expected-file card_text only) sits closer to the 30-50% range when retrieval is imperfect. The discrepancy is because the V1 evidence rung matches against **any** retrieved card_text, including unrelated files retrieved alongside expected ones.

**Tier-level distortion** (handymanapp 17 valid in all 3 judges):

| Tier | Rule | MM | Hybrid | hyb−mm |
|---|---|---|---|---|
| A | 30.48 | 56.19 | 57.33 | +1.14 (clean) |
| B | 41.33 | 58.33 | 57.73 | -0.60 (clean) |
| C | 36.67 | 38.33 | **60.33** | **+22.00** (contaminated) |
| D | 10.00 | 22.00 | **48.40** | **+26.40** (contaminated, n=1) |

A and B are clean — evidence rarely fires there because retrieval is more accurate at lower tiers. C/D are where retrieval struggles; evidence rung fires on noise.

**Root cause**: the V1 spec (`docs/ai/tasks/T-JUDGE-HYBRID-V1.md`) said "evidence_credit = 0.6 if any retrieved card_text contains keypoint substring". Codex implemented the spec faithfully. The spec's scope was wrong — evidence credit should require the card_text to come from an **expected** citation.

The evidence rung is still useful as designed — answers like `"uses Firebase authentication"` paired with an expected card containing `FirebaseAuth.getInstance()` should earn partial credit. But the credit must require the card_text to be from a file the question expected.

## Goal

Scope the evidence rung to expected-citation card_text only. **Do NOT drop the rung.** Reuse the existing `_matches_citation` helper (already in `run_qa_benchmark.py`) so the alias normalization (`handyman-admin-dashboard` → `hosteddashboard`) is consistent with `compute_retrieval_diagnostics` / `expected_fact_in_card`.

## Design

### A. Signature change

`KeypointJudge.judge` and `_judge_with_hybrid` need access to the row's expected_citations to filter card_texts. Add an optional `expected_citations: Sequence[str] | None = None` parameter.

```python
def judge(
    self,
    *,
    question: str,
    answer: str,
    keypoints: Sequence[str],
    citations: Sequence[dict[str, Any]] | None = None,
    expected_citations: Sequence[str] | None = None,
) -> tuple[list[KeypointHitDetail], str]:
    ...
```

`expected_citations` is a list of canonical-path strings (the same shape `row.expected_citations` already has, e.g. `["handyman-admin-dashboard/src/pages/Login.js"]`).

### B. Updated `_evidence_rung`

```python
def _evidence_rung(
    self,
    *,
    keypoints: Sequence[str],
    citations: Sequence[dict[str, Any]],
    expected_citations: Sequence[str] | None,
) -> list[bool]:
    """Per-keypoint bool: does any *expected* citation's card_text contain
    the keypoint? Citations that don't match any expected_citations entry
    contribute nothing.

    Reuses _matches_citation for source-name aliasing so the semantic is
    identical to compute_retrieval_diagnostics' expected_fact_in_card field.
    """
    if not expected_citations:
        return [False] * len(keypoints)

    expected_card_texts: list[str] = []
    for citation in citations:
        returned = _citation_identity(citation)
        if any(_matches_citation(returned, expected) for expected in expected_citations):
            card_text = str(citation.get("card_text") or "").strip().lower()
            if card_text:
                expected_card_texts.append(card_text)

    if not expected_card_texts:
        return [False] * len(keypoints)

    hits: list[bool] = []
    for keypoint in keypoints:
        matched = any(
            _any_kp_substring([str(keypoint)], card_text)
            for card_text in expected_card_texts
        )
        hits.append(matched)
    return hits
```

When `expected_citations` is None or empty, evidence rung returns all False — preserves the current "no expected_citations available" fallback shape used by ad-hoc judge tests.

### C. Pass-through call sites

Three call sites need to thread `expected_citations`:

1. **`run_qa_benchmark.py` main loop**: `judge.judge(..., citations=structured_citations, expected_citations=row.expected_citations)`. `row.expected_citations` is already on `DatasetRow`.
2. **`rejudge_run.py`**: `expected = [str(item) for item in record.get("expected_citations") or []]` is already extracted at line ~119; pass it as `expected_citations=expected`.
3. **Test helpers** (`test_run_qa_benchmark.py`): the existing 6 hybrid tests construct citations explicitly. Update them to also pass an `expected_citations` list when validating the evidence rung.

### D. Backward compat

Non-hybrid modes don't call the evidence rung. Their `judge.judge()` calls don't need the new parameter (it's optional with default None).

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py` — `KeypointJudge._evidence_rung`, `_judge_with_hybrid`, `judge()` signature, main loop call site.
2. `apps/backend/scripts/rejudge_run.py` — `judge()` call site (line ~142).
3. `apps/backend/tests/scripts/test_run_qa_benchmark.py` — update 2-3 existing hybrid tests to pass `expected_citations`; add 2 new tests:
   - `test_hybrid_evidence_credits_only_expected_citation_card_text` — citation matches an expected_citations entry → evidence credit fires.
   - `test_hybrid_evidence_does_not_credit_wrong_file_card_text` — citation does NOT match any expected_citations entry → evidence credit is 0 even when keypoint substring is in that card_text.

## Acceptance

1. `python -m compileall scripts/run_qa_benchmark.py scripts/rejudge_run.py tests/scripts/test_run_qa_benchmark.py` clean.
2. **All 33 existing tests still pass** (the 27 pre-V1 baseline + 6 hybrid tests, with the 6 updated to pass `expected_citations`).
3. **2 new tests pass** (above).
4. **Sanity invariant 1 still holds**: `hit_score == max(rule_credit, evidence_credit, llm_credit)` per keypoint.
5. **Re-run hybrid smoke** (`python -m scripts.rejudge_run --in-run tests/benchmarks/runs/qa-run-20260501T000245Z.jsonl --backend-url http://127.0.0.1:8000 --judge-mode hybrid --out-run tests/benchmarks/runs/qa-rejudge-handymanapp-hybrid-fixed.jsonl`):
   - **A-14 phantom delta disappears**: hybrid score must drop from 56 back to ≤ 32 (close to max(rule=30, mm=30) ≈ 30; the 0.7 LLM weight may give ≤32 if MM hits some keypoints we couldn't credit before).
   - **B-12 phantom delta disappears**: hybrid score must drop from 27 back to 0 (rule 0, mm 0, no expected-citation evidence to credit).
   - **C-11 phantom delta disappears**: hybrid score must drop from 37 back to ≤ 12 (rule 10, mm 10, expected-evidence may add a small lift if any keypoints hit expected card).
   - **D-07 phantom delta disappears**: hybrid drops from 48.4 back to ≤ 25 (rule 10, mm 22).
6. **A/B-tier means stay within ±2 points of the previous V1 hybrid run** (A 57.33, B 57.73). These are the cases where evidence rung was firing on legitimate expected-card matches; scoping should not change them materially.
7. **Per-rung evidence hit rate must drop** from 76% to a value matching the existing `expected_fact_in_card_rate` semantic (likely 40-65% on this dataset, since expected files are retrieved at top1 most of the time but not always with card_text populated).
8. **No new regression in disagreement_count or judge_rung_usage shape** — the aggregates' field names and existence stay; the values shift.

## Sanity invariants (must hold)

1. `hit_score == max(rule_credit, evidence_credit, llm_credit)` per keypoint (unchanged).
2. **NEW**: when no citation matches any expected_citation, `evidence_credit == 0.0` for every keypoint of that record.
3. When `expected_citations is None or empty`, evidence rung is a no-op (all 0).

## Out of scope

- Any change to rule rung or LLM rung weights/logic.
- Adding a second LLM family (still T-JUDGE-HYBRID-V2 after credit/key restored).
- Changing the citation_precision computation (it already uses `_matches_citation` correctly).

## Workflow

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/judge-hybrid-v1" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-JUDGE-HYBRID-V1-FIX.md
```

Continues on the existing `feat/judge-hybrid-v1` branch in the same worktree (no new branch). Codex's previous changes from T-JUDGE-HYBRID-V1 stay; this is an additive correction.

**Note for the codex sandbox**: the worktree's `apps/backend/.env` was copied in by the parent session for the smoke run. If sandbox reset wiped it, recreate before running the smoke (the smoke is in the parent's responsibility per the Tomonkyo bash split rule, but tests don't need .env).

## Why not drop evidence rung

The rung captures legitimate signal: "answer paraphrases evidence that's correctly retrieved" (e.g. answer says "uses Firebase authentication" while the expected card_text contains `FirebaseAuth.getInstance()`). This is exactly the cross-stack paraphrase case Stage 20A is designed to credit. Dropping the rung removes this signal — A-tier hybrid lift would drop from +1.14 to ~0 vs MM-only.

The fix is to constrain the rung to expected-citation card_text. Then it credits real groundedness without rewarding wrong-file retrieval.
