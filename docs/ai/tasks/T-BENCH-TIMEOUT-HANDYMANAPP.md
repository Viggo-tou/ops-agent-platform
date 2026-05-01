# T-BENCH-TIMEOUT-HANDYMANAPP — Raise default question_timeout for cross-stack benches

<!-- Effort: trivial -->
<!-- Executor: codex (or direct per user judgment) -->

**Status:** todo (P2 — Stage 19 follow-up, blocks T-STAGE19-REBENCH-N40)
**Priority:** P2 (independent ergonomic fix, surfaced by Stage 20 rejudge)
**Created:** 2026-05-01
**Branch:** `fix/bench-question-timeout` based on `checkpoint/pre-reclassify` HEAD `db5ee82`
**Linked verdict:** `docs/ai/specs/stage20-judge-verdict.md` (caveat 5)

## Background

The Stage 20 cross-family rejudge of the handymanapp 26Q artifact rescued 4 records (A-15, B-16, C-12, D-08) where the backend actually completed the task but the bench's polling loop timed out at the 240s default. Specifically:

- A-15 (handymanapp): rule timed out @ 240s → MiniMax rejudge fetched the completed task and scored 50.
- B-16: rule timed out → rejudge scored 70.
- C-12: rule timed out → rejudge scored 10.
- D-08: rule timed out → rejudge scored 12.

These were ~15% of the 26 attempted Qs. handymanapp tasks are systematically slower than dashboard tasks (more files in the corpus, longer Kotlin reads, deeper synthesis). The current 240s default reflects dashboard's distribution and is too tight for cross-stack work.

## Goal

Raise the default `--question-timeout` in `apps/backend/scripts/run_qa_benchmark.py` from `240.0` to `480.0` seconds. Same change applies to `rejudge_run.py` if it has its own timeout default (it doesn't currently — uses httpx default — confirm and leave alone).

This is a defensive change. Faster Qs still complete fast; only the long-tail (stuck or genuinely slow) Qs consume the extra budget. Net cost: pathological-stuck-task wallclock goes up by 4 minutes worst case, in exchange for not losing real signal.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py`
   - Find the argparse `--question-timeout` default (constant `QUESTION_TIMEOUT_SECONDS` likely; trace to where 240.0 is set)
   - Change default from `240.0` to `480.0`
   - Update any docstring / help-text that mentions the old value

## Tests

- All existing tests should pass unchanged (tests pass their own `--question-timeout` argument; default change does not affect them).
- No new tests required for a default-value change.

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py` clean
- `python -m pytest tests/scripts/test_run_qa_benchmark.py -v` shows 27 passed (pre-existing baseline after T-JUDGE-HYBRID-V1)
- `python -m scripts.run_qa_benchmark --help` shows the new default

## Out of scope

- Per-source timeout overrides (single global default is fine for now)
- Distinguishing "task completed after polling deadline" from "task genuinely timed out / stuck" in summary aggregates — this is `T-BENCH-TIMEOUT-V2` if Stage 20 rebench reveals the residual confusion still matters.

## Workflow

This is a 1-line value change. Trivially patchable directly without a codex round-trip; user discretion. If dispatching:

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/bench-timeout-fix" \
  -c model_reasoning_effort=low \
  - < docs/ai/tasks/T-BENCH-TIMEOUT-HANDYMANAPP.md
```

Note `model_reasoning_effort=low` because this is a literal value bump — the only justified low-effort dispatch since the 2026-05-01 default-xhigh rule landed.
