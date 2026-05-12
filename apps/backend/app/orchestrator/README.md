# Orchestrator package

The orchestrator drives the develop / QA / writeback / plan pipelines.
Today the bulk of logic lives in `service.py` (PrimaryOrchestrator,
~4500 lines). The `stages/` and `gates/` packages are the migration
target for refactoring that monolith into composable units.

## Phase 1: contracts only (current)

- `gates/base.py` — `Gate`, `GateContext`, `GateReport`, `GateVerdict`,
  `Finding`. Every review-stage check should eventually subclass `Gate`.
- `stages/base.py` — `Stage`, `StageContext`, `StageResult`,
  `StageOutcome`. Every pipeline phase should eventually subclass
  `Stage`.
- `gates/artifact_existence_gate.py` — exemplar Gate wrapping the
  existing function-style implementation.
- `gates/comment_only_gate.py` — exemplar Gate for the escalation rule.

`PrimaryOrchestrator` in `service.py` is **not** modified by Phase 1.
The new classes are unused production code; their purpose is to lock
in the contract before bulk migration.

## Phase 2: migrate gates AS A BATCH, not one at a time

> **Lessons from the aborted first migration (commit 4d0b597, reverted
> in 2dd1dd7).**
>
> Migrating individual gates in isolation does **not** reduce the
> orchestrator. Replacing 70 lines of inline `check_artifact_existence(...)`
> with 70 lines of `ArtifactExistenceGate().run(ctx)` is a horizontal
> move — the per-gate `try / except / record_event(verdict-dependent
> EventType) / fail_pipeline` boilerplate stays at the call site and
> service.py's line count is essentially unchanged. The abstraction
> is just decoration if it doesn't *replace* boilerplate, and the
> uniform interface only starts to pay off when there's a `GateRunner`
> looping over a registered set of gates.

### Migration prerequisites (do these together or not at all)

1. Subclass `Gate` for **all** review-stage checks listed below.
2. Build a `GateRunner` that iterates a registry of gates and owns the
   per-gate boilerplate: building `GateContext` once, calling
   `gate.run(ctx)`, recording the success/fail event with the right
   `EventType`, mutating `pipeline_state`, and calling
   `_fail_develop_pipeline` on `BLOCK`.
3. Replace the entire review block in `_execute_develop_pipeline` with
   one call to `runner.run_all(ctx)`. This is where service.py
   actually shrinks (~1500 lines → ~30 lines for the review block).
4. Each gate gets a unit test under `tests/orchestrator/gates/`.

Doing 1+2+3 together is the only way the abstraction earns its
maintenance cost. Doing 1 alone (the partial first attempt) leaves the
codebase with two patterns coexisting and no measurable benefit.

### Gate inventory (all currently inline in service.py)

Order suggested by isolation / risk, not by mandatory sequence:

1. ArtifactExistenceGate (today's exemplar — already implemented and
   has a passing unit test, just not wired in)
2. CommentOnlyGate (today's exemplar — same status as above)
3. CompileGate
4. RuntimeValidationGate
5. SymbolReferenceGate
6. DiffShapeGate
7. DiffReviewerGate
8. GoalDecompositionGate
9. SpecConformanceGate (most callers depend on it; touch last)

## Phase 3: stage migration (further future)

Same pattern, but for entire pipeline phases:
1. PlanningStage
2. ReviewStage (pre-execution review)
3. EvidenceBundleStage
4. CodegenStage
5. SandboxApplyStage
6. GateBatteryStage (uses Phase 2 Gate registry)
7. ApprovalParkStage
8. WritebackStage

A GateRunner sits between GateBatteryStage and the registered gates.

## Why this lego matters

The pre-Phase-1 status quo: each gate is hard-wired into `service.py`
with bespoke try/except blocks, custom event shapes, and manual
`pipeline_state` mutation. Adding a new gate means understanding the
3000-line `_execute_develop_pipeline` method and finding the right
insertion point. After migration, adding a new gate is a two-step
operation (subclass `Gate`, register), with no orchestrator edit.

The same logic applies to `Stage`: phase reordering, conditional
phase skipping (e.g. for `process_question`), and crash recovery all
become much simpler when phases are typed objects rather than blocks
inside a giant procedural method.
