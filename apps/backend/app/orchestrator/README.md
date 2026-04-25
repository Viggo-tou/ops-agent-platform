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

## Phase 2: migrate one gate at a time (future)

Migration steps for each gate:
1. Create `gates/<gate>_gate.py` subclassing `Gate`.
2. Update `service.py`'s review block to call `gate.run(ctx)` instead
   of the inline function.
3. Verify event payloads have the same shape (use
   `GateReport.to_payload()` to match the previous structure).
4. Add a unit test under `tests/orchestrator/gates/`.

Migration order (least risky → most risky):
1. ArtifactExistenceGate (already isolated, no dependencies)
2. CommentOnlyGate (depends only on goal_decomposition output)
3. CompileGate
4. RuntimeValidationGate
5. SymbolReferenceGate
6. SpecConformanceGate (most callers depend on it; migrate last)
7. GoalDecompositionGate
8. DiffShapeGate
9. DiffReviewerGate

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
