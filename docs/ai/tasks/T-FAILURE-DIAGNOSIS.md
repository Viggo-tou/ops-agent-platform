# T-FAILURE-DIAGNOSIS — Auto-generate human-readable root-cause summary on task failure

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

When a develop-pipeline task transitions to `AWAITING_APPROVAL` (cap exceeded) or `FAILED`, run an LLM-based diagnostic step that reads the failure context (events timeline + raw tool errors + sandbox state) and writes a human-readable root-cause summary in the user's language. Persist the summary on the task and surface it in the approval UI / chat, so the human reviewer can act on the failure without having to read raw stderr or grep events.

## Background

### Why this exists

A real failure observed on task `29fe0fc3` (Plan Jira issue P69-7, 2026-04-27):

- compile gate ran 3 repair rounds against `handyman-admin-dashboard/src/data/jobData.js`, all failed
- task transitioned to `AWAITING_APPROVAL` with `repair_summary` containing raw Node.js stderr:
  `Error: Invalid package config D:\项目\...\sandboxes\<id>\package.json. at getPackageScopeConfig (node:internal/modules/package_json_reader:160:33)`
- The actual root cause was a malformed `package.json` (line 8 col 5: missing comma) at the sandbox root, NOT a syntax error in `jobData.js`.
  Claude Code CLI correctly diagnosed each round ("the file is structurally valid"), but the compile gate kept re-blaming `jobData.js`, so 3 repair rounds were wasted on a healthy file.
- The user could not act on the awaiting_approval state without external help to interpret the stderr — defeating the purpose of the approval handoff.

### What this fixes

