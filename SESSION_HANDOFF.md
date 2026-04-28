# Session Handoff

Last updated: 2026-04-28

## Session 2026-04-28: Phase 3.0 verified ✅ — CC agentic mean 27.06 → 49.65 (+22.59)

**Headline**: First time the project shipped a benchmark-validated optimization. CC agentic search beats single-path RAG by +22.59 mean on the 34-question dataset; completion 14/34 → 34/34; C-tier nearly doubled (41 → 70.62). Phase 1 (measurement baseline) and Phase 3.0 (CC retrieval) both complete.

### Branches landed today (NOT yet merged to main / checkpoint)

| Branch | Worktree | Commits |
|---|---|---|
| `feat/kb-cc-agentic` | `D:/项目/ops-worktrees/cc-agentic` | `6797098` (CC impl) → `2576033` (merge qa-bench) → `7a34c37` (--question-timeout 240s) → `c090419` (baseline 49.65 lock) |
| `feat/failure-diagnosis` | `D:/项目/ops-worktrees/failure-diagnosis` | `31bb852` (T-FAILURE-DIAGNOSIS impl) |
| `docs/ops-strategic-specs-2026-04-28` | main worktree | `f416249` → `c3af1a0` → `74d9096` → `a69d134` → `c8c238d` (5 commits: 5 strategic specs + STAGE_LOG discipline + Phase AA-AF entries + Phase 3 pivot to CC + Stage 9 close + 5 follow-up specs) |

### Where to find the artifacts

- Baseline report: `docs/ai/benchmarks/qa-baseline-2026-04-28.md` (on `feat/kb-cc-agentic`)
- Stage log: `docs/ai/STAGE_LOG.md` Stages 1-9 (on `docs/ops-strategic-specs-2026-04-28`)
- Phase summary: `docs/ai/phase-summary-zh.md` Phases AA-AF (on `docs/ops-strategic-specs-2026-04-28`)

### Next session: read these first

1. `docs/ai/STAGE_LOG.md` — last 5 entries (Stages 5-9)
2. `docs/ai/phase-summary-zh.md` — Phase AF section
3. `docs/ai/benchmarks/qa-baseline-2026-04-28.md` — the new reference number 49.65

### Next session: 5 ready-to-dispatch tickets (priority order)

| Priority | Ticket | What |
|---|---|---|
| P0 | T-MERGE-CC-AGENTIC-INTO-MAIN | Integrate today's 3 feature branches into checkpoint/pre-reclassify |
| P1 | T-KB-EVIDENCE-TIER-CAP | tier-aware snippet cap (D=6000 / ABC=3000) → recover D-tier ceiling |
| P2 | T-KB-CLI-POOL | warm `claude` CLI pool → runtime 71 → 50min |
| P2 | T-KB-HYBRID-RAG-FAST-PATH | route simple queries to RAG fast-path → runtime 71 → ~40min |
| P2 | T-WINDOWS-ASCII-PATH-DEBT | decide on path A/B/C for `D:\项目\` mojibake debt |

All 5 specs are at `docs/ai/tasks/T-*.md` on `docs/ops-strategic-specs-2026-04-28`.

### Two acceptance targets that did NOT meet, with mitigation paths

- **D-tier 30.33** (vs target 40): snippet cap=3000 hurt multi-hop. Fix in T-KB-EVIDENCE-TIER-CAP.
- **Wall-clock 71min** (vs target 45min): cap helped synthesis 80→60s, not enough. Fix in T-KB-CLI-POOL + T-KB-HYBRID-RAG-FAST-PATH.

### Files NOT committed (intentional)

- `D:/项目/ops-worktrees/cc-agentic/apps/backend/.env` — has local override `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=3000` to reproduce baseline 49.65. .env is gitignored. Documented in `qa-baseline-2026-04-28.md` "Required env to reproduce" section.

### Worktrees still alive (not cleaned up — Stage 4 deferred)

29 worktrees per Stage 1 audit. 10 stale (already merged), 12 active, 3 temp Claude. Cleanup is a separate stage; not blocking.

---

## Session 2026-04-27: docs analysis / no code changes

- Session-start tag: `session-start/2026-04-27-1558`
- Purpose: read recovery docs, roadmap docs, and active task specs to rebuild project context.
- Files touched: `SESSION_HANDOFF.md` only, for this required session manifest.
- Code changes: none.
- Git diff stat: this manifest is still in the working tree; no product/source files were modified.

---

## Session 2026-04-22: batch1 优化集成 + ship 到 main

### 成果

- **Ship**: `main` → `6a35bdc merge: batch1 optimizations (prompt-cache + parallel-gates + pytest-xdist + Phase X+Y + e2e fixtures)`
- **T-SANDBOX-TEMPLATE 回退** (commit `52aa143` on batch1)：相对路径在 `git -C <template> worktree add` 下被 template dir 解析成错误位置。未来修好后可重新引入。
- **Phase Z 写入** `docs/ai/phase-summary-zh.md`，包含 batch1 整合 + 基线校准经验。
- **Follow-up ticket**: `docs/ai/tasks/T-PHASE-Y-ANCHOR-FOLLOWUP.md` — fx_neg_nonexistent 从 knowledge 分支改走 code_develop 分支绕过 anchor check。
- **Memory 新规则**: `~/.claude/projects/.../memory/feedback_verify_baseline_first.md` — 判断 regression 前必须先实测基线，不可继承 summary 里的 "X passes" 断言。

### 关键发现

之前 session summary 里写的 "e2e_quick 4/4 pass" 基线**不可复现**。今天在 `ops-worktrees/e2e-fixtures` @ `e76c4f4`（纯 `t-e2e-fixtures` 无优化）上跑 e2e_quick 仍然 0/4。batch1 合并后也是 0/4，但失败模式改变（3/4 fixture 能正确进 codegen，不再短路走 knowledge.search）——这不是 regression，是**行为改善撞到了更深层的 pre-existing bug**。

### 文件清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `main` branch | committed (`6a35bdc`) | batch1 merge + T-SANDBOX-TEMPLATE revert |
| `integrate/optimizations-batch1` | committed | 所有集成历史保留，含 revert commit |
| `docs/ai/phase-summary-zh.md` | **未 commit** (on `checkpoint/pre-reclassify`) | 加了 Phase Z + Phase Y 勘误 |
| `docs/ai/tasks/T-PHASE-Y-ANCHOR-FOLLOWUP.md` | **未 commit** (新文件) | follow-up ticket |
| `SESSION_HANDOFF.md` | **未 commit** | 本段 |

### 后续

1. 把未 commit 的 docs 合进 `main`（需要用户明确授权；当前只在 `checkpoint/pre-reclassify` 工作区）。
2. 修 T-PHASE-Y-ANCHOR-FOLLOWUP（给 code_develop 分支加 anchor check 覆盖点）。
3. fx_bugfix_nullcheck / fx_css / fx_newfile 三个 pre-existing fixture 失败各自独立 ticket 跟进。

---

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
