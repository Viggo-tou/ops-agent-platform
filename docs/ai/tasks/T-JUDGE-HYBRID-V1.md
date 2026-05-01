# T-JUDGE-HYBRID-V1 — Hybrid 3-rung judge (rule + cited-evidence + MiniMax)

<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P1 — Stage 20A primary deliverable)
**Priority:** P1 (D-010, blocks all cross-stack benchmarks)
**Created:** 2026-05-01
**Branch:** `feat/judge-hybrid-v1` based on `checkpoint/pre-reclassify` HEAD `db5ee82`
**Linked decision:** `DECISIONS.md` D-010
**Linked verdict:** `docs/ai/specs/stage20-judge-verdict.md`

## Context (do not edit)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Background

The Stage 19 cross-stack rejudge (see `docs/ai/specs/stage20-judge-verdict.md`) demonstrated that the rule-only `KeypointJudge` systematically under-scores Android/Kotlin paraphrases. Cross-family rejudge with MiniMax compressed the dashboard→handymanapp gap from rule-judge **+25.12** to semantic-judge **+8.46** — most of the gap was judge artifact, not retrieval/cards failure. D-010 promotes the hybrid judge to Stage 20A PRIMARY.

V1 implements a 3-rung hybrid judge using only rule + cited-evidence + MiniMax. A second LLM family (Anthropic/OpenAI) for cross-family disagreement tracking is **deferred to T-JUDGE-HYBRID-V2** because the Anthropic API key currently has zero credit balance and OpenAI is not configured. V1 is strictly more useful than rule-only OR MiniMax-only and unblocks the n≥40 rebench.

## Goal

Add a `hybrid` judge mode to `KeypointJudge` that, for each expected keypoint, takes the maximum credit across three independent rungs:

- **Rule rung** — current `_judge_with_rule` logic. Credit `1.0` if it matches, else `0.0`.
- **Cited-evidence rung** — `0.6` if any returned citation's `card_text` contains a keypoint substring (token-level, ≥4 chars). The intent is to credit answers that are clearly grounded in retrieved evidence even when the answer's surface wording diverges from the keypoint's wording.
- **MiniMax LLM rung** — `0.7` if MiniMax (`_judge_with_minimax`) confirms semantic match for that keypoint.

Per-Q `kp_coverage = mean(hit_scores)` where `hit_score ∈ [0.0, 1.0]`. Final score formula `kp_coverage * 60 + citation_precision * 40` is unchanged. Existing rule/minimax/anthropic/codex/claude_code modes remain available unchanged.

## Design

### A. Signature change to `KeypointJudge.judge`

