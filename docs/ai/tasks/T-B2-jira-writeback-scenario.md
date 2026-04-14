# T-B2 — Jira Writeback Scenario Wiring

## Goal

Wire a new `jira_issue_writeback` scenario end-to-end so the orchestrator can route requests like "把 OPS-123 标记为 in progress 并加评论" through the `jira.add_comment` and `jira.transition_issue` tools added in T-B1. A single approval covers both tool calls.

## Background

T-B1 added `jira.transition_issue` and `jira.add_comment` tools to the tool registry, gateway, and governance policy seeds. They are fully functional but unreachable: no scenario routes to them, no plan generates them, and no action payload maps to them.

The existing `jira_issue_plan` scenario (read-only) is the pattern to follow. The new `jira_issue_writeback` scenario differs in that it:

1. Needs an issue key (same as `jira_issue_plan`) PLUS a transition name AND/OR comment text.
2. Chains two tool calls sequentially: comment first, then transition.
3. Is `APPROVAL_REQUIRED` (MEDIUM risk), unlike `jira_issue_plan` which is LOW risk and auto-approved.
4. Uses the same Jira prefetch for initial status context.

The current `_execute_plan` in `apps/backend/app/orchestrator/service.py` executes exactly one tool per plan (via `_resolve_tool_name`). For the writeback scenario, we extend it with a small multi-tool loop for this scenario specifically.

## Files to edit

### 1. `apps/backend/app/orchestrator/service.py` — Scenario detection + execution

**`classify_request` (line 32):**

Add a new clause BEFORE the existing `jira_issue_plan` check. The writeback scenario takes priority when the request contains a Jira reference AND writeback keywords:

```python
def classify_request(request_text: str) -> str:
    lowered = request_text.lower()
    jira_reference = extract_jira_issue_reference(request_text)

    # NEW: writeback takes priority over plan when transition/comment keywords present
    if jira_reference and any(
        keyword in lowered
        for keyword in (
            "transition", "move to", "status", "标记为", "推进", "移到",
            "in progress", "done", "complete", "close", "reopen",
            "comment", "评论", "备注", "note",
        )
    ):
        return "jira_issue_writeback"

    # existing jira_issue_plan clause unchanged
    if jira_reference and (
        looks_like_jira_issue_url(request_text)
        or _contains_word(lowered, "plan", "breakdown", "implement", ...)
    ):
        return "jira_issue_plan"
    # ... rest unchanged
```

**`bootstrap_task` (around line 72):**

Add an `elif task.scenario == "jira_issue_writeback":` branch right after the existing `jira_issue_plan` block. This branch:

1. Calls `_prefetch_jira_issue_context` exactly like `jira_issue_plan` to get the current issue status (needed for `from_status` display and to verify the issue exists).
2. Re-translates with issue context (same pattern).
3. Does NOT prefetch planning repository context (not needed for writeback).
4. Augments `planning_request_text` with issue context.

```python
elif task.scenario == "jira_issue_writeback":
    issue_context = self._prefetch_jira_issue_context(
        task=task,
        actor_name=actor_name,
        issue_key=semantic_translation.issue_key,
    )
    if issue_context is None:
        return

    semantic_translation = self._translate_request(
        task=task,
        actor_name=actor_name,
        issue_context=issue_context,
    )
    task.translation_json = semantic_translation.model_dump(mode="json")

    planning_request_text = self._augment_request_with_context(
        original_request=task.request_text,
        translation_document=task.translation_json,
        issue_context=issue_context,
        planning_knowledge_context=None,
    )
```

**`_execute_plan` (around line 697):**

After the existing single-tool execution block (lines 766-816), add a writeback-specific block that chains two tool calls when `task.scenario == "jira_issue_writeback"`. The approach:

- Check `task.scenario == "jira_issue_writeback"` at the top of `_execute_plan`.
- If so, resolve the tool list from the plan (there will be 2 tools: `jira.add_comment` and `jira.transition_issue`).
- Execute them sequentially, each with its own events and error handling.
- If the comment succeeds but the transition fails, the task still fails but the comment is recorded.
- Merge both results into `task.latest_result_json`.

