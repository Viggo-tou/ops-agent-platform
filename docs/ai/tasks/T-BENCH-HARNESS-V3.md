# T-BENCH-HARNESS-V3 — Per-Q source filtering + retrieval diagnostics + Stage 19 buckets

<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P1 — Stage 19 prerequisite for clean cards-generalization signal)
**Priority:** P1 (Stage 19; previous run was contaminated by global FTS5 across multiple sources)
**Created:** 2026-05-01
**Branch:** `feat/bench-harness-v3` based on `checkpoint/pre-reclassify` HEAD `12c2067`

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Three additions on top of T-BENCH-HARNESS-V2 (already merged):

1. **Per-Q source filtering**: dataset now has top-level `source_name` field per question. Bench harness MUST pass it as the `source_name` parameter on every `/api/knowledge/search` call. In multi-source mode (>1 distinct source_name in dataset), missing `source_name` is a HARD ERROR — abort the bench rather than silently fall back to global retrieval.

2. **Retrieval-quality diagnostics per Q**: capture three signals per question that disambiguate failure layer (codex's c+d advice for cards-on-Kotlin):
   - `expected_citation_top_rank`: position (1-indexed) of the FIRST expected citation in the search response's `citations` list, or `null` if absent
   - `top_k_source_distribution`: `{source_name: count}` over the top-K returned citations
   - `wrong_source_in_top_k`: `int` count of citations whose source ≠ requested `source_name`
   - `expected_fact_in_card`: `bool | null` — does the card_text of the expected file (if retrieved) contain ANY of the expected_answer_keypoints' substring? `null` if no expected file in DB or no card.

3. **Stage 19 failure buckets**: extend the existing failure bucket classifier with cards-on-Kotlin signals. Allow OVERLAP (a Q may carry multiple bucket tags):
   - `retrieval_wrong_source`: any wrong-source citation in top-K, OR expected_citation_top_rank is null
   - `retrieval_right_source_wrong_file`: source matches but expected_citation not in top-K
   - `card_missing_keypoint_facts`: expected file retrieved but its card_text doesn't contain expected_answer_keypoints substrings
   - `cross_file_reasoning_miss`: question requires C/D-tier reasoning, expected files retrieved, answer scores low (kp_coverage < 0.3) — points at synthesis layer
   - `domain_jargon_miss`: keypoints contain Android-specific terms (Composable, Fragment, ViewModel, navController, Firebase node names) and answer doesn't mention them

   These are heuristic markers; not mutually exclusive with the existing infra/synthesis/judge buckets.

## Design

### A. Dataset schema (already migrated externally, document here)

Each question has top-level `source_name` field:

```json
{
  "id": "A-01",
  "tier": "A",
  "source_name": "hosteddashboard",
  "question": "...",
  "expected_answer_keypoints": [...],
  "expected_citations": ["handyman-admin-dashboard/src/pages/Login.js"]
}
```

Allowed values: `hosteddashboard`, `handymanapp` (extensible). Validator at parse time:
- If multi-source dataset (>1 distinct value across all rows) and any row missing `source_name` → exit 2 with clear error

### B. Pass source_name on search

In `BenchmarkClient.submit_question` (or wherever `/api/tasks` is posted with payload that includes the search query): include `source_name` in the request payload. The orchestrator already passes payload to `_execute_knowledge_search` which calls `KnowledgeService.search_repositories(query=..., source_name=...)`. Verify the source_name is plumbed through end-to-end. Add a smoke check in the harness: after submitting Q1, fetch the resulting trace and assert `selected_sources == [request.source_name]`. If not, exit 2 (backend filter is broken — not a bench bug).

### C. Diagnostics computation

After receiving the search response (trace + citations) for each Q:

```python
def compute_retrieval_diagnostics(record, trace, citations) -> dict:
    expected = record["expected_citations"]
    expected_first_rank = None
    for i, cit in enumerate(citations, start=1):
        cit_str = f"{cit['source_name']}/{cit['relative_path']}"
        for exp in expected:
            if _matches_citation(cit_str, exp):  # see helper below
                expected_first_rank = i
                break
        if expected_first_rank is not None:
            break

    src_dist = Counter(c["source_name"] for c in citations)
    requested = record["source_name"]
    wrong_source = sum(c for s, c in src_dist.items() if s != requested)

    # expected_fact_in_card: best-effort — only if expected file is in citations
    fact_in_card = None
    for cit in citations:
        cit_str = f"{cit['source_name']}/{cit['relative_path']}"
        if any(_matches_citation(cit_str, exp) for exp in expected):
            card_text = (cit.get("card_text") or "").lower()
            if card_text:
                kps = record.get("expected_answer_keypoints", [])
                # match if at least 1 keypoint has a 4+ char substring in card
                fact_in_card = _any_kp_substring(kps, card_text)
            break

    return {
        "expected_citation_top_rank": expected_first_rank,
        "top_k_source_distribution": dict(src_dist),
        "wrong_source_in_top_k": wrong_source,
        "expected_fact_in_card": fact_in_card,
    }


def _matches_citation(returned: str, expected: str) -> bool:
    """Match expected (with possibly aliased source name) against returned citation."""
    aliases = {"handyman-admin-dashboard": "hosteddashboard"}
    e_src, e_rel = expected.split("/", 1)
    e_src = aliases.get(e_src, e_src)
    return returned == f"{e_src}/{e_rel}"
```

### D. Bucket assignment (overlap allowed)

```python
def assign_stage19_buckets(record, diagnostics) -> list[str]:
    buckets = []
    if (diagnostics["wrong_source_in_top_k"] > 0 
        or diagnostics["expected_citation_top_rank"] is None):
        buckets.append("retrieval_wrong_source")
    if diagnostics["expected_citation_top_rank"] is not None:
        # right source returned the file. If rank > 4 maybe still bad
        if diagnostics["expected_citation_top_rank"] > 4:
            buckets.append("retrieval_right_source_wrong_file")
    if diagnostics["expected_fact_in_card"] is False:
        buckets.append("card_missing_keypoint_facts")
    kp_cov = float(record.get("keypoint_coverage", 0))
    if record["tier"] in ("C", "D") and kp_cov < 0.3 and not any(b.startswith("retrieval_") for b in buckets):
        buckets.append("cross_file_reasoning_miss")
    # domain_jargon: heuristic — check if expected keypoints contain Android-specific terms 
    # but answer (answer_excerpt) does not
    android_terms = {"Composable", "Fragment", "ViewModel", "Firebase", "Navigation", 
                     "Activity", "RecyclerView", "Adapter", "navController", "LazyColumn",
                     "@composable", "compose", "androidx"}
    answer = (record.get("answer_excerpt") or "").lower()
    kp_text = " ".join(record.get("expected_answer_keypoints") or []).lower()
    has_android = any(t.lower() in kp_text for t in android_terms)
    answer_misses = has_android and not any(t.lower() in answer for t in android_terms)
    if has_android and answer_misses:
        buckets.append("domain_jargon_miss")
    return buckets
```

### E. Per-Q record extension

Add to each per-question record in the artifact:

```python
record.update({
    "expected_citation_top_rank": diagnostics["expected_citation_top_rank"],
    "top_k_source_distribution": diagnostics["top_k_source_distribution"],
    "wrong_source_in_top_k": diagnostics["wrong_source_in_top_k"],
    "expected_fact_in_card": diagnostics["expected_fact_in_card"],
    "stage19_buckets": stage19_buckets,
})
```

### F. Summary aggregates

In summary line, add:

```python
summary["retrieval_top_rank_distribution"] = Counter(
    "absent" if r.get("expected_citation_top_rank") is None
    else f"top{min(r['expected_citation_top_rank'], 8)}"
    for r in records
)
summary["wrong_source_record_count"] = sum(
    1 for r in records if (r.get("wrong_source_in_top_k") or 0) > 0
)
summary["expected_fact_in_card_rate"] = (
    sum(1 for r in records if r.get("expected_fact_in_card") is True) /
    max(sum(1 for r in records if r.get("expected_fact_in_card") is not None), 1)
)
summary["stage19_bucket_counts"] = Counter(b for r in records for b in (r.get("stage19_buckets") or []))
```

### G. Per-source mean breakout

In addition to per-tier means, emit per-source means in `summary["source_summary"]`:

```python
"source_summary": {
    "hosteddashboard": {"count": ..., "valid_score_count": ..., "mean_score": ...},
    "handymanapp": {"count": ..., "valid_score_count": ..., "mean_score": ...},
}
```

This is the actual deliverable for Stage 19: did cards generalize from dashboard to handymanapp.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py` — add source_name plumbing + diagnostics + Stage 19 buckets + summary aggregates + per-source breakout
2. `apps/backend/tests/scripts/test_run_qa_benchmark.py` — 6+ new tests:
   - `test_multi_source_dataset_requires_source_name_field`
   - `test_search_passes_source_name_to_backend`
   - `test_diagnostics_compute_expected_citation_rank`
   - `test_diagnostics_count_wrong_source_citations`
   - `test_buckets_assign_retrieval_wrong_source`
   - `test_buckets_assign_card_missing_keypoint_facts`
   - `test_summary_emits_per_source_means`
3. `apps/backend/scripts/run_qa_benchmark.py` (separate fn): `_matches_citation` helper with the hosteddashboard ↔ handyman-admin-dashboard alias

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py tests/scripts/test_run_qa_benchmark.py` clean
- All existing harness tests still pass (15 from V2)
- 6+ new tests pass
- Manual smoke from Tomonkyo bash:
  - `python -m pytest tests/scripts/test_run_qa_benchmark.py -v` shows green on all
  - `python -m scripts.run_qa_benchmark --judge-mode rule --limit 2` runs to completion and emits `source_summary`, `retrieval_top_rank_distribution`, `stage19_bucket_counts` in summary

## Out of scope

- Modifying `KnowledgeService.search_repositories` (it already supports `source_name`)
- Cross-source reranking logic (Stage 20)
- Card prompt revision for Android
- Adapting `rejudge_run.py`

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/bench-harness-v3" -c model_reasoning_effort=medium - < docs/ai/tasks/T-BENCH-HARNESS-V3.md
```

Worktree: NEW `D:/项目/ops-worktrees/bench-harness-v3` on `feat/bench-harness-v3` based on `checkpoint/pre-reclassify` HEAD `12c2067`.

## Why P1

Without this, Stage 19 keeps producing contaminated numbers. We've already lost 2 bench runs (env config gap + global FTS5). The third should be clean. If we don't isolate per-source signal we cannot tell whether cards generalize to Kotlin/Android, which is the entire point of Stage 19.
