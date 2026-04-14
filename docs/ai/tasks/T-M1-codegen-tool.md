# T-M1 — Code Generation Tool (`codegen.generate_patch`)

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: xhigh -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add a `CodeGenerator` service and `codegen.generate_patch` tool that takes a plan document + file context and calls the configured LLM provider to produce a unified diff. This is the keystone for the end-to-end pipeline: it turns "what to do" (plan) into "actual code changes" (diff).

## Background

Phase M of the multi-agent MVP roadmap. The orchestrator can plan (Phase A), sandbox can apply patches (Phase C), tests can run (Phase D), diffs can be reviewed (Phase E), approvals are gated (Phase F), and rollback works (Phase G). The missing piece is **generating the actual code changes from a plan**.

The existing LLM call pattern is in `app/agents/service.py` — `PrimaryAgentPlanner.generate_plan()` calls MiniMax or OpenAI with structured prompts and parses the response. `CodeGenerator` follows the same pattern.

Config: `app/core/config.py` has `primary_agent_provider`, `minimax_api_key`, `minimax_base_url`, `openai_api_key`. The codegen service reuses these settings.

## Design

### 1. CodegenResult dataclass

Add to `apps/backend/app/agents/schemas.py`:

```python
class CodegenResult(BaseModel):
    diff: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    files_changed: list[str] = Field(default_factory=list)
    provider_name: str = ""
    model_name: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    used_fallback: bool = False
    fallback_reason: str | None = None
```

### 2. CodeGenerator service

New file: `apps/backend/app/services/codegen.py`