Implementation approach — insert a new method `_execute_writeback_plan` and call it from `_execute_plan` when the scenario matches:

```python
def _execute_plan(self, *, task, actor_name, plan, approval_id=None):
    if task.scenario == "jira_issue_writeback":
        return self._execute_writeback_plan(
            task=task, actor_name=actor_name, plan=plan, approval_id=approval_id,
        )
    # ... existing single-tool execution continues unchanged
```

New method `_execute_writeback_plan`:

```python
def _execute_writeback_plan(
    self,
    *,
    task: Task,
    actor_name: str,
    plan: GeneratedPlan,
    approval_id: str | None = None,
) -> None:
    """Chain jira.add_comment + jira.transition_issue under a single approval."""
    semantic_translation = (
        GeneratedSemanticTranslation.model_validate(task.translation_json or {})
        if task.translation_json
        else self.semantic_translator.translate(
            task_id=task.id,
            request_text=task.request_text,
            scenario=task.scenario,
            actor_name=actor_name,
        ).translation
    )
    if not task.translation_json:
        task.translation_json = semantic_translation.model_dump(mode="json")

    base_payload = self.action_agent.build_payload(
        task_id=task.id,
        request_text=task.request_text,
        scenario=task.scenario,
        semantic_translation=semantic_translation,
    )
    # base_payload has: issue_key, transition_name, text, task_id

    set_task_status(
        self.db, task=task,
        new_status=TaskStatus.EXECUTING,
        new_stage=WorkflowStage.ACTION,
        role=RoleName.ACTION,
        source=EventSource.ORCHESTRATOR,
        message="Task entered writeback execution after approval.",
        payload={"approval_id": approval_id},
    )
    record_event(
        self.db, task_id=task.id,
        event_type=EventType.EXECUTION_STARTED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.ACTION,
        role=RoleName.ACTION,
        message="Jira writeback execution started.",
        payload={"plan_id": plan.plan_id, "approval_id": approval_id},
    )

    combined_result: dict[str, object] = {}

    # Step 1: add comment (if text is non-empty)
    comment_text = base_payload.get("text", "")
    if comment_text and str(comment_text).strip():
        tool_name = "jira.add_comment"
        comment_payload = {
            "issue_key": base_payload["issue_key"],
            "text": comment_text,
        }
        record_event(
            self.db, task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            tool_name=tool_name,
            message="Requesting Jira comment post.",
            payload={"approval_id": approval_id, "payload_preview": comment_payload},
        )
        try:
            comment_result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name=tool_name,
                payload=comment_payload,
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                approval_id=approval_id,
            )
            self._sync_retry_count(task)
            record_event(
                self.db, task_id=task.id,
                event_type=EventType.TOOL_SUCCEEDED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Jira comment posted.",
                payload=comment_result,
            )
            combined_result["comment"] = comment_result
        except Exception as exc:
            self._sync_retry_count(task)
            # Comment failure is non-fatal if transition is also requested;
            # record the error but continue to transition.
            record_event(
                self.db, task_id=task.id,
                event_type=EventType.TOOL_FAILED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Jira comment post failed.",
                payload={"error": str(exc)},
            )
            combined_result["comment_error"] = str(exc)

    # Step 2: transition issue (if transition_name is non-empty)
    transition_name = base_payload.get("transition_name", "")
    if transition_name and str(transition_name).strip():
        tool_name = "jira.transition_issue"
        transition_payload = {
            "issue_key": base_payload["issue_key"],
            "transition_name": transition_name,
        }
        record_event(
            self.db, task_id=task.id,
            event_type=EventType.TOOL_CALL_REQUESTED,
            source=EventSource.TOOL_GATEWAY,
            stage=WorkflowStage.ACTION,
            role=RoleName.ACTION,
            tool_name=tool_name,
            message="Requesting Jira status transition.",
            payload={"approval_id": approval_id, "payload_preview": transition_payload},
        )
        try:
            transition_result = self.tool_gateway.execute(
                task_id=task.id,
                tool_name=tool_name,
                payload=transition_payload,
                actor_context={"actor_name": actor_name, "task_id": task.id},
                session_id=task.session_id,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                approval_id=approval_id,
            )
            self._sync_retry_count(task)
            record_event(
                self.db, task_id=task.id,
                event_type=EventType.TOOL_SUCCEEDED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Jira issue transitioned.",
                payload=transition_result,
            )
            combined_result["transition"] = transition_result
        except Exception as exc:
            self._sync_retry_count(task)
            record_event(
                self.db, task_id=task.id,
                event_type=EventType.TOOL_FAILED,
                source=EventSource.TOOL_GATEWAY,
                stage=WorkflowStage.ACTION,
                role=RoleName.ACTION,
                tool_name=tool_name,
                message="Jira transition failed.",
                payload={"error": str(exc)},
            )
            combined_result["transition_error"] = str(exc)
            # Transition failure IS fatal for the task
            task.latest_result_json = {
                "status": TaskStatus.FAILED.value,
                "message": f"Jira transition failed: {exc}",
                **combined_result,
            }
            set_task_status(
                self.db, task=task,
                new_status=TaskStatus.FAILED,
                new_stage=WorkflowStage.DONE,
                role=RoleName.ACTION,
                source=EventSource.ORCHESTRATOR,
                message="Task failed during Jira transition.",
            )
            return

    # If we got here, at least the transition succeeded (or wasn't requested)
    has_comment = "comment" in combined_result
    has_transition = "transition" in combined_result
    status_msg = []
    if has_comment:
        status_msg.append(f"commented on {base_payload['issue_key']}")
    if has_transition:
        tr = combined_result["transition"]
        status_msg.append(
            f"transitioned {base_payload['issue_key']} "
            f"from {tr.get('from_status', '?')} to {tr.get('to_status', '?')}"
        )

    task.latest_result_json = {
        "status": TaskStatus.COMPLETED.value,
        "message": "Jira writeback completed: " + " and ".join(status_msg) + ".",
        **combined_result,
    }
    set_task_status(
        self.db, task=task,
        new_status=TaskStatus.COMPLETED,
        new_stage=WorkflowStage.DONE,
        role=RoleName.ACTION,
        source=EventSource.ORCHESTRATOR,
        message="Jira writeback task completed.",
    )
    record_event(
        self.db, task_id=task.id,
        event_type=EventType.FINAL_RESPONSE_EMITTED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.DONE,
        role=RoleName.PRIMARY,
        message="Final response emitted after Jira writeback.",
        payload=combined_result,
    )
```

