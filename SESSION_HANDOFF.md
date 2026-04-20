# Session Handoff

Last updated: 2026-04-20

## Session 2026-04-20: T-042-S Sandbox Agent Codegen (Worktree Architecture)

### What happened this session

Tag: `session-start/2026-04-20-1300`

1. **Worktree-based codegen** — replaced temp-dir codegen with git worktree from source repo:
   - `_call_claude_code()` now dispatches to `_call_claude_code_worktree()` when `source_repo_path` points to a valid git repo
   - Worktree gives Claude Code full repo visibility (explore, verify, iterate)
   - Diff extracted via `git diff HEAD` instead of filesystem comparison
   - Falls back to `_call_claude_code_tempdir()` when no source repo available
   - Shared `_run_claude_cli()` handles subprocess, retry, Windows process cleanup

2. **Wired `source_repo_path` through the stack**:
   - `generate_patch()` in `codegen.py` accepts `source_repo_path: str | None`
   - `_execute_codegen_generate_patch()` in `gateway.py` passes it from payload
   - Orchestrator codegen call site reads `_resolve_knowledge_source_path()` and includes it in payload

3. **Settings additions** in `config.py`:
   - `cli_max_retries: int = 1`
   - `gate_repair_max_attempts: int = 1`
   - `gate_repair_timeout_seconds: float = 300.0`

4. **Provider chain reordered** — Claude Code first, Codex as fallback (supports worktree)

5. **Tests**: 257 passed, 2 xfailed, 0 failed (committed test suite)
   - 3 new worktree codegen tests added to `test_codegen.py`
   - 8 untracked test files (from prior sessions) need their orchestrator methods re-added

### Files changed (tracked)

| File | Change |
|------|--------|
| `apps/backend/app/services/codegen.py` | +713 -114: worktree codegen, tempdir fallback, shared CLI runner |
| `apps/backend/app/tools/gateway.py` | +4: pass source_repo_path |
| `apps/backend/app/orchestrator/service.py` | +3: pass source_repo_path in payload |
| `apps/backend/app/core/config.py` | +4: cli_max_retries, gate_repair_max_attempts, gate_repair_timeout_seconds |
| `apps/backend/tests/services/test_codegen.py` | +135: worktree tests |

### Known issues

- **Untracked tests expect missing methods**: 8 test files from prior sessions reference `_run_targeted_repair`, `_build_compile_repair_prompt`, etc. that were never committed. These need to be re-added to the orchestrator (they were part of T-042 gate repair from the prior session).
- **`test_runtime_validation_scope.py`**: imports `_is_source_file` which doesn't exist in `runtime_validation.py`

### Architecture diagram

```
Orchestrator._execute_develop_pipeline
  |
  +-- _gather_codegen_context()    <-- still provides batch context
  |
  +-- codegen.generate_patch()     <-- NEW: accepts source_repo_path
  |     |
  |     +-- _call_claude_code()
  |           |
  |           +-- source_repo_path valid?
  |           |     YES -> _call_claude_code_worktree()
  |           |              1. git worktree add
  |           |              2. Write .claude/CLAUDE.md with constraints
  |           |              3. claude -p --dangerously-skip-permissions
  |           |              4. git diff HEAD
  |           |              5. cleanup worktree + branch
  |           |     NO  -> _call_claude_code_tempdir()
  |           |              (legacy: temp dir + filesystem diff)
  |           |
  |           +-- _run_claude_cli()  <-- shared subprocess runner with retry
  |
  +-- sandbox.apply_patch()         <-- unchanged
  +-- compile_gate                  <-- unchanged
  +-- spec_conformance              <-- unchanged
```

### Next tasks

