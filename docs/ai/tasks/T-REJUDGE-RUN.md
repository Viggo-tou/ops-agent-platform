# T-REJUDGE-RUN — Offline re-judge of qa-run-20260429T011959Z.jsonl

<!-- Effort: low -->
<!-- Executor: codex (writes script only; runs from Tomonkyo shell) -->

## Goal

Recover real scores for v2 tier-cap from the bench artifact whose judge step
silently failed with npm EPERM. Backend synthesis succeeded for all 34
questions; answers are in backend DB. Rebuild scores by reading task results
from the backend and running the existing `KeypointJudge` from outside codex
sandbox.

## Background (do not re-investigate; this is established)

- Original artifact: `apps/backend/tests/benchmarks/runs/qa-run-20260429T011959Z.jsonl`.
- Every question has `task_status="runner_error"`, `score=0.0`, `answer_excerpt=""`.
- Root cause: `run_qa_benchmark.py:961` `except Exception` swallowed the judge's `RuntimeError` (npm EPERM hitting npm cache from CodexSandboxUsers user).
- Backend synthesis worked. Verified: `GET /api/tasks/<task_id>` returns `latest_result_json.result.answer` with full text + `result.citations`.
- Backend listening at `http://127.0.0.1:8004` right now (PID 48209, Tomonkyo user).
- `KeypointJudge` class in `apps/backend/scripts/run_qa_benchmark.py:243` is reusable: instantiate with `requested_mode="claude_code"`, `samples=3`, then call `.judge(question=..., answer=..., keypoints=...)`.

## Build

Create `apps/backend/scripts/rejudge_run.py`. About 80-120 lines. Argparse:

```
python -m scripts.rejudge_run \
  --in-run apps/backend/tests/benchmarks/runs/qa-run-20260429T011959Z.jsonl \
  --backend-url http://127.0.0.1:8004 \
  --judge-mode claude_code \
  --judge-samples 3 \
  --out-run apps/backend/tests/benchmarks/runs/qa-run-20260429T011959Z-rejudged.jsonl
```

Behaviour:

1. Read input jsonl. Line 1 is the summary; subsequent lines are `type=question` records.
2. **Preflight judge check** (codex's catch from pivot discussion): before processing Q1, call the judge once on a fixed dummy `(question="ping", answer="ping", keypoints=["ping"])`. If it raises, **exit non-zero with a clear message** like `Preflight judge call failed: <error>; aborting before consuming budget`. Do NOT silently fall to rule.
3. For each question record:
   - Read `task_id` from record.
   - HTTP `GET <backend-url>/api/tasks/<task_id>` with header `X-Actor-Name: qa-benchmark`.
   - Extract `request_text` (the question), `latest_result_json.result.answer` (the answer), `latest_result_json.result.citations` (list of dicts with `path` field).
   - Extract `expected_answer_keypoints` from the input record's `keypoint_hits[*].keypoint`.
   - Extract `expected_citations` from the input record's `expected_citations`.
   - Call `KeypointJudge(requested_mode=args.judge_mode, samples=args.judge_samples).judge(question=..., answer=..., keypoints=...)` → `(hits, mode)`.
   - Recompute `keypoint_coverage = sum(hits)/max(len(hits),1)`.
   - Recompute `citation_precision` using existing `compute_citation_precision` from `run_qa_benchmark`.
   - Recompute `score = keypoint_coverage * 60.0 + citation_precision * 40.0`.
4. Write `out-run` jsonl. Line 1 = new summary with **`synthesis_status` / `judge_status` / `score_status` separated** (codex's design fix from pivot discussion):
   - `synthesis_status`: `"pass"` if backend returned an answer, else `"fail"`.
   - `judge_status`: `"pass"` if judge returned hits, `"fail"` if judge raised, `"skipped"` if no answer to judge.
   - `score_status`: `"valid"` if both above pass, else `"invalid"`.
   - Plus all the original summary keys (total_questions, completed_questions, judge_modes_used, tier_summary).
5. Subsequent lines = per-question records with the new score + the new three status fields, AND the recovered `answer_excerpt` (truncated 1500 chars max).
6. **Strict pin**: if `--judge-mode claude_code` and any per-question judge call falls to `rule` (because answer was empty), record `judge_modes_used` honestly with all observed modes and increment a counter. Do NOT crash the whole run on one failure (that would defeat the purpose of recovery). Final summary has `judge_failure_count`.
7. Print to stderr: `[Q01/34] A-01 score=70.00 (kp=0.83, cp=0.67) judge=claude_code` etc., one line per question.
8. Final stderr summary: per-tier means (A/B/C/D), overall mean, score_status counts, runtime.

## Reuse

Import from `scripts.run_qa_benchmark`:
- `KeypointJudge`
- `compute_citation_precision`
- `truncate_utf8`
- `ANSWER_EXCERPT_MAX_BYTES`
- `TERMINAL_STATUSES`

Module imports from the script work because it's already a module (`scripts/__init__.py` exists or runpy handles it; check at start).

## Acceptance

- `python -m compileall scripts/rejudge_run.py` clean (run from `apps/backend/`).
- Dry-run `--in-run <artifact> --judge-mode rule --judge-samples 1` (rule judge needs no CLI) completes successfully end to end and writes a valid out-run.
- Code review: no `except Exception: pass`, no silent fallback. Errors during one question's judge are caught per-question and recorded with explicit `judge_status="fail"` + error string, but do not abort the rest.

## Out of scope

- Modifying `run_qa_benchmark.py`. The strict-pin fix there is a separate ticket (`T-BENCH-HARNESS-RESILIENCE`).
- Re-running synthesis. We only re-judge.
- Writing tests. This is a one-off recovery script; manual smoke (the dry-run above) is the bar.

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/evidence-tier-cap" -c model_reasoning_effort=low - < docs/ai/tasks/T-REJUDGE-RUN.md
```

Worktree: `D:/项目/ops-worktrees/evidence-tier-cap` (where v2 lives, where the bench artifact already is, where backend is connected).

Codex writes the script. **Tomonkyo shell** runs it (codex sandbox can't invoke `claude` CLI without hitting the same npm EPERM that started this whole mess).
