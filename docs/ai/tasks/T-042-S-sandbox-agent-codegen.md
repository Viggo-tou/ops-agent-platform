# T-042-S: Sandbox Agent Codegen — Replace Temp-Dir with Git Worktree

## Problem

The current `_call_claude_code()` in `apps/backend/app/services/codegen.py` (line ~300)
creates an empty `tempfile.mkdtemp()`, copies only the grep-discovered `context_files`
into it, then runs `claude -p --dangerously-skip-permissions`. This means Claude Code:

1. **Cannot explore** — it only sees the files we guessed it needs, not the full repo.
2. **Cannot verify** — it can't run tests, check imports, or inspect related modules.
3. **Cannot iterate** — single-shot edit in a throw-away dir with no history.

This is why the same model produces perfect code in CLI but broken code in the pipeline.

## Solution

Replace the temp-dir pattern with a **git worktree** from the knowledge source repo.
Claude Code runs in a full checkout of the target repo, edits files directly, and we
extract the diff via `git diff HEAD`.

## Architecture

```
Orchestrator._execute_develop_pipeline
  │
  ├── _gather_codegen_context()     ← unchanged (still feeds planner context)
  │
  ├── codegen.generate_patch()      ← CHANGED: new `source_repo_path` parameter
  │     │
  │     └── _call_claude_code()     ← REWRITTEN: worktree mode
  │           1. git worktree add <temp-branch> from source_repo_path
  │           2. Write .claude/CLAUDE.md with task constraints + allowlist
  │           3. Run claude -p --dangerously-skip-permissions in worktree
  │           4. Extract diff via `git diff HEAD`
  │           5. git worktree remove
  │
  ├── sandbox.apply_patch()         ← unchanged
  ├── compile_gate                  ← unchanged
  ├── spec_conformance              ← unchanged
  └── review                        ← unchanged
```

## Files to Edit

### 1. `apps/backend/app/services/codegen.py`

#### `generate_patch()` signature (line ~92)
Add optional `source_repo_path: str | None = None` parameter:

```python
def generate_patch(
    self,
    *,
    task_id: str,
    plan_json: dict[str, Any],
    context_files: dict[str, str],
    task_description: str = "",
    source_repo_path: str | None = None,   # NEW
) -> CodegenResult:
```

Pass `source_repo_path` through `_try_provider` to `_call_claude_code`.

#### `_try_provider()` signature (line ~128)
Add `source_repo_path: str | None = None` parameter. Pass it to `_call_claude_code`.

#### `_call_claude_code()` — REWRITE (line ~282)

Replace the current implementation with worktree-based codegen:

```python
def _call_claude_code(
    self, prompt: str, *,
    context_files: dict[str, str],
    source_repo_path: str | None = None,
) -> CodegenResult:
```

**New logic:**

1. **If `source_repo_path` is set and is a valid git repo:**
   - Generate a unique branch name: `codegen/{task_id_short}-{timestamp}`
   - Create worktree: `git worktree add -b <branch> <worktree_dir> HEAD`
     from `source_repo_path`
   - The worktree dir should be in a temp location (e.g. `tempfile.mkdtemp(prefix="ops_worktree_")`)
   
2. **Write a `.claude/CLAUDE.md` in the worktree** with:
   ```
   # Task Constraints
   
   You are modifying this codebase to implement the following task.
   Edit files directly. Only modify files relevant to the task.
   After making changes, verify syntax (no duplicate declarations,
   no missing brackets, no import errors).
   
   ## Allowed files
   [list of must_touch_files and affected_code_locations from context_files keys]
   
   ## Task
   [the prompt]
   ```
   
3. **Run Claude Code CLI** with the same flags as today (`-p --dangerously-skip-permissions --output-format json`)
   but with `cwd=worktree_dir` instead of `cwd=tempdir`.

4. **Extract diff** via `git diff HEAD` in the worktree, instead of filesystem comparison.
   This is more reliable and produces proper unified diff natively.

5. **Cleanup:**
   - `git worktree remove <worktree_dir> --force`
   - `git branch -D <branch>` in the source repo
   - Fallback: `shutil.rmtree(worktree_dir, ignore_errors=True)` on Windows

6. **Fallback:** If `source_repo_path` is None or not a valid git repo, fall back to
   the current temp-dir behavior (keep existing code as `_call_claude_code_tempdir`).

**IMPORTANT implementation details:**

- The `context_files` dict is still passed but only used for the `.claude/CLAUDE.md`
  allowlist and as fallback. The worktree has the full repo.
- On Windows, use `ignore_errors=True` for worktree cleanup due to git object file locks.
- Keep retry logic (the `for attempt in range(1 + max_retries)` loop). On retry,
  reset the worktree with `git checkout -- .` + `git clean -fd`.
- Keep the `_parse_claude_code_output` fallback for when `-p` doesn't edit files.
- The `_generate_diff_from_files` call is replaced by `git diff HEAD` output.

### 2. `apps/backend/app/tools/gateway.py`

#### `_execute_codegen_generate_patch()` (line ~611)

Pass `source_repo_path` from payload to `CodeGenerator.generate_patch()`:

```python
source_repo_path_value = payload.get("source_repo_path")
source_repo_path = str(source_repo_path_value) if isinstance(source_repo_path_value, str) else None

result = CodeGenerator(self.settings).generate_patch(
    task_id=task_id,
    plan_json=dict(plan_json_value),
    context_files={...},
    task_description=task_description,
    source_repo_path=source_repo_path,    # NEW
)
```

### 3. `apps/backend/app/orchestrator/service.py`

#### Codegen call site (~line 1787)

Add `source_repo_path` to the codegen payload. The value comes from
`_resolve_knowledge_source_path()`:

```python
source_path = self._resolve_knowledge_source_path()

batch_result = self._execute_develop_tool(
    task=task,
    actor_name=actor_name,
    tool_name="codegen.generate_patch",
    payload={
        "plan_json": _plan_json_for_codegen,
        "context_files": batch_files,
        "task_description": self._build_codegen_task_description(...),
        "source_repo_path": str(source_path) if source_path else None,  # NEW
    },
    ...
)
```

### 4. `apps/backend/app/core/config.py`

No new settings needed. `knowledge_source_path` (line 62) already provides the repo path.
`claude_code_timeout_seconds` (line 41) already controls the timeout.

## What NOT to Change

- `_call_codex()` — leave unchanged (codex has its own sandbox pattern)
- `_call_anthropic()`, `_call_deepseek()`, etc. — API-based providers don't use sandbox
- `_gather_codegen_context()` — still needed for batch splitting and planner context
- `_run_targeted_repair()` — uses `--print` mode (not agent mode), works fine as-is
- `_build_codegen_task_description()` — still needed for directives
- Sandbox clone/apply — still needed for the review gates

## Acceptance Criteria

1. `_call_claude_code()` creates a git worktree when `source_repo_path` is provided
2. Claude Code CLI runs in the worktree with full repo visibility
3. Diff is extracted via `git diff HEAD` (not filesystem comparison)
4. Worktree is cleaned up after codegen (branch deleted, worktree removed)
5. Fallback to temp-dir mode when no source repo path is available
6. All existing tests pass: `python -m pytest apps/backend/tests/ -x -q`
7. The `_call_codex` method is NOT changed

## Workflow (for the executor, i.e. Codex)

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-042-S-sandbox-agent-codegen.md
```
