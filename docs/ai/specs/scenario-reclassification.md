# T-SCENARIO-RECLASSIFY — Post-Translation Scenario Reclassification

## Problem

`TaskService.create_task()` at `apps/backend/app/services/tasks.py:90` stamps a `scenario` onto the task using the keyword heuristic `classify_request()` in `apps/backend/app/orchestrator/service.py:59`. Without a Jira key, codegen intents route to `process_question` / `action_with_approval` / `internal_db_query`, locking the planner into a `knowledge_answer` output contract with `must_touch_files=[]`. No patch is ever produced.

The semantic translator (MiniMax) already extracts `work_type`, `confidence`, `candidate_modules`, and `missing_information` correctly — see FX-NEWFILE run `data/e2e-reports/20260421-091919/FX-NEWFILE.json` where translation returned `work_type=feature`, `confidence=0.95`, zero missing info, but the task still completed as a knowledge answer.

## Goal

After `_translate_request` returns, override the initial `scenario` with a develop-path scenario when the translation signal clearly indicates code generation.

## Design

### New module: `apps/backend/app/services/scenario_reclassification.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.semantic_translation import SemanticTranslationDocument

# Scenarios that are NOT develop-capable — candidates for override.
_NON_DEVELOP_SCENARIOS = {
    "process_question",
    "action_with_approval",
    "internal_db_query",
    "internal_api_request",
    "slack_message",
    "jira_issue_create",
}

# work_type values that indicate code-generation intent.
_CODEGEN_WORK_TYPES = {"feature", "bug", "refactor", "chore"}

_MIN_CONFIDENCE = 0.6


@dataclass(frozen=True)
class ReclassificationResult:
    new_scenario: str
    changed: bool
    reason: str


def reclassify_scenario(
    *,
    current_scenario: str,
    translation: SemanticTranslationDocument,
) -> ReclassificationResult:
    """Decide whether to override the intake-time scenario using translation signal.

    Returns ``changed=False`` if the current scenario already routes to codegen
    (jira_issue_develop, jira_issue_plan, jira_issue_writeback) or if the
    translation signal is insufficient.

    Returns ``changed=True`` with ``new_scenario`` set to a develop scenario when:
    - current scenario is in _NON_DEVELOP_SCENARIOS
    - translation.work_type in _CODEGEN_WORK_TYPES
    - translation.confidence >= _MIN_CONFIDENCE
    - translation has candidate_modules OR requested_outputs naming file paths
    """
```

### Override target

The override target is `jira_issue_develop` when `translation.issue_key` is set, else `code_develop` (NEW scenario — see below).

### New scenario: `code_develop`

Add to the scenario space a `code_develop` scenario that exercises the same develop pipeline as `jira_issue_develop` but skips Jira prefetch and does not require an `issue_key`. Rationale: most of the existing `jira_issue_develop` branch logic at [orchestrator/service.py:146-164](apps/backend/app/orchestrator/service.py#L146) already synthesizes fake issue context when the Jira load fails — pulling that behavior into a dedicated scenario removes the lie in the telemetry.

Treat `code_develop` as a sibling of `jira_issue_develop` everywhere:

- `_infer_risk_category` in `tasks.py:378` returns the same category.
- Planner, codegen, gates, reviewer, runtime validation, semantic review, diff viewer — all treat `code_develop` identically to `jira_issue_develop`.
- The only difference: no Jira prefetch, no Jira writeback.

### Integration point

Modify `PrimaryOrchestrator._bootstrap_task_impl` in `apps/backend/app/orchestrator/service.py:137` immediately after `task.translation_json = semantic_translation.model_dump(mode="json")` at line 141:

```python
from app.services.scenario_reclassification import reclassify_scenario

# ... existing translation code ...
task.translation_json = semantic_translation.model_dump(mode="json")

