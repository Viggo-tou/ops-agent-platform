# T-Q7 — Read Full File Content for Codegen Context

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Fix `_gather_codegen_context()` in the orchestrator so it reads FULL file content instead of short RAG snippets. Currently the knowledge service returns only matched fragments (3-5 lines), which is useless for code generation — the LLM needs the complete file to produce a valid unified diff.

## Background

When the develop pipeline prepares context_files for codegen, it calls `_gather_codegen_context()` which tries:
1. Sandbox dir files (sandbox may not exist yet at this point)
2. Knowledge service search (returns RAG snippets, not full files)

The knowledge search returns snippets like `"package com.example.handyman.chatbox\n\nimport android.os.Bundle"` — just 3 lines. MiniMax can't produce a valid diff from 3 lines of context.

The knowledge source path is configured as `OPS_AGENT_KNOWLEDGE_SOURCE_PATH=D:\项目\HandymanApp-master`. The full files are right there on disk. The fix: read the file directly from the knowledge source path using `pathlib.Path`.

## Design

In `_gather_codegen_context()`, add a FIRST strategy before sandbox and knowledge search:

```python
def _gather_codegen_context(self, *, task, plan):
    context_files = {}
    
    # Strategy 1: Read directly from knowledge source path on disk
    source_path = self._resolve_knowledge_source_path()
    if source_path:
        for location in plan.affected_code_locations:
            relative_path = self._normalize_codegen_path(location.relative_path)
            if not relative_path or relative_path in context_files:
                continue
            full_path = source_path / relative_path
            if full_path.is_file():
                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                    # Truncate very large files to avoid token explosion
                    max_bytes = getattr(self.tool_gateway.settings, "knowledge_max_file_bytes", 120_000)
                    if len(content) <= max_bytes:
                        context_files[relative_path] = content
                    else:
                        context_files[relative_path] = content[:max_bytes] + "\n... (truncated)"
                except Exception:
                    pass
    
    # Strategy 2: Sandbox (existing)
    # Strategy 3: Knowledge search (existing, fallback)
    ...
    return context_files
```

The `_resolve_knowledge_source_path()` method reads from settings:
```python
def _resolve_knowledge_source_path(self) -> Path | None:
    path_str = getattr(self.tool_gateway.settings, "knowledge_source_path", None)
    if path_str:
        p = Path(path_str)
        if p.is_dir():
            return p
    return None
```

This way files get full content (up to 120KB), which is enough for MiniMax to produce valid diffs.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — modify `_gather_codegen_context()` to read full files from knowledge source path, add `_resolve_knowledge_source_path()`.

## Tests

Add to existing orchestrator tests:

1. **`test_gather_context_reads_full_file`** — Create a temp dir with a 50-line file. Set `knowledge_source_path` to that dir. Mock plan with an affected location pointing to that file. Assert `_gather_codegen_context()` returns the FULL file content (all 50 lines), not a snippet.
2. **`test_gather_context_truncates_large_file`** — Create a file larger than `knowledge_max_file_bytes`. Assert content is truncated.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass.
- Full suite still green.
- `_gather_codegen_context()` returns full file content for files that exist on disk.
- Files larger than `knowledge_max_file_bytes` are truncated.
- If knowledge_source_path is not configured, falls back to existing strategies.

## Workflow (for the executor)

<!-- Effort: low — add file read strategy to existing method -->

1. Read `app/orchestrator/service.py` — focus on `_gather_codegen_context()`, `_read_sandbox_context_file()`, `_read_knowledge_context_file()`.
2. Add direct file read strategy and `_resolve_knowledge_source_path()`.
3. Add tests.
4. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q7-full-file-context.md
```