1. Re-add targeted repair methods to orchestrator (from prior session's T-042 work)
2. E2E test: run a develop pipeline from frontend with worktree codegen
3. Consider adding `_strip_inline_file_context()` to reduce prompt tokens in worktree mode

---

## Session 2026-04-17: T-041 Defense Matrix + Pipeline Provider Swap

### What happened this session

1. **T-041 Anti-Hallucination Defense Matrix** — all 8 new mechanisms implemented and verified:
   - Evidence bundle (T-041-01), Diff shape checker (T-041-02/03), Evidence chain (T-041-04)
   - Symbol reference gate (T-041-05), Failing test gate (T-041-06), Runtime validation (T-041-07)
   - Goal decomposition (T-041-08)
   - All integrated into `_execute_develop_pipeline` in `orchestrator/service.py`
   - E2E test: `tests/e2e_defense_matrix.py` — **41/41 pass**
   - Pytest: **101/101 pass** across defense-related suites

2. **Frontend E2E verification** — submitted P69-10 tasks from the browser:
   - `evidence_bundle.build` → fired, found 2/4 anchors
   - `diff_shape.check` → fired, passed
   - `compile_gate.check` → fired, **BLOCKED** (codegen produced syntax error)
   - `spec_conformance` → shadow_implementation + hit_delta confirmed blocking in older tasks
   - Anchor precheck → confirmed blocking ghost anchors

3. **Model upgrade** — `~/.claude/settings.json` updated to `claude-opus-4-7-20260416`

### Current state

- **Backend**: running with `OPS_AGENT_PRIMARY_AGENT_PROVIDER=mock` on port 8000
- **Frontend**: Vite dev server on port 5173
- **Tests**: 41/41 E2E + 101/101 pytest defense tests green
- **Git**: no new commits since `c6c3101`

### NEXT TASK: Pipeline Provider Swap (Claude + Codex)

**Goal**: Replace MiniMax with Claude API (planner/reviewer) + Codex CLI (codegen) in the backend pipeline, then run P69-10 from the Ops frontend to 100% completion.

**Architecture change needed**:

| Role | Current (MiniMax) | Target |
|------|-------------------|--------|
| Semantic translation | MiniMax chatcompletion_v2 | Claude API (`claude-opus-4-7`) |
| Plan generation | MiniMax chatcompletion_v2 | Claude API (`claude-opus-4-7`) |
| Codegen | MiniMax chatcompletion_v2 (JSON mode) | Codex CLI (`codex exec`) |
| Diff review | Deterministic (spec_conformance) | Keep as-is |

**Files to modify**:

1. `apps/backend/app/core/config.py` — add `codex_*` config (timeout, model, etc.)
2. `apps/backend/app/services/codegen.py` — add `_call_codex()` provider that invokes `codex exec` as subprocess
3. `apps/backend/app/agents/service.py` — ensure `_generate_plan_with_anthropic()` works (method may already exist or need creation)
4. `apps/backend/app/agents/translation.py` — add `_translate_with_anthropic()` for semantic translation
5. `.env` — set `OPS_AGENT_PRIMARY_AGENT_PROVIDER=anthropic`, `OPS_AGENT_ANTHROPIC_MODEL=claude-opus-4-7-20260416`

**Existing infrastructure**:
- `codegen.py` already has `_call_anthropic()` (line 214) — Claude codegen works
- `config.py` already has `anthropic_api_key`, `anthropic_base_url`, `anthropic_model` fields
- `.env` already has `OPS_AGENT_ANTHROPIC_API_KEY` configured
- Codex CLI installed: `codex-cli 0.120.0` at `/c/Users/Tomonkyo/AppData/Roaming/npm/codex`

**Execution path**: Frontend → POST /api/tasks with Jira key → jira_issue_develop → develop pipeline → all T-041 gates → approval → Jira writeback

### What to say to continue

> 继续：把 Codex CLI 接入 codegen provider，Claude API 接入 planner/reviewer，然后从前端跑 P69-10

---

## Prior sessions (archived)

See git history and prior handoff entries for T-039, T-038, T-034, T-026 context.
Prior test baselines: T-039 = 171 passed, T-034 = 129 passed.
Current baseline with T-041: 256+ tests (defense suites alone = 101).
