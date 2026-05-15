# T-LEARNING-LOOP-V3: Cold-Start Playbook Promotion

Status: proposed
Priority: P0
Owner: codex

## Problem

The learning loop now stabilizes known task families, but P69-17 showed a
dangerous edge: a broad domain playbook can be stable and still wrong for a
nearby subtype. If the system has no exact historical playbook, it must not
pretend it has a deterministic recipe.

We need a promotion pipeline for new task types:

- unknown task -> cold-start workflow
- repeated evidence -> draft playbook
- verified repeated success -> promoted playbook
- drift/staleness/failure -> demote back to cold-start

The goal is self-improvement without template pollution.

## Design

### 1. Cold-Start Mode

Used when no promoted playbook matches the exact task subtype.

Behavior:

- Use normal planner + evidence retrieval.
- Prefer structured edit intent where available, but do not force a recipe.
- Harness still owns file grounding, patch apply, compile, acceptance,
  SymbolGraph, semantic review, reservations, and approval routing.
- Terminal failures write `failure_observation`.
- Successful runs write candidate playbook evidence, not a promoted rule.

Hard rule: cold-start may rely on model reasoning, but never bypasses gates.

### 2. Draft Playbook Mode

Generated from observed runs, but quarantined.

Draft content may include:

- trigger phrases and negative trigger phrases
- candidate must-touch ranking logic
- required contracts
- forbidden patterns
- protected symbols
- common compile/repair failure observations
- source/dependency fingerprints

Draft content must not include:

- full answer templates
- unconditional static diffs
- paths invented outside retrieved evidence
- rules promoted from a single unverified run

Draft playbooks can be injected as warnings or checklists, but cannot drive a
deterministic fast path.

### 3. Promotion Gate

A draft becomes promoted only when all are true:

- At least N same-subtype runs reach normal approval.
- Compile, acceptance, SymbolGraph, runtime validation, and reservations pass.
- Semantic review has no unsuppressed high findings.
- Final diffs are stable enough or contracts are consistently satisfied.
- Human or high-confidence automated review confirms the subtype boundary.
- Repo/dependency fingerprints still match.

Promotion output is a constraint recipe, not an answer template.

### 4. Expiry And Demotion

Promoted playbooks are automatically demoted when:

- source/dependency fingerprint changes
- same-subtype failure rate exceeds threshold
- semantic review repeatedly contradicts deterministic gates
- a new failure class appears for the playbook
- trigger collision is detected with a neighboring subtype

Demotion sends future tasks back to cold-start mode.

## Implementation Plan

1. Add playbook metadata fields:
   - `status`: `draft | promoted | demoted`
   - `task_subtype`
   - `source_name`
   - `source_fingerprint`
   - `dependency_fingerprint`
   - `promotion_evidence_task_ids`
   - `last_verified_at`
   - `failure_rate_window`
   - `negative_triggers`

2. Add a playbook registry service:
   - load promoted playbooks for routing
   - load draft playbooks only as advisory memory
   - expose promotion/demotion decisions as auditable events

3. Add cold-start router logic:
   - exact promoted subtype match -> normal playbook path
   - broad family but no subtype match -> cold-start
   - draft subtype match -> cold-start plus draft warnings

4. Add draft synthesis:
   - extract contracts from passed gates and semantic/reservation feedback
   - extract candidate file ranking from evidence and modified files
   - extract negative examples from failed neighboring tasks
   - save as draft only

5. Add promotion evaluator:
   - scheduled/manual evaluation over recent tasks
   - require repeated normal approvals and clean deterministic gates
   - emit `playbook.promoted` or keep `draft`

6. Add expiry evaluator:
   - check fingerprints and recent failures before routing
   - demote stale or drifting playbooks

## Acceptance Criteria

- A task with no exact promoted subtype does not use deterministic fast path.
- A successful unknown task creates or updates a draft playbook only.
- A draft playbook can influence prompts as an advisory warning, but cannot
  force must-touch files or skip planner/codegen.
- A draft promotes only after repeated verified approvals.
- A promoted playbook demotes when dependency/source fingerprints drift.
- P69-17-style neighboring subtype collision is covered by regression tests:
  P69-19 signup/KYC map selection cannot satisfy P69-17 job-default-address
  contracts, and vice versa.

## Files To Edit

- `apps/backend/app/services/domain_classifier.py`
- `apps/backend/app/services/memory.py`
- `apps/backend/app/services/playbook_registry.py` (new)
- `apps/backend/app/orchestrator/service.py`
- `apps/backend/data/domain_playbooks/*.yaml`
- `apps/backend/tests/services/test_domain_classifier.py`
- `apps/backend/tests/services/test_playbook_registry.py` (new)
- `apps/backend/tests/orchestrator/test_playbook_promotion_loop.py` (new)

## Workflow

Workflow (for the executor): codex
