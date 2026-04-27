# T-CODEGEN-PROVIDER-OBSERVABILITY — Record which provider actually ran each codegen call

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P0 — instrumentation prerequisite for any provider-related decision)
**Priority:** P0 (without this, every "codegen quality" question is unverifiable)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Record which provider actually executed each `codegen.generate_patch` and `codegen.repair` call, plus the fallback chain attempted before it. Store this in the event payload so post-mortem queries can answer questions like "did claude_code actually run for P69-7's failed batches?" without grepping backend stdout logs.

**Current operational chain** (2026-04-28): `claude_code` CLI (npx) and `codex` CLI are the intended defaults. `anthropic` / `minimax` exist as API fallbacks because their keys remain in `.env`; if either of them ever runs in production, that is a SIGNAL ("CLI providers failed and we silently burned API budget") that this observability layer is designed to surface. The `kind` enum below stays broad to capture every possible execution path, but the operational analysis focuses on the two CLI providers.

## Background

Real failure observed on task `29fe0fc3` (Plan Jira issue P69-7, 2026-04-27): compile gate ran 3 repair rounds, all failed, task transitioned to `AWAITING_APPROVAL`. When investigating root cause, the question "was claude_code the provider, or did it fall back to minimax?" could not be answered from the database — the event payloads for `codegen.generate_patch` were `{}` empty. The `codegen.provider_chain` logger writes to backend stdout but stdout is not persisted.

This is one specific symptom of a broader instrumentation gap. Without it, T-FAILURE-DIAGNOSIS can't reliably distinguish "claude_code refused to modify the file (good — file was healthy)" from "minimax wrote bad output (bad — provider quality issue)". Future provider-swap decisions also need this data.

### Dependencies

- None. Pure instrumentation. Doesn't change provider logic.

## Design

### A. Event payload extension

When `CodegenService.generate_patch()` or `CodegenService.repair_patch()` returns or raises, the calling tool (`codegen.generate_patch` / `codegen.repair` in `apps/backend/app/tools/gateway.py`) records the event. Today the payload is empty. Extend to:

```python
{
    "provider_used": "claude_code",         # the provider that produced the result
    "provider_chain_attempted": ["claude_code"],  # ordered list of providers tried before success
    "fallback_count": 0,                    # = len(provider_chain_attempted) - 1
    "duration_ms": 12345,                   # end-to-end including fallbacks
    "files_modified": ["src/foo.js"],       # for success; empty list for failure
    "result_kind": "success" | "failure",   # success = produced non-empty diff; failure = exception
    "error_class": "CodegenError" | null,   # only set when result_kind == failure
    "model_name": "claude-opus-4-7" | null  # if provider exposes model id (claude_code: from CLI; anthropic: from settings)
}
```

For successful runs with no fallback: `provider_chain_attempted = ["claude_code"]`, `fallback_count = 0`.
For runs where claude_code timed out and codex succeeded: `provider_chain_attempted = ["claude_code", "codex"]`, `fallback_count = 1`, `provider_used = "codex"`.

### B. Plumbing

`CodegenService.generate_patch()` already returns `CodegenResult` (in `apps/backend/app/services/codegen.py`). Extend `CodegenResult`:

```python
@dataclass
class CodegenResult:
    diff: str
    files_changed: list[str]
    # NEW:
    provider_used: str
    provider_chain_attempted: list[str]
    fallback_count: int
    duration_ms: int
    model_name: str | None
```

For exceptions, `CodegenError` gains an attribute `provider_chain_attempted` so the caller can record it on `result_kind="failure"`.

The provider-chain loop at `_run_provider_chain` records each attempt's start/end timestamps, gathers per-attempt errors into a list, and at the end populates the new fields.

### C. Tool gateway recording

`apps/backend/app/tools/gateway.py::_execute_codegen_generate_patch` and the `codegen.repair` equivalent currently emit a `TOOL_SUCCEEDED` / `TOOL_FAILED` event with empty payload. Update to include the full payload from CodegenResult / CodegenError.

### D. Backward compatibility

Existing event payloads in `event` table are sparse. Don't migrate existing rows. New rows after deploy carry the new fields. The `failure_diagnosis` / future analytics tools must handle both shapes (older rows treated as `provider_used="unknown"`).

### E. Configuration

No new settings. Provider observability is always on — instrumentation is free.

## Files to create

1. `apps/backend/tests/services/test_codegen_provider_observability.py` — unit tests for `CodegenResult` extension + chain attempt recording.

## Files to edit