This card does NOT fix the misattribution bug in compile gate (that's a separate ticket — see "Out of scope"). What it DOES is: even when an upstream gate misroutes blame, the post-mortem diagnostic LLM reads the WHOLE picture (gate output + tool errors + sandbox state + events) and tells the human: "Here's what actually happened, here's the most likely root cause, here's what to do."

Conceptually: Ops should explain its own failures the way a senior engineer would explain them in a postmortem doc.

### Dependencies

- T-WS-FS-WORKSPACE has shipped (`015d256` + `f646cb6`); EvidenceItem schema available.
- T-PIPELINE-REPAIR-CAP has shipped (`75adc6d`); awaiting_approval handoff is the natural trigger point.
- LLM provider chain in `apps/backend/app/services/codegen.py::_resolve_provider_chain` already supports claude_code / codex / anthropic / minimax — reuse it.

## Design

### A. Trigger points

The diagnostic runs automatically on these transitions:

1. `TASK_STATUS_CHANGED` → `AWAITING_APPROVAL` with `result.decision == "compile_repair_cap_exceeded"`.
2. `TASK_STATUS_CHANGED` → `AWAITING_APPROVAL` with any future cap-exceeded variants (runtime_validation cap, etc.).
3. `TASK_STATUS_CHANGED` → `FAILED` for any develop-scenario task where the failure was a tool error (not user cancellation).

The trigger is a new orchestrator hook `_run_failure_diagnosis(task)` called from `_mark_awaiting_approval()` and `_mark_task_failed()`.

### B. Inputs to the diagnostic LLM

A new module `apps/backend/app/services/failure_diagnosis.py` builds a structured `FailureContext`:

```python
class FailureContext(BaseModel):
    task_id: str
    scenario: str                              # "jira_issue_develop", etc.
    failure_kind: Literal[                     # what triggered the diagnosis
        "compile_repair_cap_exceeded",
        "runtime_validation_cap_exceeded",
        "tool_failed_terminal",
        "approval_rejected",
    ]
    last_n_events: list[EventSummary]          # last 30 events, structured
    repair_summary: dict | None                # latest_result_json.result if present
    residual_errors: list[str]                 # raw stderr / tool error messages
    sandbox_dir: str | None                    # path to sandbox root
    sandbox_keyfiles: dict[str, str]           # {filename: head_500_chars} for known keyfiles
    plan_json: dict | None                     # task.plan_json
    user_request: str                          # task.request_text
```

`sandbox_keyfiles` deliberately includes commonly-causing-trouble files even if NOT mentioned in residual_errors:
- `package.json` (root + immediate subdirs)
- `tsconfig.json`, `jsconfig.json`
- `.env` filename only (don't read content), so diagnostic LLM can mention "no .env present" not the secrets

### C. Diagnostic prompt

The LLM is asked to produce:

```python
class DiagnosisOutput(BaseModel):
    summary: str          # 1-2 sentences, user's language (zh-CN if request was zh-CN)
    root_cause: str       # 2-4 sentences, technical detail
    likely_fix: str       # 2-4 sentences, concrete next step
    confidence: Literal["high", "medium", "low"]
    related_files: list[str]  # files the human should look at
```

Prompt template (in `apps/backend/app/services/failure_diagnosis_prompts.py`):

```
You are a senior engineer doing a post-mortem on a failed automated coding task.

The system attempted: {user_request}

It ran this plan: {plan_summary}

The pipeline failed at: {failure_kind}

Last events:
{events_table}

Tool errors (raw):
{residual_errors}

Sandbox key files:
{sandbox_keyfiles_block}

Your job:
1. Identify the ACTUAL root cause, even if the gate's blamed file is wrong.
2. Distinguish "target file is broken" from "external/sandbox state is broken"
   from "LLM output was wrong" from "tool config is missing".
3. Write a 1-2 sentence summary in {user_language} that a non-engineer
   reviewer can act on.
4. List the files a human should LOOK AT (not necessarily edit).
5. State your confidence — "high" only if the root cause is unambiguous from
   the data; "low" if you're guessing.

Output JSON matching the DiagnosisOutput schema. Do not output prose outside the JSON.
```

### D. Output persistence + UI

- New event type `FAILURE_DIAGNOSIS_GENERATED` recorded against the task with the full DiagnosisOutput in payload.
- `Task.latest_result_json` gains a `failure_diagnosis` field with the same structure.
- Frontend approval block reads `failure_diagnosis` and renders:
  - Summary at the top (bold, larger font)
  - Expandable "Technical detail" section with root_cause + likely_fix + related_files
  - Confidence badge (green / yellow / orange)
  - Fall-back: if no diagnosis present (LLM failed / disabled), show current raw `repair_summary` UI unchanged.

### E. Failure modes of the diagnostic itself

- **LLM call timeout / error**: log warning, do not block task transition. Approval UI falls back to raw `repair_summary`.
- **Diagnosis budget**: max 1 attempt per task transition. Hard timeout 30s. No retry.
- **Provider fallback**: reuse `_resolve_provider_chain` ordering. **In current operation: claude_code CLI first, then codex CLI**. anthropic / minimax exist as API fallbacks because their keys are still in `.env`, but they should NOT be the default diagnosis source — if either fires, log a WARN ("diagnosis fell back to API provider X — check why CLI failed"). If all providers fail, skip diagnosis silently.
- **Confidence handling**: a "low" confidence diagnosis is still shown but with a clear "best guess" disclaimer in UI.

### F. Configuration

New settings in `app/core/config.py`:
```python
failure_diagnosis_enabled: bool = True
failure_diagnosis_timeout_seconds: float = 30.0
failure_diagnosis_max_events: int = 30
failure_diagnosis_keyfile_head_chars: int = 500
```

Env overrides:
```
OPS_AGENT_FAILURE_DIAGNOSIS_ENABLED=true
OPS_AGENT_FAILURE_DIAGNOSIS_TIMEOUT_SECONDS=30
```

## Files to create

1. `apps/backend/app/services/failure_diagnosis.py` — main module: `FailureContext`, `DiagnosisOutput`, `run_diagnosis(task, db, settings)` entry point.
2. `apps/backend/app/services/failure_diagnosis_prompts.py` — prompt templates + `build_diagnostic_prompt(ctx)` function.
3. `apps/backend/tests/services/test_failure_diagnosis.py` — unit tests (see Tests section).
4. `apps/backend/tests/orchestrator/test_failure_diagnosis_integration.py` — orchestrator integration tests.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — add `_run_failure_diagnosis(task)` call from `_mark_awaiting_approval()` and `_mark_task_failed()`. Wrap in try/except; never let diagnosis failure break the parent transition.
2. `apps/backend/app/core/config.py` — add the 4 new settings listed above.
3. `apps/backend/app/models/task.py` (or wherever `latest_result_json` is shaped) — document the new `failure_diagnosis` field. Schema is JSON so no migration needed.
4. `apps/backend/app/events.py` — add `EventType.FAILURE_DIAGNOSIS_GENERATED`.
5. `apps/web/src/components/chat/AwaitingApprovalBlock.tsx` (NEW; coordinated with T-CHAT-APPROVAL-UX) — render the diagnosis block. If T-CHAT-APPROVAL-UX hasn't shipped yet, fall back to editing `MessageList.tsx` to render the diagnosis inline.

## Tests

### Unit tests (test_failure_diagnosis.py)

1. `test_failurecontext_built_from_compile_repair_cap_task` — given a fixture task with `result.decision == "compile_repair_cap_exceeded"` and 30+ events, FailureContext.last_n_events has exactly 30, residual_errors contains the cap message.
2. `test_sandbox_keyfiles_includes_package_json_when_present` — fixture sandbox dir has package.json → keyfiles dict has it. No package.json → key absent.
3. `test_sandbox_keyfiles_skips_dotenv_content` — sandbox has `.env` → keyfiles has the FILENAME but content is `<redacted>` placeholder.
4. `test_diagnosis_output_parses_valid_llm_response` — given a JSON LLM response, parses into DiagnosisOutput.
5. `test_diagnosis_output_rejects_malformed_llm_response` — given non-JSON or wrong-schema LLM response, raises typed error.
6. `test_run_diagnosis_returns_none_on_llm_timeout` — mock LLM that hangs; run_diagnosis with timeout=1s returns None, logs warning.
7. `test_run_diagnosis_skipped_when_disabled` — settings.failure_diagnosis_enabled=False → run_diagnosis returns None without calling LLM.
8. `test_provider_fallback_when_first_provider_fails` — claude_code raises CodegenError → codex called → returns valid diagnosis.

### Integration tests (test_failure_diagnosis_integration.py)

9. `test_orchestrator_calls_diagnosis_on_awaiting_approval` — set up develop pipeline that hits compile_repair_cap. Verify `_run_failure_diagnosis` was called once. Verify event `FAILURE_DIAGNOSIS_GENERATED` recorded. Verify `task.latest_result_json["failure_diagnosis"]` set.
10. `test_orchestrator_calls_diagnosis_on_failed_task` — set up develop pipeline that fails terminally on a tool error. Verify diagnosis runs.
11. `test_orchestrator_does_not_call_diagnosis_on_normal_completion` — successful develop task. Verify NO `FAILURE_DIAGNOSIS_GENERATED` event.
12. `test_diagnosis_failure_does_not_break_task_transition` — mock failure_diagnosis to raise. Task still transitions to AWAITING_APPROVAL correctly. Warning logged.

### Snapshot test for the P69-7-style failure

13. `test_p69_7_style_failure_produces_correct_root_cause` — fixture replaying the P69-7 events + sandbox state (broken package.json, claude_code refusing to modify jobData.js). Run diagnosis with a recorded LLM mock. Assert summary mentions "package.json" and `related_files` includes the malformed package.json path. **This is the regression test that proves the original observed failure now produces a useful diagnosis.**

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 13 new tests pass.
- Full suite still green.
- Backend log on awaiting_approval transition: `failure_diagnosis: {confidence} - {summary[:80]}`.
- Manual verification: re-run a known-broken task (intentionally corrupt sandbox `package.json`); approval UI shows the diagnosis block with summary in the user's language.
- Diagnosis is gated by config: setting `OPS_AGENT_FAILURE_DIAGNOSIS_ENABLED=false` disables it cleanly.
- No new external dependency introduced (reuse existing LLM provider chain).

## Out of scope (explicitly NOT in this card)

- Fixing the compile gate's mis-classification of `Invalid package config` as a target-file syntax error. That's `T-COMPILE-GATE-ERROR-CLASSIFICATION` (separate ticket).
- Adding sandbox pre-flight validation that rejects malformed package.json before pipeline starts. That's `T-SANDBOX-PREFLIGHT` (separate ticket).
- Adding `provider_used` to codegen events. That's `T-CODEGEN-PROVIDER-OBSERVABILITY` (separate ticket).
- Auto-applying the diagnostic's `likely_fix` (e.g., automatically copying a healthy package.json over a broken one). The diagnostic is read-only — humans still grant/reject.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read current `_mark_awaiting_approval` and `_mark_task_failed` in `apps/backend/app/orchestrator/service.py` to understand the transition hook points.
2. Read `_resolve_provider_chain` and `_call_*` methods in `apps/backend/app/services/codegen.py` to understand how to reuse the provider chain for a non-codegen LLM call.
3. Read `apps/backend/app/events.py` for the EventType enum pattern.
4. Implement `failure_diagnosis.py` with `FailureContext`, `DiagnosisOutput`, `run_diagnosis()`. Reuse provider chain via a thin wrapper (don't duplicate the resolution logic).
5. Implement `failure_diagnosis_prompts.py`. Keep prompt deterministic — same input should produce same output across runs (set temperature=0 if provider supports).
6. Wire the orchestrator hooks. Wrap diagnosis call in try/except — failure transitions must NEVER be blocked by diagnosis errors.
7. Add config settings + env override parsing.
8. Write all 13 tests. Use `unittest.mock` for LLM responses; do NOT make real LLM calls in tests.
9. Run `python -m compileall app` and full test suite.
10. Manually verify: corrupt a sandbox `package.json` (the P69-7 reproduction), trigger a task, observe diagnosis appears in event log + on task.latest_result_json.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-FAILURE-DIAGNOSIS.md
```

## Follow-up tickets unblocked by this one

- `T-COMPILE-GATE-ERROR-CLASSIFICATION` — type the gate's errors so repair loop doesn't spin on healthy files. Diagnosis can't replace this; just hide its bad UX.
- `T-SANDBOX-PREFLIGHT` — validate critical config files (package.json / tsconfig.json) at sandbox setup. If corrupt, fail fast with a clear message instead of letting the gate misroute.
- `T-CODEGEN-PROVIDER-OBSERVABILITY` — record `provider_used` in `codegen.generate_patch` events so diagnosis (and future analytics) can answer "did claude_code actually run?".