Add an optional `citations` keyword argument so the evidence rung can inspect retrieved card_text. `citations` is a list of dicts with at least `source_name`, `relative_path`, `card_text` (the same shape `compute_retrieval_diagnostics` already receives). When `citations is None`, the evidence rung silently contributes 0 (preserves backwards compat for callers that don't have citations available, e.g. ad-hoc judge tests).

```python
def judge(
    self,
    *,
    question: str,
    answer: str,
    keypoints: Sequence[str],
    citations: Sequence[dict[str, Any]] | None = None,
) -> tuple[list[KeypointHitDetail], str]:
    ...
```

`KeypointHitDetail` is a new TypedDict (or simple dict structure) with fields:

```python
{
    "keypoint": str,
    "hit": bool,           # derived: hit_score >= 0.5
    "hit_score": float,    # 0.0 to 1.0
    "rule_credit": float,  # 0.0 or 1.0
    "evidence_credit": float,  # 0.0 or 0.6
    "llm_credit": float,   # 0.0 or rung weight (e.g. 0.7); 0.0 if rung not run or errored
    "provenance": str,     # "rule" | "evidence" | "llm" | "miss"
}
```

Provenance is the rung name that supplied the maximum credit (ties broken in this preference order: `rule` > `llm` > `evidence` so that the strongest available evidence wins). If `hit_score == 0`, provenance is `miss`.

For non-hybrid modes (`rule`, `minimax`, `anthropic`, `claude_code`, `codex`, `auto`), populate the relevant rung's credit only and leave the others at 0.0. This keeps all artifacts uniform.

### B. New `_judge_with_hybrid` method

```python
def _judge_with_hybrid(
    self,
    *,
    question: str,
    answer: str,
    keypoints: Sequence[str],
    citations: Sequence[dict[str, Any]] | None,
) -> list[KeypointHitDetail]:
    rule_hits = self._judge_with_rule(answer=answer, keypoints=keypoints)

    # Cached evidence: keypoint → bool (substring match against any citation card_text)
    evidence_hits = self._evidence_rung(keypoints=keypoints, citations=citations or [])

    # MiniMax LLM rung: list[bool] per keypoint, runs once for the whole batch
    try:
        llm_hits = self._judge_with_minimax(question=question, answer=answer, keypoints=keypoints)
    except Exception:
        llm_hits = [False] * len(keypoints)

    details: list[KeypointHitDetail] = []
    for kp, rule, evidence, llm in zip(keypoints, rule_hits, evidence_hits, llm_hits):
        rule_c = 1.0 if rule else 0.0
        evidence_c = 0.6 if evidence else 0.0
        llm_c = 0.7 if llm else 0.0
        candidates = [("rule", rule_c), ("llm", llm_c), ("evidence", evidence_c)]  # tie-break order
        provenance, score = max(candidates, key=lambda t: t[1])
        if score == 0.0:
            provenance = "miss"
        details.append(
            {
                "keypoint": kp,
                "hit": score >= 0.5,
                "hit_score": score,
                "rule_credit": rule_c,
                "evidence_credit": evidence_c,
                "llm_credit": llm_c,
                "provenance": provenance,
            }
        )
    return details
```

### C. Evidence rung helper

```python
def _evidence_rung(
    self,
    *,
    keypoints: Sequence[str],
    citations: Sequence[dict[str, Any]],
) -> list[bool]:
    """Per-keypoint bool: does any citation's card_text contain the keypoint
    (full normalized substring OR any 4+ char alphanumeric token)?
    Reuses the same _any_kp_substring logic the harness already uses for
    `expected_fact_in_card` to keep evidence-rung semantics consistent
    with the existing diagnostics field."""
    hits: list[bool] = []
    card_texts = [
        str(citation.get("card_text") or "").strip().lower()
        for citation in citations
    ]
    for keypoint in keypoints:
        matched = False
        for card_text in card_texts:
            if not card_text:
                continue
            if _any_kp_substring([keypoint], card_text):
                matched = True
                break
        hits.append(matched)
    return hits
```

### D. Bench harness wire-up (`run_qa_benchmark.py` main loop)

In the per-Q execution path that currently calls `judge.judge(question=..., answer=..., keypoints=...)`, also pass the `citations` parameter using the structured citations already extracted via `extract_answer_and_citations`. The structured citations include `card_text` when the backend returned it.

The current code returns `keypoint_hits, judge_mode_used`. Update to handle the new return type — `keypoint_hits` becomes `list[KeypointHitDetail]` (a richer object). Compute `kp_coverage` from `hit_scores` (`sum(d["hit_score"] for d in keypoint_hits) / max(len(keypoint_hits), 1)`).

Update the per-record artifact field:
```python
"keypoint_hits": keypoint_hits,  # now richer dicts with rung breakdown
```

The `keypoint_hits` field's per-entry shape is the new `KeypointHitDetail` shape for ALL modes. For non-hybrid modes, the non-active rungs are 0.0 — schema is uniform.

### E. CLI mode

Add `hybrid` to the `--judge-mode` argparse choices alongside the existing modes. `--judge-mode hybrid` should run the new path. `auto` mode keeps its current chain (claude_code → minimax → rule fallback); it is not changed in V1.

### F. Summary aggregates (new fields)

In the summary record, add:

```python
summary["judge_rung_usage"] = {
    "rule_only": <count of records where every keypoint was credited solely by the rule rung>,
    "evidence_only": <... by evidence rung only>,
    "llm_only": <... by LLM rung only>,
    "rule_and_llm": <... where rule and llm both contributed (any keypoint)>,
    "rule_and_evidence": <...>,
    "llm_and_evidence": <...>,
    "all_three": <... where all three rungs contributed (any keypoint)>,
    "all_miss": <... where every keypoint was a miss across all rungs>,
}
summary["per_rung_kp_hit_rate"] = {
    "rule": <fraction of keypoints across the run with rule_credit > 0>,
    "evidence": <... with evidence_credit > 0>,
    "llm": <... with llm_credit > 0>,
}
summary["disagreement_count"] = <count of records with at least one keypoint where rule/evidence/llm rungs disagree on the hit decision>
```

These aggregates are only populated meaningfully in `hybrid` mode but should be present (zeros) for other modes for schema uniformity.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py`
   - Add `hybrid` to argparse choices
   - Add `KeypointHitDetail` TypedDict (or just dict; TypedDict preferred for type clarity)
   - Add `_judge_with_hybrid` method on `KeypointJudge`
   - Add `_evidence_rung` helper
   - Update `judge()` dispatch in `KeypointJudge` to handle `hybrid`
   - Update `judge()` signature to accept optional `citations` kwarg
   - Update non-hybrid dispatch to wrap their bool hits in the new `KeypointHitDetail` shape
   - In main loop: pass `citations=structured_citations` to `judge.judge(...)`; use new `keypoint_hits` shape downstream
   - Update score computation to use `hit_score` instead of bool
   - Update summary aggregates with the 3 new fields
2. `apps/backend/scripts/rejudge_run.py`
   - Add `hybrid` to argparse choices
   - Update `keypoints()` helper if it relies on the old `{"keypoint", "hit"}` shape (it does — line 56-60 of current rejudge_run.py extracts `item.get("keypoint")` from `keypoint_hits`; should still work since we keep `keypoint` field, but verify)
   - Pass `citations` parameter through `judge.judge(...)` call
   - Update per-record output to use the new shape
3. `apps/backend/tests/scripts/test_run_qa_benchmark.py`
   - Add tests (see Acceptance)

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py scripts/rejudge_run.py tests/scripts/test_run_qa_benchmark.py` clean
- All existing 27 tests in `test_run_qa_benchmark.py` still pass after the schema change (modes other than hybrid must keep producing equivalent score outputs; only the artifact's per-keypoint shape gets richer)
- 6 new tests pass:
  - `test_hybrid_credits_rule_when_rule_matches` — keypoint that rule rung matches → `provenance="rule"`, `hit_score=1.0`
  - `test_hybrid_credits_evidence_when_only_card_text_contains_keypoint` — keypoint substring in card_text but not in answer → `provenance="evidence"`, `hit_score=0.6`
  - `test_hybrid_credits_llm_when_only_paraphrase` — keypoint not in answer or card_text but MiniMax returns hit → `provenance="llm"`, `hit_score=0.7` (test fakes the MiniMax client)
  - `test_hybrid_returns_zero_when_all_rungs_miss` — `provenance="miss"`, `hit_score=0.0`
  - `test_hybrid_max_picks_rule_over_lower_rungs` — when rule and evidence both fire, provenance is `rule` and hit_score is `1.0`
  - `test_hybrid_summary_aggregates_per_rung_hit_rate` — end-to-end run with hybrid mode emits `per_rung_kp_hit_rate`, `judge_rung_usage`, `disagreement_count`
- Manual smoke: `python -m scripts.rejudge_run --in-run tests/benchmarks/runs/qa-run-20260501T000245Z.jsonl --backend-url http://127.0.0.1:8000 --judge-mode hybrid --out-run tests/benchmarks/runs/qa-rejudge-handymanapp-hybrid.jsonl` runs to completion. Resulting per-tier means must satisfy the **sanity invariant**: for each tier, `rule_mean ≤ hybrid_mean ≤ minimax_mean + epsilon`. (Hybrid takes max across rungs, so it cannot be lower than rule and is bounded by the strongest rung; epsilon=2.0 to absorb evidence rung edge cases that flip the bound.)

## Sanity invariants (must hold or the implementation is wrong)

1. For every record: `hit_score == max(rule_credit, evidence_credit, llm_credit)`.
2. For every record: `kp_coverage <= 1.0`.
3. For every record in `hybrid` mode: if rule rung matches a keypoint, that keypoint's `hit_score == 1.0` regardless of other rungs.
4. For every record where MiniMax errored (network/credit), `llm_credit == 0` and the record's `error` field captures the failure; bench does not abort.

## Out of scope (V2)

- Second LLM judge family (Anthropic / OpenAI) for cross-family disagreement tracking. **T-JUDGE-HYBRID-V2** picks this up after Anthropic credit is restored or OpenAI is configured.
- Tunable rung weights (currently hard-coded `1.0 / 0.6 / 0.7`).
- Symbol-aware judge (AST-based exact symbol match) — separate `T-JUDGE-SYMBOL-AWARE` if needed.
- Rejudge of dashboard or handymanapp official artifacts with hybrid (this is a separate ticket: `T-STAGE19-HYBRID-REBENCH`, runs after V1 lands).

## Workflow

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/judge-hybrid-v1" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-JUDGE-HYBRID-V1.md
```

Worktree: NEW `D:/项目/ops-worktrees/judge-hybrid-v1` on `feat/judge-hybrid-v1` based on `checkpoint/pre-reclassify` HEAD `db5ee82`.

## Why P1

D-010 promoted Stage 20A to PRIMARY. Without hybrid judge, every future cross-stack benchmark (Java backends, Go services, Rust, etc.) will be misread the same way Stage 19's Android numbers were. This blocks Stage 20C (cards-v2 decision needs n≥40 rebench under hybrid) and blocks any general claim about cross-stack quality.
