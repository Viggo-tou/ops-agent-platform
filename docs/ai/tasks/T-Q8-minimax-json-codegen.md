# T-Q8 — MiniMax Codegen: JSON File Output + difflib Diff Generation

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

Change `_call_minimax()` in `CodeGenerator` to use a different output format: instead of asking MiniMax to produce a unified diff (which it does unreliably), ask it to return JSON with the modified file contents. Then use Python's `difflib.unified_diff()` to generate the diff deterministically. Other providers (anthropic, openai) keep the existing unified diff output format.

## Background

After 7 pipeline attempts, MiniMax-M2.7-highspeed shows a consistent pattern:
- It understands the code and knows WHAT to change
- But it cannot reliably produce valid unified diff format (outputs context diffs, wrong hunk headers, corrupt patches)
- Other providers (Anthropic, OpenAI) handle unified diff format well

Solution: ask MiniMax to output JSON like `{"files": [{"path": "a/b.kt", "content": "full modified content"}]}`, then generate the diff ourselves using `difflib`.

## Design

### 1. New system prompt for MiniMax

```python
CODEGEN_SYSTEM_PROMPT_JSON_MODE = """You are a code generation agent. Given a task plan and source file contents, produce the MODIFIED versions of the files.

CRITICAL RULES:
1. Output ONLY valid JSON. Nothing else. No markdown fences, no explanations.
2. Use this exact JSON structure:
{
  "files": [
    {
      "path": "relative/path/to/file.ext",
      "content": "full modified file content here",
      "summary": "one-line description of what changed"
    }
  ]
}
3. The "content" field must contain the COMPLETE file content after your modifications.
4. Only include files that you actually modified. Do not include unchanged files.
5. Make minimal, focused changes. Do not refactor unrelated code.
6. Preserve existing code style (indentation, naming conventions).

EXAMPLE:
Given a file app/greet.py with content:
def greet(name):
    return "Hello " + name

If the task is to use f-strings, output:
{"files":[{"path":"app/greet.py","content":"def greet(name):\\n    return f\\"Hello, {name}!\\"\\n","summary":"Use f-string for greeting"}]}"""
```

### 2. Modified `_call_minimax()` flow

```python
def _call_minimax(self, prompt: str, context_files: dict[str, str]) -> CodegenResult:
    # Use JSON mode prompt instead of diff prompt
    # Call MiniMax API with CODEGEN_SYSTEM_PROMPT_JSON_MODE
    # Parse JSON response
    # For each file in response:
    #   - Get original content from context_files
    #   - Generate unified diff using difflib.unified_diff()
    # Combine all diffs into one unified diff string
    # Return CodegenResult with the generated diff
```

### 3. Diff generation helper

```python
import difflib

def _generate_diff_from_files(
    original_files: dict[str, str],
    modified_files: list[dict],
) -> tuple[str, list[str]]:
    """Generate unified diff from original and modified file contents.
    
    Returns (diff_string, files_changed_list).
    """
    diff_parts = []
    files_changed = []
    for mod in modified_files:
        path = mod["path"]
        new_content = mod["content"]
        old_content = original_files.get(path, "")
        
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        
        diff_lines = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        ))
        
        if diff_lines:
            # Add git-style header
            diff_parts.append(f"diff --git a/{path} b/{path}")
            diff_parts.extend(diff_lines)
            files_changed.append(path)
    
    return "\n".join(diff_parts) + "\n", files_changed
```

### 4. JSON response parsing

```python
def _parse_json_codegen_response(self, content: str) -> list[dict]:
    """Parse MiniMax JSON codegen response. Handle markdown fences."""
    text = content.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    
    data = json.loads(text)
    files = data.get("files", [])
    if not files:
        raise CodegenError("MiniMax JSON response contains no files.")
    
    for f in files:
        if not isinstance(f.get("path"), str) or not f["path"].strip():
            raise CodegenError("MiniMax JSON response has file entry with missing path.")
        if not isinstance(f.get("content"), str):
            raise CodegenError(f"MiniMax JSON response has no content for {f['path']}.")
    
    return files
```

### 5. Method signature change

`_call_minimax` needs access to `context_files` (the original file contents) to compute the diff. Update `generate_patch()` to pass `context_files` to `_call_minimax`:

```python
if provider == "minimax":
    return self._call_minimax(call_prompt, context_files=context_files)
```

Other providers (`_call_anthropic`, `_call_openai`) keep the existing signature — they receive the prompt and return diff directly.

### 6. Retry compatibility

The existing retry loop checks for `"valid unified diff"` in the error message. With JSON mode, the error messages will be different ("no files", "missing path", JSON parse errors). Update the retry check:

```python
def _is_retryable_codegen_error(self, exc: CodegenError) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("valid unified diff", "changed file headers", "json", "no files", "missing path"))
```

## Files to edit

1. `apps/backend/app/services/codegen.py` — add `CODEGEN_SYSTEM_PROMPT_JSON_MODE`, modify `_call_minimax()` to use JSON mode, add `_generate_diff_from_files()` and `_parse_json_codegen_response()`, update `generate_patch()` to pass `context_files` to minimax, update retry check.

## Tests

Add to `apps/backend/tests/services/test_codegen.py`:

1. **`test_generate_diff_from_files`** — Provide original and modified file dicts. Assert the generated diff is valid unified diff format with correct `diff --git` headers.
2. **`test_generate_diff_no_changes`** — Provide identical original and modified content. Assert empty diff raises `CodegenError`.
3. **`test_parse_json_codegen_response_valid`** — Provide valid JSON with files array. Assert correct parsing.
4. **`test_parse_json_codegen_response_with_fences`** — Wrap JSON in ```json fences. Assert fences stripped and parsing works.
5. **`test_parse_json_codegen_response_empty_files`** — Provide `{"files": []}`. Assert `CodegenError`.
6. **`test_minimax_uses_json_mode_prompt`** — Mock httpx.post. Assert the request body contains `CODEGEN_SYSTEM_PROMPT_JSON_MODE` (not the diff prompt). Assert the response is processed through JSON parsing and difflib.

## Acceptance criteria

- `python -m compileall app` exits 0.
- All new tests pass.
- Full suite still green.
- MiniMax codegen uses JSON output mode with `CODEGEN_SYSTEM_PROMPT_JSON_MODE`.
- Anthropic and OpenAI codegen still use unified diff output mode with `CODEGEN_SYSTEM_PROMPT`.
- Generated diff from difflib is valid unified diff that `git apply` accepts.
- Retry loop works for both JSON parse errors and diff format errors.

## Workflow (for the executor)

<!-- Effort: medium — modify existing codegen with new MiniMax path -->

1. Read `app/services/codegen.py` — focus on `_call_minimax()`, `generate_patch()`, `CODEGEN_SYSTEM_PROMPT`, retry logic.
2. Add `CODEGEN_SYSTEM_PROMPT_JSON_MODE`.
3. Add `_generate_diff_from_files()` and `_parse_json_codegen_response()`.
4. Modify `_call_minimax()` to use JSON mode.
5. Update `generate_patch()` to pass context_files.
6. Update retry check.
7. Add tests.
8. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q8-minimax-json-codegen.md
```