### 2. `apps/backend/app/agents/service.py` — Plan generation + action payload

**`generate_plan` (after the `jira_issue_create` block, around line 348):**

Add a new `if scenario == "jira_issue_writeback":` branch. Model it on `jira_issue_create` but with two tools and MEDIUM risk + requires_approval=True:

```python
if scenario == "jira_issue_writeback":
    tools = []
    comment_tool = "jira.add_comment"
    transition_tool = "jira.transition_issue"
    comment_category = registry.get_permission_category(comment_tool)
    transition_category = registry.get_permission_category(transition_tool)
    tools.append(PlanTool(
        tool_name=comment_tool,
        permission_category=comment_category,
        purpose="Post a progress comment to the Jira issue.",
    ))
    tools.append(PlanTool(
        tool_name=transition_tool,
        permission_category=transition_category,
        purpose="Transition the Jira issue to the requested workflow status.",
    ))
    issue_summary = str(issue_context.get("summary") or "").strip() if issue_context else ""
    current_status = str(issue_context.get("issue_status") or "").strip() if issue_context else ""
    return GeneratedPlanPayload(
        objective="Write status and comments back to the Jira issue.",
        request_summary=request_summary,
        scenario=scenario,
        change_summary=(issue_summary or change_summary)[:320],
        change_explanation=change_explanation[:1200],
        assumptions=[
            "The referenced Jira issue exists.",
            "The requested transition is available in the issue's current workflow state.",
        ],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        approval_reasons=[
            "Jira writeback tools are mapped to approval_required in the current tool policy."
        ],
        affected_code_locations=[],
        tools=tools,
        steps=[
            PlanStep(
                step_id="step_1",
                title="Validate the writeback request",
                kind="analysis",
                owner_role=RoleName.PLANNER,
                depends_on=[],
                tool_name=None,
                expected_output="Validated issue key, transition name, and comment text.",
                success_criteria="The request contains a valid issue key and at least one writeback action.",
            ),
            PlanStep(
                step_id="step_2",
                title="Post progress comment to Jira",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=["step_1"],
                tool_name=comment_tool,
                expected_output="Comment ID and creation timestamp.",
                success_criteria="The comment is visible on the Jira issue.",
            ),
            PlanStep(
                step_id="step_3",
                title="Transition the Jira issue status",
                kind="action",
                owner_role=RoleName.ACTION,
                depends_on=["step_2"],
                tool_name=transition_tool,
                expected_output="From-status and to-status confirmation.",
                success_criteria="The issue status matches the requested target.",
            ),
            PlanStep(
                step_id="step_4",
                title="Review the writeback results",
                kind="review",
                owner_role=RoleName.REVIEWER,
                depends_on=["step_3"],
                tool_name=None,
                expected_output="Confirmed writeback result for dashboard display.",
                success_criteria="Both comment and transition completed successfully.",
            ),
        ],
        final_output_contract=FinalOutputContract(
            type="jira_writeback_result",
            required_fields=["status", "issue_key", "comment", "transition"],
        ),
    )
```

