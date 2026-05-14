# Round19 P69-19 Plan

Date: 2026-05-14

## Goal

Validate that the structured planner context plus structured Kotlin codegen path can reach `awaiting_approval` with a trustworthy final-tree approval diff.

## Preconditions

- Backend running on or after `84d734b T-PLANNER-CONTEXT structured prompt packet`.
- Final-tree diff refresh fix present: `1d68a81 fix(compile-repair): refresh approval diff from final tree`.
- Structured Kotlin codegen path present: `c3d823a T-STRUCTURED-CODEGEN Kotlin JSON edit path`.
- Rollback tag available: `checkpoint/round11e-p69-19-approval`.

## What Round19 Should Prove

- Planner prompt uses a stable `<planner_context>` packet.
- Provider prompt does not duplicate Jira/repository context outside the packet.
- Codegen still targets the four existing P69-19 files instead of inventing helper files.
- Compile repair may run, but approval diff must be regenerated from the final sandbox tree.
- Approval artifact must contain the compile-passing final symbols:
  - `MapView`
  - `MapEventsReceiver`
  - `singleTapConfirmedHelper`
  - `Geocoder(..., Locale.getDefault())`
  - `getFromLocation`
  - `updateChildren` or `setValue`

## Metrics To Record

- Planner wall time and provider latency.
- `planner.context_packet` event payload: chars, candidate file count, duplicate-context suppression.
- Per-batch codegen latency and provider path (`structural_kotlin` vs fallback).
- Compile duration and repair rounds.
- Final created-to-approval runtime.
- Diff quality notes: duplicate map/geocoder logic, thread correctness, Firebase write atomicity, schema consistency.

## Stop Conditions

- Do not approve Jira transition automatically.
- If compile passes but approval diff does not contain final repair edits, stop and treat it as a P0 artifact correctness regression.
- If planner invents new helper files not named by Jira/user text, stop and inspect planner prompt before another live run.

