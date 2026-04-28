# T-QA-ACCURACY-BENCHMARK — QA accuracy benchmark + baseline gate for all future optimization work

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P0 — Phase 1 prerequisite for everything in Phase 3-8)
**Priority:** P0 (BLOCKS all "optimization" work; without this every "+10 points" claim is unverifiable)
**Created:** 2026-04-23 (re-spec'd 2026-04-28)

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Build a fixed 34-question QA benchmark + scoring script + baseline report that:

1. Produces a reproducible 0-100 accuracy score (multi-sample N=3 to neutralize stochastic LLM variance) covering 4 difficulty tiers (A/B/C/D).
2. Records per-question keypoint coverage and citation precision so future runs can localize regressions.
3. Stores baseline results in version control so any later PR can re-run and compare.
4. Establishes a project rule: any task in `docs/release-roadmap.md` Phase 3-8 that claims "improves accuracy" must include before/after benchmark numbers in its acceptance criteria — no numbers, no merge.

This is Phase 1 of the v1.0 roadmap. Phase 3.0 (AST chunking), 3.1 (query expansion), 3.3 (hybrid evidence), 3.4 (rerank), 5.4 (agentic retrieval) are all gated on this — they each promise specific +N point gains, those gains can only be measured against this baseline.

## Background

### Why now (re-spec'd 2026-04-28)

Original spec (2026-04-23) marked this P2 "basic infrastructure". User has since elevated to P0:

> 目标：以后所有"优化"都要有数字 (Goal: every "optimization" from now on must have numbers backing it)

The 2026-04-26 roadmap update also surfaced a concrete pain point: a single-sample baseline of 30.04 dropped to 27.06 under multi-sample N=3 — the "honesty tax". Future optimization claims must reference the multi-sample number, not the inflated single-sample one. This spec bakes that in.

### Two direct uses

1. **Short term (regression alarm)**: T-SKIP-PIPELINE-FOR-QA merge claimed "shortcut doesn't drop accuracy"; this is unverified. After this benchmark exists, any future change that touches knowledge / synthesis / model / chunking must re-run and not regress on A+B+C tiers.
2. **Long term (D-tier gap quantification)**: D tier (multi-hop / cross-file analysis) is expected to score low. Phases 3.x and 5.4 promise specific D-tier gains. Without baseline, "D tier improved" is unfalsifiable.

### Dependencies

- None. This is foundational and unblocks everything downstream.

## Design

### A. Question set (34 questions, 4 tiers)

Stored at `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` (one JSON object per line):

```json
{
  "id": "A-001",
  "tier": "A",
  "question": "Where is the Login component defined?",
  "language": "en",
  "expected_answer_keypoints": ["src/pages/Login.js", "Login functional component", "exports default Login"],
  "expected_citations": ["src/pages/Login.js"],
  "min_keypoint_coverage": 0.66,
  "min_citation_precision": 0.5,
  "knowledge_source": "data/knowledge/handyman/"
}
```

| Tier | Count | Description | Example | Expected baseline |
|---|---|---|---|---|
| A. Simple location | 10 | "where is X defined" / "what file holds X" | "Where is Login.js?" | 80-100 (BM25 ought to nail) |
| B. Single-file explanation | 10 | "what does X do" / "explain X.tsx" | "Explain AuthProvider.tsx" | 60-80 (synthesis able) |
| C. Cross-file reference | 8 | "which files use X" / "callers of X" | "Which components use AuthProvider?" | 30-50 (BM25 partial, synthesis weak) |
| D. Multi-hop analysis | 6 | "trace X to Y to Z" / "coupling between A and B" | "Coupling between login module and order module" | < 30 (expected low — quantify gap) |

`expected_answer_keypoints` are 3-5 short phrases the answer must contain (LLM-judged for fuzzy match). `expected_citations` are file paths the answer must cite.

### B. Scoring (multi-sample N=3, weighted composite)

`apps/backend/scripts/run_qa_benchmark.py`:

```
For each question:
  For sample in range(3):
    Run the QA pipeline → answer + citations
    keypoint_coverage = LLM_judge(answer, expected_keypoints)  # 0..1
    citation_precision = |answer.cites ∩ expected_citations| / |answer.cites|  # 0..1
    sample_score = 0.7 * keypoint_coverage + 0.3 * citation_precision
  question_score = mean(sample_scores) * 100
  # Multi-sample N=3 averages out LLM stochasticity. The honest score.

Tier scores = mean of question_scores within tier
Total score = weighted mean: A * 0.20 + B * 0.25 + C * 0.30 + D * 0.25
```

Output:
- `data/benchmarks/qa-baseline-YYYY-MM-DD-HHMM.json` — raw per-question samples + scores
- `docs/ai/benchmarks/qa-baseline-YYYY-MM-DD-HHMM.md` — human-readable summary
- Stamp: judge_model_version + commit_sha + scenario_config + git_dirty flag

### C. Judge model

LLM-as-judge for keypoint coverage. Reuse the existing provider chain — in current operation that means **claude_code CLI first, codex CLI second** (anthropic / minimax exist as API fallbacks but should NOT be the default judge because they burn API budget; if either fires, log a WARN). Default judge prompt:

```
Question: {question}
Expected keypoints (the answer should mention these): {expected_keypoints}
System answer: {answer}

For each keypoint, decide if the system answer covers it (true/false).
Output JSON: {"keypoints": [{"point": "...", "covered": true/false, "reason": "..."}]}
Keypoint coverage = sum(covered) / len(keypoints)
```

Judge model versioned in the report. Re-running with same judge model + same answer → same score within ±2 points (judge stochasticity).

### D. CLI usage

```
python apps/backend/scripts/run_qa_benchmark.py \
  --dataset apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl \
  --tier all \
  --samples 3 \
  --judge-provider claude_code \  # CLI; falls back to codex CLI if unavailable
  --output data/benchmarks/qa-baseline-2026-04-28-1500.json \
  --report docs/ai/benchmarks/qa-baseline-2026-04-28-1500.md
```

Subcommands:
- `--tier A`/`B`/`C`/`D` to run a single tier
- `--samples N` to override (default 3)
- `--question id` to run one question (debugging)
- `--diff <prev_baseline.json>` to print delta vs a prior baseline

### E. Baseline gate (enforcement of "all optimizations need numbers")

Add a markdown section to `docs/release-roadmap.md` Phase 3-8:

> **Quality gate enforcement**: before merging any task whose acceptance criteria mentions "+N points", run `run_qa_benchmark.py` on the task branch and on `main`, attach both reports to the PR. If the diff doesn't meet the claimed gain, do NOT merge — either iterate, drop the claim, or downgrade the gate language.

This is documentation, not a CI check (CI requires LLM API budget which user is avoiding). The discipline is **on the human reviewer**: PR description must link to before/after benchmark reports.

## Files to create

1. `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` — 34 questions with tier / language / expected_keypoints / expected_citations
2. `apps/backend/scripts/run_qa_benchmark.py` — runner CLI
3. `apps/backend/scripts/qa_benchmark_judge.py` — LLM-as-judge module (importable)
4. `apps/backend/tests/benchmarks/test_run_qa_benchmark.py` — unit tests for runner + judge (mocked LLM)
5. `docs/ai/benchmarks/.gitkeep` — directory placeholder; first baseline goes in next.
6. `docs/ai/benchmarks/qa-baseline-2026-04-28-1500.md` — first baseline (run after the runner is built)

## Files to edit

1. `docs/release-roadmap.md` — add a top-level "Quality gate enforcement" section near the start of Phase 3 referencing this benchmark; remove the original P2 priority annotation (now P0).
2. `docs/ai/tasks/T-QA-ACCURACY-BENCHMARK.md` — already this file; updates land here.

## Tests

### Unit tests (test_run_qa_benchmark.py)

1. `test_dataset_loads_34_questions` — `qa_benchmark_dataset.jsonl` has exactly 34 lines, each parses, each has all required fields, tier counts match (A:10, B:10, C:8, D:6).
2. `test_judge_returns_keypoint_coverage_for_simple_match` — given a mocked judge LLM that says all keypoints covered, judge returns 1.0.
3. `test_judge_returns_partial_coverage` — mocked judge says 2/3 covered → returns ~0.66.
4. `test_judge_handles_malformed_llm_response` — mocked judge returns non-JSON → judge returns 0.0 + logs warning, does NOT crash.
5. `test_citation_precision_exact_match` — answer cites `[a, b]`, expected `[a, b]` → 1.0.
6. `test_citation_precision_partial` — answer cites `[a, c]`, expected `[a, b]` → 0.5.
7. `test_citation_precision_empty_answer` — answer cites `[]` → 0.0.
8. `test_runner_aggregates_multi_sample_scores` — given 3 mocked QA pipeline runs producing `[80, 90, 70]`, question_score = 80.
9. `test_runner_writes_report_with_judge_model_version_stamp` — output JSON contains `judge_model`, `commit_sha`, `samples_per_question`, `git_dirty`, `timestamp`.
10. `test_runner_diff_command_prints_per_tier_delta` — given two baseline JSONs, `--diff` prints A/B/C/D + total delta correctly.
11. `test_runner_handles_zero_samples_gracefully` — `--samples 0` → error message, exit 2.
12. `test_runner_runs_single_tier_only` — `--tier C` only runs the 8 C-tier questions, total score weighting still A:20% B:25% C:30% D:25% but absent tiers contribute 0.

### Integration smoke (separate file, not in CI by default)

13. `test_smoke_real_pipeline_a_tier_one_question` — runs the QA pipeline against ONE A-tier question with REAL LLM. Asserts score > 50 (sanity, not strict). Skipped unless `RUN_BENCHMARK_SMOKE=1` env var set.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 12 unit tests pass.
- Full suite still green.
- Dataset file has exactly 34 questions, distributed A:10 B:10 C:8 D:6.
- Runner CLI produces output JSON + markdown report on a single-question dry run with mocked LLM.
- **Baseline report committed**: `docs/ai/benchmarks/qa-baseline-2026-04-28-<HHMM>.md` exists in the repo with REAL run results (judge_model recorded, commit_sha recorded, multi-sample N=3, all 34 questions). This is the project's "Day 0" reference for all future optimization work.
- `docs/release-roadmap.md` Phase 3 has a "Quality gate enforcement" callout that links back to this benchmark.

## Out of scope (explicitly NOT in this card)

- **Improving any accuracy number.** This is pure measurement. No retrieval / synthesis / chunking / prompt change is allowed inside this ticket — those go into separate tickets that REFERENCE this baseline.
- **Cross-language (zh/en mixed) test set.** Pick one language for v1; mixed comes later.
- **CI integration.** Running real LLM in CI burns budget user is avoiding. Discipline is on the human reviewer for now (see "Baseline gate" above).
- **Judge meta-evaluation.** We accept whatever the judge model says as ground truth as long as it's stable run-to-run.
- **D-tier root-cause classification.** That's a follow-up: after baseline is set and D-tier scores are confirmed low, a separate ticket reads the per-question D-tier failures and classifies them (retrieval-miss / synthesis-miss / planner-would-have-helped / other) so the next ticket can target the right layer.

## Risks

- **Question set too small (34) → high variance.** Mitigation: multi-sample N=3, fixed scenario config, deterministic seeds where possible.
- **Judge model drift across runs.** Mitigation: judge model ID + version stamped in every baseline report; when judge changes, re-run prior baselines on the new judge in the SAME commit so deltas are comparable.
- **English-only questions miss zh-CN regressions.** Acceptable for v1; track in `qa-baseline-*.md` notes for future bilingual extension.
- **First baseline might score very low across all tiers** (system not as good as we think). That's a feature, not a bug — the whole point is to have honest numbers.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read `apps/backend/app/services/knowledge.py` and `apps/backend/app/orchestrator/service.py` to understand the QA path used by `process_question` scenario.
2. Read `apps/backend/app/services/codegen.py::_resolve_provider_chain` to understand how to reuse the LLM provider chain for the judge model.
3. Curate 34 questions against the existing `data/knowledge/` content. For each:
   - State the question
   - Locate the actual relevant files in the repo so `expected_citations` is real (not aspirational)
   - Pull 3-5 keypoints the answer should mention (use the actual code as ground truth)
4. Implement `qa_benchmark_judge.py` with the judge prompt + JSON parsing + retry handling.
5. Implement `run_qa_benchmark.py` CLI with the subcommands listed in section D.
6. Write all 12 unit tests with mocked LLM responses (use `unittest.mock`). Do NOT make real LLM calls inside unit tests.
7. Run `python -m compileall app` and unit suite.
8. Run the runner against the real pipeline (this WILL burn LLM budget — but it's a one-time baseline + every future "+N points" claim depends on it). Generate `qa-baseline-2026-04-28-<HHMM>.md`.
9. Commit dataset + scripts + baseline together. PR description includes the baseline scores so reviewer can sanity-check.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-QA-ACCURACY-BENCHMARK.md
```

## Follow-up tickets gated on this baseline

After this lands, every following ticket's acceptance MUST cite a benchmark delta:

- `T-KB-AST-CHUNKING` (Phase 3.0): "A unchanged, B +5, C +3, D +5" → must show those exact gains in PR.
- `T-KB-FTS5-INDEX` (Phase 3.3-1): "A unchanged" — must prove no regression.
- `T-KB-FILE-CARDS` (Phase 3.3-2): "C +5, D +5".
- `T-KB-CC-EVIDENCE` (Phase 3.3-3): "D +3".
- Phase 3.4 Rerank: "D +5 on top of 3.3".
- Phase 5.4 KnowledgeAgent: "D +8 over 3.x baseline".

If a follow-up ticket's PR doesn't include before/after benchmark numbers, the reviewer rejects it. Period.