**`_build_planning_instructions` (line 1040):**

Add one line to the instruction string:

```python
"If the scenario is jira_issue_writeback, the allowed tools are jira.add_comment and jira.transition_issue. "
```

Insert it after the `jira_issue_create` line. Also add `jira.add_comment` and `jira.transition_issue` to the master tool list at line 1047.

**`ActionAgent.build_payload` (line 1081):**

Add a new branch after the `jira_issue_plan` block (after line 1146):

```python
if scenario == "jira_issue_writeback":
    issue_reference = extract_jira_issue_reference(request_text)
    issue_key = (
        semantic_translation.issue_key
        if semantic_translation and semantic_translation.issue_key
        else issue_reference.issue_key if issue_reference else ""
    )

    # Extract transition name from request text
    transition_name = ""
    # Try semantic translation objective first
    # Then fall back to keyword extraction
    transition_keywords = {
        "in progress": "In Progress",
        "to do": "To Do",
        "done": "Done",
        "in review": "In Review",
        "complete": "Done",
        "close": "Done",
        "reopen": "To Do",
    }
    lowered = request_text.lower()
    for keyword, mapped_name in transition_keywords.items():
        if keyword in lowered:
            transition_name = mapped_name
            break

    # If semantic translation has a more specific objective, prefer it
    if semantic_translation and semantic_translation.objective:
        obj_lower = semantic_translation.objective.lower()
        for keyword, mapped_name in transition_keywords.items():
            if keyword in obj_lower:
                transition_name = mapped_name
                break

    # Extract comment text: look for text after "comment"/"评论"/"备注" markers,
    # or use the normalized request as the comment body
    comment_text = ""
    comment_markers = ["comment:", "评论:", "备注:", "note:", "comment ", "评论 ", "备注 "]
    for marker in comment_markers:
        idx = lowered.find(marker)
        if idx >= 0:
            comment_text = request_text[idx + len(marker):].strip()
            # Trim off any trailing transition keywords
            break

    if not comment_text and semantic_translation and semantic_translation.normalized_request:
        comment_text = semantic_translation.normalized_request

    return {
        "issue_key": issue_key,
        "transition_name": transition_name,
        "text": comment_text,
        "task_id": task_id,
    }
```

### 3. `apps/backend/app/agents/translation.py` — Scenario inference maps

**`_infer_work_type` (line 97):**

Add after the `jira_issue_plan` check:

```python
if scenario == "jira_issue_writeback":
    return "operations"
```

**`_infer_intent` (line 114):**

Add to the `intent_map` dict:

```python
"jira_issue_writeback": "writeback_jira_issue",
```

**`_infer_risk_category` (around line 302):**

Update the existing `jira_issue_create` / `jira_issue_plan` line to also include `jira_issue_writeback`:

```python
if scenario in {"jira_issue_create", "jira_issue_plan", "jira_issue_writeback"}:
    return RiskCategory.CHANGE_MANAGEMENT
```

