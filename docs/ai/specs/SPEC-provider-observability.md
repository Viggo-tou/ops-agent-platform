# SPEC: Surface codegen provider + fallback history in event payload

## Context (read this, don't re-explore the repo)

- `apps/backend/app/services/codegen.py:92-128` — `CodegenService.generate_patch` iterates `providers = self._resolve_provider_chain()`, tries each via `_try_provider`, returns first success. Each `_try_provider` return path (anthropic/claude_code/codex/minimax/ollama/mock — lines 283, 536, 833, 1113, 1252, 1299, 1353) already sets `CodegenResult.provider_name`.
- `apps/backend/app/tools/gateway.py:642` is the sole call site: `CodeGenerator(self.settings).generate_patch(...)`. Result is returned as `result.model_dump(mode="json")`.
- `apps/backend/app/orchestrator/service.py` wraps that via `_execute_develop_tool` which fires `TOOL_SUCCEEDED` with `payload=result` — so whatever fields we add to `CodegenResult` automatically land in the event payload and reach the frontend.

## Goal

Make the codegen event stream show **which provider actually ran** AND **which providers failed first** (fallback chain), without plumbing a callback through three layers.

## The change

**Edit 1: `apps/backend/app/services/codegen.py`**

Add an `attempt_history: list[dict] | None` field to the `CodegenResult` dataclass (find its definition at the top of the file; it's a Pydantic model or dataclass already carrying `provider_name`, `model_name`, `files_changed`, etc.). Default `None` or `[]`.

In `generate_patch` (line ~92-128), track attempts:

```python
def generate_patch(self, *, task_id, plan_json, context_files, task_description="", source_repo_path=None) -> CodegenResult:
    providers = self._resolve_provider_chain()
    import logging as _log
    _logger = _log.getLogger("codegen.provider_chain")
    _logger.info("Provider chain: %s", providers)

    attempts: list[dict] = []
    for provider_idx, provider in enumerate(providers):
        _logger.info("Trying provider %d/%d: %s", provider_idx + 1, len(providers), provider)
        try:
            result = self._try_provider(
                provider=provider,
                task_id=task_id,
                plan_json=plan_json,
                context_files=context_files,
                task_description=task_description,
                source_repo_path=source_repo_path,
            )
            _logger.info("Provider %s succeeded: %d files changed", provider, len(result.files_changed))
            attempts.append({"provider": provider, "status": "succeeded"})
            # Attach history to the result before returning.
            # Use setattr if CodegenResult is a frozen model; otherwise direct assignment.
            try:
                result.attempt_history = attempts
            except Exception:
                # If model is immutable, construct a copy.
                result = result.model_copy(update={"attempt_history": attempts})
            return result
        except CodegenError as exc:
            _logger.warning("Provider %s failed: %s", provider, str(exc)[:300])
            attempts.append({
                "provider": provider,
                "status": "failed",
                "error": str(exc)[:300],
            })
            if _is_provider_level_error(exc) and provider_idx < len(providers) - 1:
                _logger.info("Classified as provider-level error — trying next provider")
                continue
            raise

    raise CodegenError("No codegen provider available.")
```

**That is literally the only code change.** No changes to `_try_provider`, gateway, or orchestrator — the existing event machinery picks up `attempt_history` automatically through `model_dump(mode="json")`.

**Edit 2: tests — `apps/backend/tests/services/test_codegen_provider_events.py`**

Create this file. Two tests:

1. `test_attempt_history_on_single_provider_success`:
   - Configure `CodegenService` with `codegen_provider="mock"`.
   - Call `generate_patch(task_id="t1", plan_json={"objective":"x", "steps":[]}, context_files={})`.
   - Assert `result.attempt_history == [{"provider": "mock", "status": "succeeded"}]`.

2. `test_attempt_history_records_failed_providers_then_success`:
   - Monkey-patch `CodegenService._try_provider` so that:
     - First call (provider="codex") raises `CodegenError("503 service unavailable")`.
     - Second call (provider="mock") returns a valid `CodegenResult` with `provider_name="mock"`.
   - Configure the chain to be `["codex", "mock"]` (either via settings or by monkey-patching `_resolve_provider_chain`).
   - Assert `result.attempt_history == [{"provider":"codex","status":"failed","error":"503 service unavailable"}, {"provider":"mock","status":"succeeded"}]`.
   - Ensure `_is_provider_level_error` classifies "503" as retryable — if it doesn't, use an error string it does recognize (check the function definition).

Do NOT add an orchestrator-level integration test.

## How to run

```
cd D:/项目/ops-worktrees/provider-observability/apps/backend
pytest tests/services/test_codegen_provider_events.py -q
pytest tests/ -q
```

Both must pass. If any pre-existing test fails unrelated to this change, STOP and describe the failure in your final report — do NOT "fix" it.

## Hard constraints

- **Do NOT read** `AGENTS.md`, `CURRENT_STATE.md`, `DECISIONS.md`, `PROJECT_CONTEXT.md`, `TASK_QUEUE.md`, `SESSION_HANDOFF.md`, `CLAUDE.md`. This spec has all the context you need.
- **Do NOT create a session-start tag.** That ritual is for the human session owner, not this codex dispatch.
- **Do NOT run `git status` or explore the repo structure.** Spec gives you file paths.
- **Do NOT modify `_try_provider`, gateway.py, or orchestrator/service.py.** Only codegen.py + the new test file.
- **Do NOT add new dependencies or new imports beyond what's already in these files.**

## Success criteria (copy verbatim into final report)

- [ ] `CodegenResult.attempt_history` field added to the dataclass/model.
- [ ] `generate_patch` populates `attempt_history` on both success and fallback-through-failure paths.
- [ ] `test_codegen_provider_events.py` has 2 passing tests.
- [ ] `pytest tests/ -q` full suite shows no new failures (report counts).
- [ ] Diff size: `git diff --stat` shows only `codegen.py` + 1 new test file modified.
