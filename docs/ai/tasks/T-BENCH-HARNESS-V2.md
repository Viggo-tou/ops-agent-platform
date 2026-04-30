# T-BENCH-HARNESS-V2 — Pause-on-infra-blip + failure-bucket reporting

<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P1 — Stage 19 prerequisite)
**Priority:** P1 (Stage 19; needed before 60Q bench)
**Created:** 2026-04-30
**Branch:** `feat/bench-harness-v2` based on `checkpoint/pre-reclassify` HEAD `24fa7eb`

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Two policy upgrades to `apps/backend/scripts/run_qa_benchmark.py` (T-BENCH-HARNESS-RESILIENCE landed Stage 14):

1. **Pause-on-3-consecutive-infra-errors**: when 3 consecutive questions hit `synthesis_status` in `{task_error, timeout}` (i.e. `score_status="invalid"` due to backend infra not model quality), pause the run for a configurable backoff (default 30s), then probe backend health, then resume. If a 2nd burst happens within the run, escalate to longer backoff (60s). After a 3rd, abort the run with `pinned_judge_run_intact=False` style flag and exit 2. This protects the bench from a 5-min CC wobble poisoning the entire dataset signal (Stage 19 control bench had this exact pattern: 6 consecutive infra errors in C-06 → D-02 window).

2. **Failure-bucket reporting in summary**: in addition to existing `score_status_counts` / `synthesis_status_counts`, classify each `score_status="invalid"` record into one of:
   - `infra_timeout` — duration ≈ overall timeout values (e.g. ~30s exact)
   - `infra_task_error` — task crashed before completion
   - `cc_failure` — task completed but error mentions claude_code/cc_agent
   - `synthesis_empty` — synthesis returned empty
   - `synthesis_error` — synthesis raised
   - `judge_failure` — judge could not score a successful synthesis
   - `other` — unclassified

   Emit `failure_bucket_counts` in the summary. This lets Stage 19 cards-on-handymanapp bench distinguish "RN/Kotlin code shape problem" from "CC agent infra noise".

## Design

### A. Pause-on-burst

```python
# apps/backend/scripts/run_qa_benchmark.py — add after build_summary helper
INFRA_INVALID_SYNTH_STATUSES = {"task_error", "timeout"}
INFRA_BURST_THRESHOLD = 3
INFRA_BURST_BACKOFF_S = [30, 60]  # first burst: 30s, second burst: 60s. Third burst: abort.
```

In the per-question loop, after computing `score_status`:

```python
if score_status == "invalid" and synthesis_status in INFRA_INVALID_SYNTH_STATUSES:
    consecutive_infra_invalid += 1
    if consecutive_infra_invalid >= INFRA_BURST_THRESHOLD:
        burst_count += 1
        if burst_count > len(INFRA_BURST_BACKOFF_S):
            print(f"BENCH ABORT: {burst_count} infra bursts; aborting bench to avoid contaminated signal", file=sys.stderr)
            break  # abort the for-loop; finalize summary with abort_reason set
        backoff = INFRA_BURST_BACKOFF_S[burst_count - 1]
        print(f"BENCH PAUSE: {consecutive_infra_invalid} consecutive infra-invalid records; sleeping {backoff}s, then probing backend health", file=sys.stderr)
        time.sleep(backoff)
        # probe health before resuming
        try:
            client.ensure_backend_reachable()
        except Exception as exc:
            print(f"BENCH ABORT: backend unreachable after pause: {exc}", file=sys.stderr)
            break
        consecutive_infra_invalid = 0  # reset counter for the next window
else:
    consecutive_infra_invalid = 0  # any non-infra-invalid resets the streak
```

Add to summary:
```python
"infra_burst_count": burst_count,
"abort_reason": "infra_burst_exceeded" if burst_count > len(INFRA_BURST_BACKOFF_S) else None,
```

### B. Failure bucket classification

Helper:
```python
def classify_failure_bucket(record: dict) -> str | None:
    """Return the failure bucket name for an invalid record, or None for valid records."""
    if record.get("score_status") == "valid":
        return None
    syn = record.get("synthesis_status")
    judge = record.get("judge_status")
    duration = float(record.get("duration_s") or 0)
    error = (record.get("error") or "").lower()

    if syn == "timeout":
        return "infra_timeout"
    if syn == "task_error":
        # 30-second-shaped task_error is a CC overall_timeout cap, not a model crash
        if 25 <= duration <= 35:
            return "cc_failure"
        return "infra_task_error"
    if syn == "empty":
        return "synthesis_empty"
    if syn == "pass" and judge == "fail":
        return "judge_failure"
    if "cc_agent" in error or "claude_code" in error or "cc decision" in error:
        return "cc_failure"
    return "other"
```

Emit in summary:
```python
from collections import Counter
failure_buckets = Counter()
for rec in records:
    b = classify_failure_bucket(rec)
    if b: failure_buckets[b] += 1
"failure_bucket_counts": dict(sorted(failure_buckets.items())),
```

### C. Settings (CLI args)

```python
parser.add_argument("--infra-burst-threshold", type=int, default=3,
    help="Pause when N consecutive infra-invalid records appear")
parser.add_argument("--infra-burst-backoff", type=str, default="30,60",
    help="Comma-separated backoff seconds; bursts beyond list count abort the bench")
parser.add_argument("--no-pause-on-burst", action="store_true",
    help="Disable burst pause; emit warnings only (legacy behavior)")
```

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py` — burst counter + pause logic + failure bucket helper + summary fields + CLI args
2. `apps/backend/tests/scripts/test_run_qa_benchmark.py` — add 5+ tests:
   - `test_burst_pause_after_3_consecutive_infra_invalid`
   - `test_burst_pause_resets_counter_on_valid_record`
   - `test_burst_abort_after_max_bursts`
   - `test_failure_bucket_classifies_30s_task_error_as_cc_failure`
   - `test_failure_bucket_classifies_other_task_error_as_infra`
   - `test_summary_emits_failure_bucket_counts_and_burst_count`
   - `test_no_pause_on_burst_flag_disables_pause`

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py tests/scripts/test_run_qa_benchmark.py` clean
- All existing harness tests still pass (8 from Stage 14 should be unchanged)
- 7+ new tests pass
- Manual smoke from Tomonkyo bash:
  - `python -m pytest tests/scripts/test_run_qa_benchmark.py -v` shows green on all
  - `python -m scripts.run_qa_benchmark --judge-mode rule --limit 2` runs to completion and emits `failure_bucket_counts` + `infra_burst_count: 0` in summary

## Out of scope

- Modifying rejudge_run.py
- Changing judge selection logic
- Per-question infra retry (we just skip past bursts; we do not re-submit failed questions)
- Distinguishing RN-specific from Kotlin-specific synthesis failures (that's Stage 19 analysis, not harness)

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/bench-harness-v2" -c model_reasoning_effort=low - < docs/ai/tasks/T-BENCH-HARNESS-V2.md
```

Worktree: NEW `D:/项目/ops-worktrees/bench-harness-v2` on `feat/bench-harness-v2` based on `checkpoint/pre-reclassify` HEAD `24fa7eb`.

## Why P1 for Stage 19

Stage 19's 60Q handymanapp bench WILL have CC agent transients (Stage 18 hybrid bench: 8/34 invalid; Stage 19 control: 6/34 invalid; CC agent has wobble of ~10-25%). Without burst pause, a 5-min wobble in the middle of 60Q can corrupt 10+ records and make the result uninterpretable. This harness fix is the prerequisite that makes Stage 19's number trustworthy.
