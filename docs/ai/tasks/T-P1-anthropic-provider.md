# T-P1 — Add Anthropic/Claude as LLM Provider

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

Add `anthropic` as a fourth LLM provider option for both code generation (`CodeGenerator`) and task planning (`PrimaryAgentPlanner`). This enables Claude models to serve as the codegen and planning backbone when configured.

## Background

Phase P of the multi-agent MVP roadmap. The platform currently supports three providers: `mock`, `openai`, and `minimax`. The user has MiniMax configured for semantic translation but needs a stronger code generation provider. Claude excels at producing structured code output (unified diffs) and structured JSON planning. Adding Anthropic as a provider lets the system use Claude for codegen and planning while MiniMax handles translation.

Existing patterns:
- `app/services/codegen.py` — `CodeGenerator` with `_call_minimax()` and `_call_openai()` methods using `httpx`.
- `app/agents/service.py` — `PrimaryAgentPlanner` with `_generate_plan_with_openai()` and `_generate_plan_with_minimax()` using `httpx`.
- `app/core/config.py` — `Settings` with `primary_agent_provider: Literal["auto", "mock", "openai", "minimax"]`.
- All LLM calls use raw `httpx` POST requests — no provider SDKs. Keep this pattern.

The Anthropic Messages API endpoint is `https://api.anthropic.com/v1/messages`. Auth header is `x-api-key` (not Bearer). Required headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`. Request body uses `model`, `max_tokens`, `system` (string), `messages` (list of `{"role": "user", "content": "..."}`) fields.

Response shape:
```json
{
  "content": [{"type": "text", "text": "..."}],
  "usage": {"input_tokens": 123, "output_tokens": 456}
}
```

## Design

### 1. Config changes (`app/core/config.py`)

Add three new settings and expand the provider literal:

```python
primary_agent_provider: Literal["auto", "mock", "openai", "minimax", "anthropic"] = "auto"
anthropic_api_key: str | None = None
anthropic_base_url: str = "https://api.anthropic.com"
anthropic_model: str = "claude-sonnet-4-20250514"
```

### 2. Auto-resolution order

In `auto` mode, the priority for **codegen** is: `anthropic > openai > minimax > mock`.
In `auto` mode, the priority for **planning** is: `anthropic > openai > minimax > mock`.

Rationale: Claude and OpenAI produce better structured output (diffs, JSON plans) than MiniMax. MiniMax stays as a fallback.

### 3. CodeGenerator changes (`app/services/codegen.py`)

Add `_call_anthropic()` method:

```python
def _call_anthropic(self, prompt: str) -> CodegenResult:
    """Call Anthropic Messages API for code generation."""
    if not self.settings.anthropic_api_key:
        raise CodegenError("OPS_AGENT_ANTHROPIC_API_KEY is not configured.")

    model_name = self.settings.anthropic_model
    url = f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": self.settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model_name,
        "max_tokens": 8192,
        "system": CODEGEN_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=120)
        response.raise_for_status()
        data = response.json()
        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
        usage = data.get("usage", {})
        return self._parse_response(
            content,
            provider_name="anthropic",
            model_name=model_name,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
    except httpx.HTTPError as exc:
        raise CodegenError(f"Anthropic API error: {exc}") from exc
```

Update `_resolve_provider()`:

```python
def _resolve_provider(self) -> str:
    provider = self.settings.primary_agent_provider
    if provider != "auto":
        return provider
    if self.settings.anthropic_api_key:
        return "anthropic"
    if self.settings.openai_api_key:
        return "openai"
    if self.settings.minimax_api_key:
        return "minimax"
    return "mock"
```

Update `generate_patch()` to add the anthropic branch:

```python
if provider == "anthropic":
    return self._call_anthropic(prompt)
```

### 4. PrimaryAgentPlanner changes (`app/agents/service.py`)

Add `_generate_plan_with_anthropic()` method that calls the Anthropic Messages API with the planning system prompt and expects a JSON response matching `GeneratedPlanPayload`.

The method should:
- Use the same `_build_planning_instructions()` as system prompt.
- Send the same user message format as the OpenAI planner.
- Parse the JSON response and validate with `GeneratedPlanPayload.model_validate()`.
- Fall back to `default_payload` on parse errors (same pattern as OpenAI/MiniMax).

Add `should_try_anthropic` branch in `generate_plan()`:

```python
should_try_anthropic = provider_mode == "anthropic" or (
    provider_mode == "auto" and bool(self.settings.anthropic_api_key)
)
```

Place it BEFORE the `should_try_openai` block. Same try/except pattern with fallback to mock on failure.

### 5. .env additions

Add to `apps/backend/.env` (after the OpenAI section):

```
# Anthropic:
# provider for codegen and planning when primary_agent_provider=auto or anthropic
OPS_AGENT_ANTHROPIC_API_KEY=
OPS_AGENT_ANTHROPIC_BASE_URL=https://api.anthropic.com
OPS_AGENT_ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

## Files to create

None.

## Files to edit

1. `apps/backend/app/core/config.py` — add `anthropic_api_key`, `anthropic_base_url`, `anthropic_model` fields; expand `primary_agent_provider` Literal to include `"anthropic"`.
2. `apps/backend/app/services/codegen.py` — add `_call_anthropic()`, update `_resolve_provider()` and `generate_patch()`.
3. `apps/backend/app/agents/service.py` — add `_generate_plan_with_anthropic()`, add `should_try_anthropic` block in `generate_plan()`.
4. `apps/backend/.env` — add Anthropic config vars.

## Tests

Add to existing test files. Use `unittest.TestCase`. All tests use mock — no network.

In `apps/backend/tests/services/test_codegen.py`, add:

1. **`test_resolve_provider_anthropic_auto`** — Set `anthropic_api_key="sk-test"`, `openai_api_key=None`, `minimax_api_key=None`, `primary_agent_provider="auto"`. Assert `_resolve_provider()` returns `"anthropic"`.
2. **`test_resolve_provider_anthropic_explicit`** — Set `primary_agent_provider="anthropic"`. Assert `_resolve_provider()` returns `"anthropic"`.
3. **`test_resolve_provider_auto_prefers_anthropic_over_minimax`** — Set both `anthropic_api_key` and `minimax_api_key`. Assert auto resolves to `"anthropic"`.
4. **`test_call_anthropic_no_key_raises`** — Set `anthropic_api_key=None`, `primary_agent_provider="anthropic"`. Assert calling `generate_patch()` raises `CodegenError` with "not configured".

In `apps/backend/tests/agents/test_anthropic_planner.py` (new file), add:

5. **`test_generate_plan_anthropic_auto`** — Mock httpx.post to return a valid JSON plan response shaped like Anthropic Messages API. Set `anthropic_api_key="sk-test"`, `primary_agent_provider="auto"`. Assert `generate_plan()` returns provider_name `"anthropic"`.
6. **`test_generate_plan_anthropic_fallback_on_error`** — Mock httpx.post to raise an error. Assert fallback to mock plan with `used_fallback=True`.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 6 new tests pass.
- Full suite still green.
- `primary_agent_provider` accepts `"anthropic"` as a valid value.
- In `auto` mode with only `anthropic_api_key` set, both codegen and planner use Anthropic.
- In `auto` mode with both `anthropic_api_key` and `minimax_api_key` set, codegen and planner prefer Anthropic.
- Anthropic calls use `x-api-key` header (not Bearer), include `anthropic-version: 2023-06-01`.
- `.env` has Anthropic config vars (key empty by default).

## Workflow (for the executor)

<!-- Effort: medium — provider wiring following established patterns -->

1. Read `app/core/config.py`, `app/services/codegen.py` (full), `app/agents/service.py` (focus on `generate_plan`, `_generate_plan_with_openai`, `_generate_plan_with_minimax`, `_build_planning_instructions`), `apps/backend/.env`.
2. Edit `config.py` — add 3 fields, expand Literal.
3. Edit `codegen.py` — add `_call_anthropic()`, update `_resolve_provider()`, add branch in `generate_patch()`.
4. Edit `service.py` — add `_generate_plan_with_anthropic()`, add `should_try_anthropic` in `generate_plan()`.
5. Edit `.env` — add Anthropic vars.
6. Add tests to `tests/services/test_codegen.py`.
7. Create `tests/agents/test_anthropic_planner.py`.
8. Run `python -m compileall app && python -m unittest tests.services.test_codegen -v && python -m unittest tests.agents.test_anthropic_planner -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-P1-anthropic-provider.md
```