```python
class CodegenError(Exception):
    pass

class CodeGenerator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def generate_patch(
        self,
        *,
        task_id: str,
        plan_json: dict,
        context_files: dict[str, str],  # filename -> content
        task_description: str = "",
    ) -> CodegenResult:
        """Generate a unified diff from a plan and file context."""
        prompt = self._build_prompt(plan_json, context_files, task_description)
        
        provider = self.settings.primary_agent_provider
        if provider == "auto":
            provider = "minimax" if self.settings.minimax_api_key else "openai" if self.settings.openai_api_key else "mock"

        if provider == "mock":
            return self._mock_generate(plan_json, context_files)
        elif provider == "minimax":
            return self._call_minimax(prompt)
        elif provider == "openai":
            return self._call_openai(prompt)
        else:
            raise CodegenError(f"Unknown provider: {provider}")

    def _build_prompt(self, plan_json: dict, context_files: dict[str, str], task_description: str) -> str:
        """Build the LLM prompt for code generation."""
        # System: you are a code generator that produces unified diffs
        # Include: plan objective, steps, affected locations
        # Include: file contents as context
        # Strict: output ONLY valid unified diff, no explanation
        ...

    def _mock_generate(self, plan_json: dict, context_files: dict[str, str]) -> CodegenResult:
        """Deterministic mock for testing — produces a minimal valid diff from the first context file."""
        if not context_files:
            raise CodegenError("No context files provided for code generation.")
        first_file = next(iter(context_files))
        # Generate a trivial diff that adds a comment
        diff = (
            f"diff --git a/{first_file} b/{first_file}\n"
            f"--- a/{first_file}\n"
            f"+++ b/{first_file}\n"
            f"@@ -1,1 +1,2 @@\n"
            f" {context_files[first_file].splitlines()[0] if context_files[first_file].strip() else ''}\n"
            f"+# Generated change for task\n"
        )
        return CodegenResult(
            diff=diff,
            summary=f"Mock patch: added comment to {first_file}",
            files_changed=[first_file],
            provider_name="mock",
            model_name="mock",
        )

    def _call_minimax(self, prompt: str) -> CodegenResult:
        """Call MiniMax API for code generation."""
        url = f"{self.settings.minimax_base_url}/v1/text/chatcompletion_v2"
        headers = {"Authorization": f"Bearer {self.settings.minimax_api_key}", "Content-Type": "application/json"}
        body = {
            "model": "MiniMax-Text-01",
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=self.settings.minimax_planner_timeout_seconds)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return self._parse_response(content, provider_name="minimax", model_name="MiniMax-Text-01",
                                         input_tokens=usage.get("prompt_tokens", 0),
                                         output_tokens=usage.get("completion_tokens", 0))
        except httpx.HTTPError as exc:
            raise CodegenError(f"MiniMax API error: {exc}") from exc

    def _call_openai(self, prompt: str) -> CodegenResult:
        """Call OpenAI API for code generation."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"}
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=90)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return self._parse_response(content, provider_name="openai", model_name="gpt-4o",
                                         input_tokens=usage.get("prompt_tokens", 0),
                                         output_tokens=usage.get("completion_tokens", 0))
        except httpx.HTTPError as exc:
            raise CodegenError(f"OpenAI API error: {exc}") from exc

    def _parse_response(self, content: str, *, provider_name: str, model_name: str,
                        input_tokens: int, output_tokens: int) -> CodegenResult:
        """Extract unified diff from LLM response. Handle code fences."""
        # Strip markdown code fences if present
        diff = content.strip()
        if diff.startswith("```"):
            lines = diff.split("\n")
            # Remove first line (```diff or ```) and last line (```)
            lines = [l for i, l in enumerate(lines) if not (i == 0 or (i == len(lines)-1 and l.strip() == "```"))]
            diff = "\n".join(lines)

        if not diff.startswith("diff --git") and not diff.startswith("---"):
            raise CodegenError("LLM response does not contain a valid unified diff.")

        files_changed = DiffReviewer.parse_changed_files(diff)
        summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"

        return CodegenResult(
            diff=diff,
            summary=summary,
            files_changed=files_changed,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
```

### 3. System prompt

```python
CODEGEN_SYSTEM_PROMPT = """You are a code generation agent. Given a task plan and source file contents, produce a unified diff that implements the plan.

Rules:
1. Output ONLY a valid unified diff. No explanations, no markdown, no commentary.
2. Use standard unified diff format: `diff --git a/path b/path`, `--- a/path`, `+++ b/path`, `@@ ... @@` hunks.
3. Only modify files mentioned in the plan's affected_code_locations.
4. Make minimal, focused changes. Do not refactor unrelated code.
5. Preserve existing code style (indentation, naming conventions).
6. If the plan mentions creating a new file, use `--- /dev/null` as the source.
7. Include enough context lines (3) around each change for `git apply` to work."""
```

### 4. Tool registration

Register `codegen.generate_patch` in `app/tools/registry.py` as `APPROVAL_REQUIRED`. Tags: `("codegen", "llm", "code-change")`.

Gateway executor in `app/tools/gateway.py`:
- Required payload: `plan_json: dict`, `context_files: dict[str, str]`
- Optional: `task_description: str`
- Returns `CodegenResult` as dict

### 5. Cost tracking integration

After a successful codegen call, record LlmUsage (if cost_tracking is available):
```python
from app.services.cost_tracking import CostTracker
tracker = CostTracker(self.db)
tracker.record_usage(task_id=task_id, actor_name=..., provider_name=result.provider_name,
                     model_name=result.model_name or "", input_tokens=result.input_tokens,
                     output_tokens=result.output_tokens, purpose="codegen")
```

### 6. Governance seed

Add policy rule in `app/services/governance.py`:
- `codegen.generate_patch.*.require_approval.v1` — all roles require approval for code generation.

## Files to create

1. `apps/backend/app/services/codegen.py`
2. `apps/backend/tests/services/test_codegen.py`

## Files to edit

3. `apps/backend/app/agents/schemas.py` — add `CodegenResult`.
4. `apps/backend/app/tools/registry.py` — add tool definition.
5. `apps/backend/app/tools/gateway.py` — add dispatcher + executor.
6. `apps/backend/app/services/governance.py` — add policy rule.

## Tests

All in `apps/backend/tests/services/test_codegen.py`. Use `unittest.TestCase`. All tests use mock provider (no network).

1. **`test_mock_generate_produces_valid_diff`** — Call with mock provider, one context file. Assert diff starts with `diff --git`, `CodegenResult.files_changed` is correct.
2. **`test_mock_generate_no_context_files_raises`** — Empty `context_files`. Assert `CodegenError`.
3. **`test_parse_response_valid_diff`** — Call `_parse_response` with a clean unified diff string. Assert correct extraction.
4. **`test_parse_response_with_code_fences`** — Wrap diff in ````diff\n...\n``` ` fences. Assert fences stripped, diff extracted.
5. **`test_parse_response_invalid_content`** — Pass plain English text. Assert `CodegenError`.
6. **`test_build_prompt_includes_plan_and_files`** — Call `_build_prompt` with a plan dict and context files. Assert prompt string contains the objective, file names, and file contents.
7. **`test_codegen_result_schema`** — Create `CodegenResult` with valid fields. Assert Pydantic validation passes.
8. **`test_tool_registered`** — Assert `codegen.generate_patch` exists in `ToolRegistry` with `APPROVAL_REQUIRED`.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 8 new tests pass.
- Full suite still green.
- `codegen.generate_patch` in tool registry as `APPROVAL_REQUIRED`.
- Mock provider produces a valid unified diff that `DiffReviewer.parse_changed_files()` can parse.
- `_parse_response` handles both raw diff and markdown-fenced diff.

## Workflow (for the executor)

<!-- Effort: xhigh — new service with LLM integration, prompt engineering, response parsing -->

1. Read `app/agents/service.py` (LLM call pattern), `app/agents/schemas.py`, `app/core/config.py`, `app/tools/registry.py`, `app/tools/gateway.py`, `app/services/governance.py`, `app/services/reviewer.py` (for `parse_changed_files`).
2. Add `CodegenResult` to schemas.
3. Create `app/services/codegen.py` with full implementation.
4. Wire tool in registry, gateway, governance.
5. Create tests.
6. Run `python -m compileall app && python -m unittest tests.services.test_codegen -v && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-M1-codegen-tool.md
```