### 4. `apps/backend/app/services/tasks.py` — Scenario set + title

Two changes:

**`_infer_risk_category` (line 302):** The existing check `if scenario in {"jira_issue_create", "jira_issue_plan"}:` must add `"jira_issue_writeback"`:

```python
if scenario in {"jira_issue_create", "jira_issue_plan", "jira_issue_writeback"}:
    return RiskCategory.CHANGE_MANAGEMENT
```

**`_build_title` (line 273):** Add a clause before the existing `jira_issue_plan` title logic to generate a better title for writeback requests:

```python
if issue_reference and any(
    keyword in lowered
    for keyword in ("transition", "status", "标记为", "推进", "comment", "评论", "备注")
):
    return f"Jira writeback {issue_reference.issue_key}"
```

## Files to create

### 5. `apps/backend/tests/orchestrator/test_jira_writeback_scenario.py`

Unit tests using `unittest.mock`. Stub `ToolGateway.execute` and `SemanticTranslator.translate`. Tests:

1. **`test_classify_request_writeback_with_transition`** — "把 OPS-123 标记为 in progress" → `"jira_issue_writeback"`.
2. **`test_classify_request_writeback_with_comment`** — "在 OPS-123 上加评论" → `"jira_issue_writeback"`.
3. **`test_classify_request_plan_not_writeback`** — "plan the implementation for OPS-123" → `"jira_issue_plan"` (not writeback).
4. **`test_generate_plan_writeback`** — Call `generate_plan` with `scenario="jira_issue_writeback"` and verify it returns a `GeneratedPlanPayload` with 2 tools (`jira.add_comment`, `jira.transition_issue`), `requires_approval=True`, `risk_level=MEDIUM`.
5. **`test_build_payload_writeback`** — Call `ActionAgent().build_payload` with `scenario="jira_issue_writeback"` and request text "把 OPS-123 标记为 in progress 评论: 已开始处理". Verify the returned dict has `issue_key`, `transition_name="In Progress"`, `text` containing "已开始处理".

Construct minimal mock objects. Do not spin up FastAPI or a real DB.

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- `classify_request("把 OPS-123 标记为 in progress")` returns `"jira_issue_writeback"`.
- `classify_request("plan the work for OPS-123")` still returns `"jira_issue_plan"` (no regression).
- The new tests pass: `python -m pytest apps/backend/tests/orchestrator/test_jira_writeback_scenario.py -q` (or unittest equivalent).
- `generate_plan` for `jira_issue_writeback` returns a plan with 2 tools and `requires_approval=True`.
- `ActionAgent.build_payload` for `jira_issue_writeback` extracts `issue_key`, `transition_name`, and `text`.
- The `_build_planning_instructions` string mentions both new tools.
- Save test output to `docs/ai/runs/T-B2.log`.

## Out of scope

- Actually calling the Jira API (that's integration testing).
- Chat lifecycle rendering (Phase H).
- Approval UI inline buttons (Phase F/H).
- Frontend changes — the chat panel already renders task results; the new scenario will appear as a task with its `latest_result_json`.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/orchestrator/service.py` (full file), `apps/backend/app/agents/service.py` (full file), `apps/backend/app/agents/translation.py` (full file). Confirm the exact line numbers and patterns before editing.
2. Check whether `apps/backend/tests/orchestrator/` exists. If not, create it with an `__init__.py`.
3. Implement in order:
   a. `orchestrator/service.py` — `classify_request` new clause + `bootstrap_task` new branch + `_execute_writeback_plan` method + early return in `_execute_plan`.
   b. `agents/service.py` — `generate_plan` new branch + `_build_planning_instructions` update + `ActionAgent.build_payload` new branch.
   c. `agents/translation.py` — `_infer_work_type`, `_infer_intent`, `_infer_risk_category` updates.
   d. Tests.
4. Run `python -m compileall app` from `apps/backend/`.
5. Run the tests. Save output to `docs/ai/runs/T-B2.log`.
6. Do not touch any file outside the list above. Do not modify the tool registry, gateway, or governance files (those are T-B1, already done).

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-B2-jira-writeback-scenario.md
```