1. `apps/backend/app/services/codegen.py`:
   - Extend `CodegenResult` dataclass
   - Extend `CodegenError` to carry `provider_chain_attempted` attribute
   - Modify provider-chain loop to record start/end timestamps per attempt
   - Each `_call_*` method returns `CodegenResult` with `provider_used` + `model_name` populated
2. `apps/backend/app/tools/gateway.py`:
   - `_execute_codegen_generate_patch`: extract observability fields from CodegenResult and include in event payload
   - Same for `codegen.repair` handler
3. `apps/backend/app/services/runtime_validation.py`: if it calls codegen for repair, propagate the same observability.
4. `apps/backend/app/orchestrator/service.py::_run_compile_repair_loop`: when emitting `compile_repair.round_completed` events, include aggregate observability (which provider produced each round's repaired files).

## Tests

### Unit tests (test_codegen_provider_observability.py)

1. `test_codegenresult_has_provider_fields` — create `CodegenResult(diff="...", files_changed=[], provider_used="claude_code", provider_chain_attempted=["claude_code"], fallback_count=0, duration_ms=100, model_name="claude-opus-4-7")` — assert fields readable.
2. `test_call_claude_code_populates_provider_used` — mock `_call_claude_code_worktree` to return raw diff; wrap in result and check `result.provider_used == "claude_code"`.
3. `test_provider_chain_records_fallback` — mock claude_code to raise CodegenError; codex to succeed. Check final result has `provider_used == "codex"`, `provider_chain_attempted == ["claude_code", "codex"]`, `fallback_count == 1`.
4. `test_codegenerror_carries_chain_history` — mock all providers to fail. Catch CodegenError, assert `exc.provider_chain_attempted == ["claude_code", "codex", ...]`.
5. `test_duration_ms_includes_fallbacks` — mock claude_code to take 5s and fail, codex to take 2s and succeed. Final `duration_ms` ≥ 7000.
6. `test_model_name_from_claude_code_cli_output` — claude_code CLI stdout includes a model marker; result.model_name = that value. If CLI doesn't expose model, model_name = None (not "unknown").
7. `test_gateway_event_payload_contains_observability_fields` — call `_execute_codegen_generate_patch` with a mock service. Inspect emitted event payload, assert all 8 observability fields present.
8. `test_gateway_failure_event_payload_includes_error_class` — service raises CodegenError. Event payload has `result_kind="failure"`, `error_class="CodegenError"`, `provider_chain_attempted` populated.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 8 new tests pass.
- Full suite still green.
- Manual verification: trigger a develop pipeline. After it runs, query `event` table for `tool_name = 'codegen.generate_patch'` and confirm payload has `provider_used`, `provider_chain_attempted`, `fallback_count`, `duration_ms`, `files_modified`, `result_kind`, `error_class`, `model_name`.
- The query in T-FAILURE-DIAGNOSIS spec section B can now answer "which provider ran each codegen batch" without backend log grepping.
- **Operational signal**: backend WARN-level log line whenever `provider_used in ("anthropic", "minimax", "openai", "deepseek")` — these are API-based fallbacks the user is trying to avoid; if they fire, the user wants to know immediately. Format: `provider_fallback_to_api: provider={X} task={tid} reason="claude_code+codex both failed"`.

## Out of scope (explicitly NOT in this card)

- Changing provider chain order or default provider. That's a separate decision gated on the benchmark numbers.
- Adding token-count / cost tracking to events. That belongs in a `T-LLM-COST-TRACKING` ticket.
- Frontend display of provider info. The data being in the DB is enough; UI surfacing comes when it's needed.
- Migrating historical event rows. Old rows stay sparse.

## Workflow (for the executor)

<!-- Effort: low -->

1. Read current `CodegenResult` + `CodegenError` definitions in `apps/backend/app/services/codegen.py`.
2. Read `_run_provider_chain` (or the equivalent loop in `generate_patch`) to find the per-attempt try/except.
3. Extend `CodegenResult` dataclass + `CodegenError` exception class.
4. Wrap each provider call to record start/end timestamps; collect into `provider_chain_attempted`.
5. Update each `_call_*` method to return `CodegenResult` with populated fields.
6. Update `apps/backend/app/tools/gateway.py` event emission.
7. Update `apps/backend/app/orchestrator/service.py` repair-loop event payloads (aggregate observability per round).
8. Write all 8 tests with mocked providers.
9. Run `python -m compileall app` + unit suite.
10. Manually trigger one develop task, verify event payloads in DB.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-CODEGEN-PROVIDER-OBSERVABILITY.md
```
