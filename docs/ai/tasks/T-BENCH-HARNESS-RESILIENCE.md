# T-BENCH-HARNESS-RESILIENCE — Make `run_qa_benchmark.py` truthful when judge or infra fails

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P1 — prerequisite for any future v3-v∞ tier-cap, FTS5, cards, hybrid fast-path benchmarks)
**Priority:** P1 (today's incident: 64 min wall-clock + 3+ hr debug because harness mislabeled judge failure as model failure)
**Created:** 2026-04-29

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Make the QA benchmark harness **incapable of converting an infrastructure failure into a fake model failure**. Today (Stage 12 v2 incident), the judge subprocess failed with `npm EPERM` for all 34 questions; the script's broad `except Exception` caught it, set `task_status="runner_error"`, dropped the answer text, and recorded `score=0.0` for every question. Backend synthesis was fine — the data was already in the DB. The artifact mislabeled this as "v2 policy failed" when in fact "judge couldn't run".

This spec hardens four failure-domain hardening passes that together ensure the artifact tells the truth about which layer failed.

## The four fixes (one ticket; all four ship together)

### Fix 1 — `--judge-mode <name>` is a strict pin

Today: `--judge-mode claude_code` silently falls to `rule` if the named judge fails. Artifact records `judge_modes_used=["rule"]` but `judge_auto_fallback_reason=null`.

Required behaviour:
- If user passes `--judge-mode auto`: existing fallback chain stays. Each fallback step writes a non-null entry into `judge_auto_fallback_reason` (never `null`).
- If user passes `--judge-mode <X>` where X ≠ auto: judge MUST be X. If X fails on Q1's preflight (Fix 4), exit 2 BEFORE consuming any synthesis budget. If X fails mid-run on a single question (Q2..QN), record that question's `judge_status="fail"` + the error string, but do NOT fall to rule and do NOT continue treating the run as pinned. The summary must record `pinned_judge_failure_count` and a flag `pinned_judge_run_intact` that goes False on first per-Q failure.
- The summary's `judge_modes_used` is the union of judges that actually ran, in order of first appearance. With strict pin, this should be exactly `[X]`.

### Fix 2 — Persist backend answer BEFORE judging

Today: lines 941-948 of `run_qa_benchmark.py` do `extract_answer_and_citations` then immediately call `judge.judge(...)`. If judge raises, the broad `except Exception` at 961 catches it, but `answer_excerpt` was set in the try block AFTER the judge call → empty string in artifact.

Required behaviour:
- Move the answer extraction + `answer_excerpt = truncate_utf8(answer, ...)` assignment **above** the judge call.
- `citations_found` (display + canonical) and `expected_citations` get computed BEFORE the judge call.
- Even if judge throws, the per-question record retains the real answer excerpt, real citations, real keypoints. Only `keypoint_hits`, `keypoint_coverage`, and the score-derived fields get the "judge unavailable" treatment.

### Fix 3 — Separate `synthesis_status` / `judge_status` / `score_status` fields

Today: one `task_status` string conflates "backend pipeline state" with "did we get a usable answer". One `score=0.0` swallows three different failure modes (no answer, judge failure, real model miss).

Required behaviour: each per-question record gains three new top-level fields:
- `synthesis_status`: `"pass"` (backend returned non-empty answer), `"empty"` (backend completed but answer is empty), `"timeout"` (backend never finished within `--question-timeout`), `"task_error"` (backend errored)
- `judge_status`: `"pass"` (judge returned hits), `"fail"` (judge raised), `"skipped"` (no answer to judge)
- `score_status`: `"valid"` (synthesis_status=pass AND judge_status=pass), `"invalid"` (anything else)
- `score` field stays for backward compat but artifacts with `score_status="invalid"` should not be averaged into tier means by downstream tools (note this in the summary header).

The summary line gains aggregate counts: `synthesis_status_counts`, `judge_status_counts`, `score_status_counts`.

### Fix 4 — Preflight judge call before Q1

Required behaviour:
- After parsing args + before `for row in rows:`, call the judge once with a fixed dummy `(question="ping", answer="ping", keypoints=["ping"])`.
- If preflight raises:
  - With `--judge-mode <X>` (strict pin): exit 2 with a clear message `"Preflight judge call failed: <error>; aborting before consuming synthesis budget"`.
  - With `--judge-mode auto`: log the failure as a warning, mark `judge_auto_fallback_reason` for the failed mode, and proceed. Same fallback chain as today.
- Preflight status recorded in summary as `preflight_judge_status` and `preflight_judge_error`.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py` — all four fixes
2. `apps/backend/tests/scripts/test_run_qa_benchmark.py` — NEW (or extend existing test if any). 8+ tests covering:
   - `test_strict_pin_fails_fast_on_preflight_failure` (mock judge to raise; assert exit 2 before any synthesis)
   - `test_strict_pin_records_per_q_judge_failure_without_fallback`
   - `test_auto_mode_falls_back_with_recorded_reason`
   - `test_answer_excerpt_populated_when_judge_fails` (the today-incident regression)
   - `test_status_fields_separated_synthesis_pass_judge_fail`
   - `test_status_fields_separated_synthesis_empty_judge_skipped`
   - `test_summary_aggregates_status_counts`
   - `test_preflight_records_status_in_summary`

3. `apps/backend/scripts/rejudge_run.py` — already has the status separation (codex built it correctly today). Keep aligned with run_qa_benchmark's new fields so artifact shape is consistent.

4. `docs/ai/benchmarks/qa-baseline-2026-04-28.md` — DO NOT TOUCH. The historical baseline reference must not change.

## Acceptance criteria

- `python -m compileall apps/backend/scripts apps/backend/tests/scripts` clean
- 8+ new tests pass
- Existing `pytest tests/services/test_knowledge_synthesis.py` still passes (sanity)
- Manual smoke from Tomonkyo bash:
  - Run with `--judge-mode rule --limit 2` (rule judge needs no CLI). Verify artifact has 3 status fields per question + 3 status_counts in summary.
  - Run with `--judge-mode claude_code --limit 1` if claude CLI works. Verify pinned-success path.
  - Stub a broken claude_code (e.g. set `CLAUDE_CODE_GIT_BASH_PATH` to a bogus path) and run with `--judge-mode claude_code --limit 1`. Verify exit 2 + `preflight_judge_status="fail"` recorded in stderr or summary.
- Verify `--judge-mode auto --limit 1` still works as today (chain still chains, but each step writes its reason).

## Out of scope

- Modifying `KeypointJudge` class internals beyond what's needed for status reporting.
- Adding new judge modes.
- Changing the scoring formula `score = kp*60 + cp*40`.
- Changing the artifact filename convention.
- Doing the same hardening for `rejudge_run.py` beyond field-shape alignment.
- Touching `qa-baseline-2026-04-28.md`.

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/Ops_agent_platform" -c model_reasoning_effort=medium - < docs/ai/tasks/T-BENCH-HARNESS-RESILIENCE.md
```

Worktree: NEW worktree `D:/项目/ops-worktrees/bench-harness` on new branch `feat/bench-harness-resilience` based on `checkpoint/pre-reclassify` HEAD `372089e`. Codex creates the worktree if it doesn't exist; if the path lock prevents it, Tomonkyo creates it manually and codex runs in-place.

## Why this is P1 (don't get tempted to skip)

Today's bench incident: 64-min wall-clock + 3+ hours of debug. Without these fixes, the next bench failure (npm EPERM, claude CLI 429, codex CLI down, anthropic API change) will look exactly the same — empty answers, all 0 scores, fake "model regression" headlines. Each new feature stage (FTS5, cards, hybrid fast-path) needs a benchmark to validate; if the benchmark can't tell the truth about which layer failed, the feature stage's measurement is suspect.

The cost of this fix once: ~1-2 hours codex impl + ~30 min review.
The cost of skipping: 3 hours per future incident, indefinitely, with the additional damage of false negative quality conclusions.
