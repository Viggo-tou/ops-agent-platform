# T-BENCH-RUNNER-TIMEOUT-FLAG — Add per-question timeout CLI flag to QA benchmark runner

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P0 — blocks Phase 1 fair re-baseline of CC mode)
**Priority:** P0 (CC mode synthesis takes ~80s; current hardcoded 120s timeout drops 20/34 questions and biases the baseline)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Make the per-question polling timeout in `apps/backend/scripts/run_qa_benchmark.py` configurable via a CLI flag with a sensibly larger default, so future baseline runs stop being silently capped by the runner instead of by the backend's actual completion behavior.

## Background

Stage 8 baseline run today (2026-04-28):
- 20 / 34 questions hit the runner's 120s deadline and were marked `timed_out`
- All 20 stopped exactly at 120.0s — meaning the backend was still working
- For CC-mode tasks, observed end-to-end durations were `cc_agent ≤ 30s + synthesis ≈ 80s + overhead`, totalling 100-130s
- Result: baseline mean was 17.82 (down from RAG's 27.06), but the drop is timeout-dominated, not quality-dominated

The runner currently has:

```python
# apps/backend/scripts/run_qa_benchmark.py:48
QUESTION_TIMEOUT_SECONDS = 120.0
```

and uses it in `poll_task()`:

```python
def poll_task(self, task_id: str, timeout_seconds: float = QUESTION_TIMEOUT_SECONDS) -> ...:
    ...
    if elapsed > timeout_seconds:
        ...
```

It's also reported in the run summary header:

```python
"question_timeout_s": QUESTION_TIMEOUT_SECONDS,
```

There is no CLI flag to override it, and no env var read.

## Design

### A. Add CLI flag

In the argparse setup, add:

```python
parser.add_argument(
    "--question-timeout",
    type=float,
    default=240.0,
    help=(
        "Per-question backend polling deadline in seconds. Each question that "
        "stays running past this is marked timed_out. Default 240s; raise if "
        "synthesis is slow under heavy load."
    ),
)
```

### B. Thread the flag through

- The runner currently uses `QUESTION_TIMEOUT_SECONDS` as a module constant in two spots: `poll_task()` default, and `run_summary["question_timeout_s"]`.
- Replace both usages with the parsed CLI value (carry it on the runner / benchmark object, not on the module constant).
- Keep the module constant `QUESTION_TIMEOUT_SECONDS = 240.0` for any external callers / backward-compat, but actual control flow reads from the runtime argument.

### C. Surface in the run summary

The summary header at the top of each `qa-run-*.jsonl` already records `question_timeout_s`. Make sure it reflects the actual value used (the CLI argument), not the constant.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py`:
   - Bump `QUESTION_TIMEOUT_SECONDS` constant from `120.0` to `240.0` (new default).
   - Add `--question-timeout FLOAT` CLI flag, default = `QUESTION_TIMEOUT_SECONDS`.
   - Plumb `args.question_timeout` to `poll_task()` and to `run_summary["question_timeout_s"]`.

## Files to create

None.

## Tests

The runner is a script, not a backend module. No unit tests exist in the repo for it today. Acceptance is by manual inspection:

1. `python apps/backend/scripts/run_qa_benchmark.py --help` shows the new `--question-timeout` flag with default `240.0`.
2. `python ... --question-timeout 60 --limit 1` runs a single question with a 60s timeout. The summary header `question_timeout_s` field is `60.0`.
3. `python ... --limit 1` (no flag) uses the new default `240.0`.
4. `python -m compileall apps/backend/scripts/run_qa_benchmark.py` is clean.

## Acceptance criteria

- `python -m compileall apps/backend/scripts/run_qa_benchmark.py` exits 0.
- New CLI flag `--question-timeout` accepts a float and threads through to the polling deadline and to the summary header.
- Default raised from 120s to 240s.
- No other behavior changes (judge logic, scoring, polling cadence all untouched).

## Out of scope (explicitly NOT in this card)

- Tuning `knowledge_synthesis_max_snippet_chars` (handled separately via `.env` override).
- Changing CC agent budget (`cc_agent_overall_timeout_s`, etc.) — those are separate decisions.
- Adding a CLI flag for the synthesis snippet cap.
- Re-running the benchmark — the runner change is the only deliverable here.
- New tests / refactor of the runner script structure.

## Workflow (for the executor)

<!-- Effort: low -->

1. Read `apps/backend/scripts/run_qa_benchmark.py`. Locate `QUESTION_TIMEOUT_SECONDS`, `poll_task`, the argparse setup, and the run-summary builder.
2. Bump constant `QUESTION_TIMEOUT_SECONDS = 240.0`.
3. Add the `--question-timeout` argparse argument with `default=QUESTION_TIMEOUT_SECONDS`.
4. Plumb the value into `poll_task()` calls and `run_summary["question_timeout_s"]`.
5. Run `python apps/backend/scripts/run_qa_benchmark.py --help` to sanity-check the flag.
6. Run `python -m compileall apps/backend/scripts/run_qa_benchmark.py` to confirm clean compile.
7. Do NOT git commit — leave changes dirty for Claude to review and commit on the appropriate worktree.

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-BENCH-RUNNER-TIMEOUT-FLAG.md
```

Worktree to use: re-use the existing `D:/项目/ops-worktrees/cc-agentic` worktree (branch `feat/kb-cc-agentic`). The CC code + benchmark runner are both already on this branch from the earlier merge.
