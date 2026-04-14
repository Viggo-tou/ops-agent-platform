# T-Q5 — Codegen Retry Loop & Stronger MiniMax Prompt

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

Make MiniMax codegen reliable enough for demos by: (1) adding automatic retry on invalid diff output, and (2) improving the system prompt with stricter formatting rules and a concrete few-shot example of valid unified diff output.

## Background

MiniMax-M2.7-highspeed can produce valid unified diffs — it succeeded in 2 out of 4 attempts during testing. The failures are because the LLM sometimes returns explanatory text, markdown, or malformed diff headers instead of pure unified diff.

Two fixes:
1. **Retry loop**: If `_parse_response()` raises `CodegenError` (invalid diff), retry up to 2 more times (3 total attempts). Each retry appends a correction hint to the prompt.
2. **Better prompt**: Add a concrete example of the expected output format and explicitly forbid common failure modes.

## Design

### 1. Retry in `generate_patch()`

In `app/services/codegen.py`, wrap the LLM call in a retry loop:

```python
def generate_patch(self, *, task_id, plan_json, context_files, task_description="") -> CodegenResult:
    del task_id
    prompt = self._build_prompt(plan_json, context_files, task_description)
    provider = self._resolve_provider()

    if provider == "mock":
        return self._mock_generate(plan_json, context_files)

    max_attempts = 3
    last_error = None
    for attempt in range(max_attempts):
        try:
            call_prompt = prompt
            if attempt > 0:
                call_prompt += f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\nYou MUST output ONLY a valid unified diff. No text before or after. Start with 'diff --git'."
            
            if provider == "anthropic":
                return self._call_anthropic(call_prompt)
            if provider == "minimax":
                return self._call_minimax(call_prompt)
            if provider == "openai":
                return self._call_openai(call_prompt)
            raise CodegenError(f"Unknown provider: {provider}")
        except CodegenError as exc:
            if "valid unified diff" in str(exc) or "changed file headers" in str(exc):
                last_error = str(exc)
                continue  # retry
            raise  # non-retryable error (e.g., API connection failure)
    
    raise CodegenError(f"Failed to generate valid diff after {max_attempts} attempts. Last error: {last_error}")
```

### 2. Enhanced system prompt

Replace `CODEGEN_SYSTEM_PROMPT` with a stronger version that includes a concrete example:

```python
CODEGEN_SYSTEM_PROMPT = """You are a code generation agent. Given a task plan and source file contents, produce a unified diff.

CRITICAL RULES:
1. Output ONLY a valid unified diff. Nothing else. No explanations, no markdown fences, no commentary.
2. The very first line of your output MUST be "diff --git a/path b/path".
3. Use standard unified diff format with --- a/path, +++ b/path, and @@ hunk headers.
4. Only modify files mentioned in the plan.
5. Make minimal, focused changes.
6. For new files, use --- /dev/null.
7. Include 3 context lines around each change.

EXAMPLE OUTPUT FORMAT (your response must look exactly like this):
diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -10,7 +10,7 @@
 import os
 
 def greet(name):
-    return "Hello " + name
+    return f"Hello, {name}!"
 
 def main():
     print(greet("World"))

DO NOT output anything before "diff --git". DO NOT wrap in markdown code fences. DO NOT add explanations."""
```

### 3. Enhanced `_parse_response()`

Make the parser more lenient — try to extract valid diff content even if the LLM prefixed it with some text:

```python
def _parse_response(self, content, *, provider_name, model_name, input_tokens, output_tokens):
    diff = content.strip()
    
    # Strip markdown code fences
    if diff.startswith("```"):
        diff = re.sub(r"^```(?:diff|patch)?\s*", "", diff)
        diff = re.sub(r"\s*```$", "", diff).strip()
    
    # NEW: Try to find diff start if LLM added preamble text
    if not diff.startswith("diff --git") and not diff.startswith("---"):
        match = re.search(r"(diff --git .+)", diff, re.DOTALL)
        if match:
            diff = match.group(1)
    
    if not diff.startswith("diff --git") and not diff.startswith("---"):
        raise CodegenError("LLM response does not contain a valid unified diff.")
    
    # ... rest unchanged
```

## Files to edit

1. `apps/backend/app/services/codegen.py` — retry loop in `generate_patch()`, new `CODEGEN_SYSTEM_PROMPT`, lenient `_parse_response()`.

## Tests

Add to `apps/backend/tests/services/test_codegen.py`:

1. **`test_retry_on_invalid_diff`** — Mock the LLM call to return plain text on first call, valid diff on second. Assert `generate_patch()` succeeds with the second attempt's result.
2. **`test_retry_exhausted`** — Mock the LLM call to always return plain text. Assert `CodegenError` with "after 3 attempts".
3. **`test_parse_response_extracts_diff_from_preamble`** — Pass content like `"Here is the diff:\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"`. Assert the diff is extracted correctly.
4. **`test_api_error_not_retried`** — Mock the LLM call to raise `CodegenError("API error")` (no "valid unified diff" in message). Assert it raises immediately without retry.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass.
- Full suite still green.
- Invalid diff triggers up to 2 retries with correction hint.
- API/connection errors are NOT retried.
- Parser can extract diff even with preamble text.
- System prompt includes concrete example format.

## Workflow (for the executor)

<!-- Effort: medium — modify existing codegen with retry + prompt changes -->

1. Read `app/services/codegen.py` — focus on `generate_patch()`, `CODEGEN_SYSTEM_PROMPT`, `_parse_response()`.
2. Add retry loop to `generate_patch()`.
3. Replace `CODEGEN_SYSTEM_PROMPT`.
4. Make `_parse_response()` more lenient.
5. Add tests.
6. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q5-codegen-retry-and-prompt.md
```
