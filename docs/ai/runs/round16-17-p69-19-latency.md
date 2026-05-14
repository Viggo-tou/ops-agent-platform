# Round16-17 P69-19 Latency And Repair Notes

Date: 2026-05-14

## Summary

Round17 did not produce an approval result, but it moved the failure boundary:

- Compile repair round 1 improved from Round16's 208s partial repair to Round17's 16.54s deterministic repair across 3 files.
- The new bottleneck is not the deterministic repair code. It is repeated full planner/codegen execution:
  - Round17 planner LLM: 342.6s.
  - Round17 codegen batch long tail: batch4 took about 662s.
- Abandon was not actually cancelling the running pipeline. The task was marked failed at 06:12:26, but the worker continued writing codegen/compile/repair events until the backend was restarted.

## Round16

Task: `035a4b80-747f-4c9a-af89-0e0cdea9b80d`

Outcome: failed/abandoned after compile repair timeout.

Key timings:

- Planner: 05:30:59 to 05:36:49, about 5m50s.
- Codegen parallel batches: 05:36:58 to 05:46:19, about 9m21s.
- Compile round 1: failed at 05:47:30.
- Compile repair round 1:
  - CustomerKYC deterministic repair succeeded in about 3s.
  - HandymanKYC deterministic repair missed the Firebase listener chain shape.
  - C10 structural repair timed out after 120s.
  - Round duration: 208.24s.

Fix produced from this evidence:

- `2fcbe99 T-LEARN-V2 close firebase listener chains`
  - Adds deterministic repair for Firebase Task chains where `.addOnSuccessListener { ... }` is missing its closing brace before `.addOnFailureListener`.

## Round17

Task: `f083beae-9f07-45c2-bd6c-8878a6d260b4`

Outcome: manually abandoned because batch4 exceeded the operator wait window. The backend worker continued afterward, exposing the abandon/cancel bug.

Key timings:

- Planner LLM: 342,557ms.
- Codegen dispatch: 06:02:38.
- Batch 1 done: 06:04:42, about 124s.
- Batch 2 done: 06:08:06, about 328s.
- Batch 3 done: 06:09:15, about 397s.
- Batch 4 done after abandon: 06:13:40, about 662s.
- Compile round 1 failed at 06:14:48.
- Compile repair round 1:
  - CustomerKYC deterministic repair succeeded.
  - HandymanKYC deterministic repair succeeded.
  - HandymanSignup deterministic repair succeeded.
  - Round duration: 16.54s.
- Compile round 2 failed only on:
  - `HandymanKYCAddressForm.kt`: unresolved `mapView` inside `MapView(...).apply {}`.

Fixes produced from this evidence:

- `5762ef7 fix(task-abandon): request cooperative cancel`
  - `/abandon` now sets the cooperative cancellation flag after recording the abandon events.
- `175f4f3 T-LEARN-V2 qualify mapview apply receiver`
  - Adds deterministic repair for `mapView.*` calls inside `MapView(...).apply {}` by qualifying the receiver as `this@apply`.

## Verification

Commands run:

- `python -m pytest tests/services/test_structural_edit.py tests/api/test_abandon_task.py -q`
- `python -m compileall app`

Result:

- 21 targeted tests passed.
- Backend compileall passed.
- Backend restarted cleanly after commits; `/health` reports `pipeline_workers.active=0`.

## Interpretation

This is not mainly a "more gates made it slower" problem. The slow path is:

1. Full planner reruns on the same Jira issue.
2. Full codegen reruns on the same 4 files.
3. Direct parallel codegen has weak cancellation/observability.

The deterministic repairs are doing what they should: they replaced repeated 120s LLM repair calls with seconds-scale scoped fixes. The process-level problem is that every validation run still pays the full planner + codegen cost.

## Next

Do not keep launching full new rounds blindly.

Next development should focus on:

- Add/activate a resume-from-checkpoint path for P69-19 so repeated verification can resume after planner/review instead of paying the 5-6 minute planner cost each time.
- Add per-batch codegen latency payloads/heartbeats for direct parallel codegen so batch long tails are visible without manual DB reconstruction.
- Keep adding deterministic repairs only for recurring structural edit classes, not task-specific strings.
- Re-run P69-19 only after resume/latency controls are in place, or run from an existing checkpoint rather than a fresh task.