# T-SCENARIO-RECLASSIFY: override intake-time scenario using translation signal
reclassification = reclassify_scenario(
    current_scenario=task.scenario,
    translation=semantic_translation,
)
if reclassification.changed:
    previous_scenario = task.scenario
    task.scenario = reclassification.new_scenario
    self.db.flush()
    record_event(
        self.db,
        task_id=task.id,
        event_type=EventType.SCENARIO_RECLASSIFIED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.PLANNING,
        role=RoleName.PRIMARY,
        message=f"Scenario reclassified from {previous_scenario} to {task.scenario} based on semantic translation.",
        payload={
            "previous_scenario": previous_scenario,
            "new_scenario": task.scenario,
            "reason": reclassification.reason,
            "work_type": semantic_translation.work_type,
            "confidence": semantic_translation.confidence,
        },
    )
```

### New event type

Add `SCENARIO_RECLASSIFIED` to `app/core/enums.py::EventType`.

### Risk category for `code_develop`

Add `code_develop` to the develop branch in `TaskService._infer_risk_category`:

```python
if scenario in {"jira_issue_create", "jira_issue_plan", "jira_issue_writeback", "code_develop"}:
    # ... existing develop category return
```

## Files to edit

| File | Action | Description |
|---|---|---|
| `apps/backend/app/services/scenario_reclassification.py` | CREATE | The reclassifier service |
| `apps/backend/app/orchestrator/service.py` | MODIFY | Call reclassifier after translation; extend scenario branches to include `code_develop` wherever `jira_issue_develop` is checked |
| `apps/backend/app/services/tasks.py` | MODIFY | Include `code_develop` in develop-risk branch of `_infer_risk_category` |
| `apps/backend/app/core/enums.py` | MODIFY | Add `SCENARIO_RECLASSIFIED` event type |
| `apps/backend/tests/services/test_scenario_reclassification.py` | CREATE | Unit tests for the reclassifier |
| `apps/backend/tests/orchestrator/test_scenario_reclassification_integration.py` | CREATE | Integration test showing a fixture-like task flips from process_question → code_develop |

## Acceptance criteria

1. `reclassify_scenario()` unit-tested for: positive cases (work_type=feature + high confidence + non-develop current), negative cases (already develop, low confidence, wrong work_type, missing required info).
2. Integration test: a task created with request `"Add a useDebounce hook in src/hooks/useDebounce.js that accepts value and delay"` and no Jira key starts as `process_question` but is reclassified to `code_develop` after translation. Verified by task.scenario + SCENARIO_RECLASSIFIED event.
3. `code_develop` scenario exercises the same planner/codegen/gates as `jira_issue_develop` end-to-end. Planner produces a plan with non-empty `must_touch_files`.
4. Existing develop-path tests still pass (Jira-based flows unchanged).
5. `python -m compileall apps/backend/app` passes.
6. `python -m pytest apps/backend/tests/ -x` passes.

## Non-goals

- Do NOT replace `classify_request()`. Keep the intake-time keyword heuristic as the initial guess. Reclassification is an override layer.
- Do NOT add new codegen / tool logic. `code_develop` reuses the existing develop pipeline.
- Do NOT wire MCP / intent resolution (that is T-IR-V2, out of scope).
- Do NOT change semantic translation behavior.

## Workflow for executor (codex)

1. Read this spec plus `apps/backend/app/orchestrator/service.py`, `apps/backend/app/services/tasks.py`, `apps/backend/app/schemas/semantic_translation.py`.
2. Create `scenario_reclassification.py` per the shape above.
3. Add `SCENARIO_RECLASSIFIED` to `EventType`.
4. Extend scenario branches: grep for `"jira_issue_develop"` across `apps/backend/app/` and include `"code_develop"` everywhere it's a peer (planner routing, risk category, review paths, UI-facing serializers if any).
5. Wire the reclassifier into `_bootstrap_task_impl` at the marked integration point.
6. Write unit + integration tests per acceptance criteria.
7. Run `python -m compileall apps/backend/app && python -m pytest apps/backend/tests/ -x`.
8. Do NOT commit. Stop after tests pass and report back.
