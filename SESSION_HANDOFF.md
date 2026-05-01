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

---

## Session 2026-05-01 — Stage 19 official + Stage 20 judge-bias verdict

**Tag**: `session-start/2026-05-01-0930` (retroactively created at `a3f0cf4`; the mandatory session-start ritual was missed at the actual start and patched at session-close).

### Outcomes

1. **Stage 19 official rule-judge results recorded.** Dashboard 34Q mean **59.32** valid 25/34 (`qa-run-20260430T155831Z.jsonl`). Handymanapp 26Q mean **34.20** valid 17/26 (`qa-run-20260501T000245Z.jsonl`). Per-tier handymanapp: A 30.5 / B 41.3 / C 36.7 / D 10.0 (n=1).
2. **Stage 20 judge-bias verdict landed** (`docs/ai/specs/stage20-judge-verdict.md` + `DECISIONS.md` D-010). Cross-family rejudge (MiniMax) of both artifacts collapsed the dashboard→handymanapp gap from rule-judge **+25.12** to semantic-judge **+8.46**. The Stage 19 reading "cards don't generalize to Android" was largely a rule-judge paraphrase artifact, not retrieval/cards failure.
3. **Stage 20 priority locked in D-010**: 20A (hybrid judge) = PRIMARY; 20B (answer prompt) = DEPRIORITIZED; 20C (cards-v2) = NARROW + CONDITIONAL on n≥40 re-bench.

### Touched files (since `session-start/2026-05-01-0930`)

**Modified**:
- `apps/backend/scripts/run_qa_benchmark.py` — V3 smoke abort policy loosened (cross-source-only abort, not empty-source); new `retrieval_empty_no_source` bucket; systematic-empty 3-streak guardrail + early-window guardrail.
- `apps/backend/tests/scripts/test_run_qa_benchmark.py` — +6 tests for new bucket / smoke abort behavior. **27 green**.
- `apps/backend/scripts/rejudge_run.py` — 1-line fix: V3 `extract_answer_and_citations` returns 5-tuple; rejudge was unpacking 3.
- `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` — top-level `source_name` field on all 60 rows.
- `DECISIONS.md` — D-010 (Stage 20A → PRIMARY).
- `SESSION_HANDOFF.md` — this section.

**New**:
- `apps/backend/scripts/analyze_stage19_diagnostic.py` — diagnostic analyzer (per-tier means, low/high sample triage, hybrid-judge recommendation). Reusable across stages.
- `apps/backend/tests/benchmarks/qa_benchmark_dataset_handymanapp.jsonl` — 26Q split.
- `apps/backend/tests/benchmarks/qa_benchmark_dataset_hosteddashboard.jsonl` — 34Q split.
- `docs/ai/specs/stage20-judge-verdict.md` — ADR-style verdict (1.5 pages, 5 caveats, hybrid sketch, 4-item action plan).

**Bench artifacts (committed)**:
- `apps/backend/tests/benchmarks/runs/qa-run-20260430T155831Z.jsonl` — dashboard official.
- `apps/backend/tests/benchmarks/runs/qa-run-20260501T000245Z.jsonl` — handymanapp official.
- `apps/backend/tests/benchmarks/runs/qa-rejudge-handymanapp-minimax.jsonl` — Stage 20 diagnostic.
- `apps/backend/tests/benchmarks/runs/qa-rejudge-dashboard-minimax.jsonl` — Stage 20 diagnostic.

**Bench artifacts (left untracked)**:
Smoke runs and aborted-attempt artifacts under `tests/benchmarks/runs/` are not part of the verdict evidence chain. Either commit later as a "historical archive" or `.gitignore` selectively. Untouched in this session's commits.

### Tests

- `apps/backend/tests/scripts/test_run_qa_benchmark.py` — **27 passed** after V3 smoke patch.

### Queued tickets (referenced in `docs/ai/specs/stage20-judge-verdict.md`)

1. `T-JUDGE-HYBRID-V1` — implement hybrid judge in `KeypointJudge` per the verdict's sketch. Add second LLM family (restore Anthropic credit OR add OpenAI fallback). Currently MiniMax is the only cross-family judge.
2. `T-BENCH-TIMEOUT-HANDYMANAPP` — raise `question_timeout_seconds` to 360-480s for handymanapp (rejudge rescued 4 records where backend completed past the 240s polling deadline).
3. `T-STAGE19-REBENCH-N40` — after the above two land, re-bench handymanapp at n≥40 valid records to confirm/falsify the residual 8.46 gap.
4. Stage 20C decision deferred until n≥40 residual is measured.

### Independent infra issue

**Anthropic API key has zero credit balance** (`apps/backend/.env`). Stage 20A spec depends on a second LLM judge family — credit must be restored, or OpenAI added as the second-family fallback.

### What to say to continue

> 继续 Stage 20A：实现 hybrid judge per `docs/ai/specs/stage20-judge-verdict.md` sketch，同时恢复 Anthropic credit 或加 OpenAI judge fallback。然后跑 T-BENCH-TIMEOUT-HANDYMANAPP，再 re-bench 验证 residual gap.

---

## Session 2026-05-01-1324 — Stage 20A V1 landed (MM-only); hybrid retired-as-default

**Tag**: `session-start/2026-05-01-1324` at `db5ee82` (created at session open).

### Outcomes

1. **Hybrid judge implemented and smoked, then RETIRED as V1 default**. T-JUDGE-HYBRID-V1 + T-JUDGE-HYBRID-V1-FIX landed in code. Smoke against handymanapp and dashboard surfaced two issues: (a) wrong-file evidence credit (fixed by V1-FIX), (b) rule rung over-fires keypoints LLM correctly says miss. Manual inspection of 10 rule-vs-MM disagreement cases found TP=2, FP=3, ambig=5; net `TP − FP = −1`. Hybrid stays in code as `--judge-mode hybrid` (experimental) for V2 reuse.

2. **Stage 20A V1 = MM-only**. T-JUDGE-DEFAULT-MINIMAX-V1 landed: argparse default minimax, `auto` mode removed (silent rule-fallback was a benchmarking footgun), 3 new artifact summary fields (`judge_family_count`, `cross_family_validated`, `judge_caveats`) so single-family limitation is surfaced in every artifact.

3. **Bench question_timeout 240s → 480s** (T-BENCH-TIMEOUT-HANDYMANAPP). Rejudge had rescued 4 records where backend completed past the 240s polling deadline; default raised to 480s for cross-stack runs.

4. **DECISIONS.md D-010 amended**: original "Stage 20A = hybrid primary" framing superseded by "V1 = MM-only, V2 = cross-family hybrid (deferred)".

### Touched files (since `session-start/2026-05-01-1324`)

**Specs / docs (all new in this session)**:
- `docs/ai/tasks/T-JUDGE-HYBRID-V1.md` — original hybrid spec (retired-as-default)
- `docs/ai/tasks/T-JUDGE-HYBRID-V1-FIX.md` — evidence-rung scope fix (retired-as-default with V1)
- `docs/ai/tasks/T-JUDGE-DEFAULT-MINIMAX-V1.md` — V1 ship spec (the one that landed)
- `docs/ai/tasks/T-JUDGE-HYBRID-V2.md` — V2 deferred spec stub
- `docs/ai/tasks/T-JUDGE-AMBIG-CALIBRATION.md` — V2 prerequisite calibration dataset stub
- `docs/ai/specs/stage20-judge-verdict.md` — V1 landing addendum appended

**DECISIONS.md** — D-010 amendment (V1 = MM-only, V2 deferred)
**SESSION_HANDOFF.md** — this section

**Code**:
- `apps/backend/scripts/run_qa_benchmark.py` — hybrid implementation (mode + helpers + summary aggregates) + V1 reset (default minimax, auto removed, family metadata helper)
- `apps/backend/scripts/rejudge_run.py` — hybrid mode wiring + V1 reset (default minimax, family metadata)
- `apps/backend/tests/scripts/test_run_qa_benchmark.py` — 6 hybrid tests, 2 evidence-scope tests, 3 wiring tests, 1 auto-rejected test, 1 family-metadata test (replaced 1 auto-fallback test). Net **36 passed**.

**Bench artifacts (committed)**:
- `qa-rejudge-handymanapp-hybrid-fixed.jsonl` — experimental hybrid rejudge (post-FIX); useful evidence for V2 calibration

### Tests

- `apps/backend/tests/scripts/test_run_qa_benchmark.py` — **36 passed** after V1 reset.

### Headline numbers (handymanapp 17 valid records, apples-to-apples vs same-set MM rejudge)

| Judge mode | Mean | Cross-stack gap (dashboard − handymanapp) |
|---|---|---|
| Rule | 34.20 | +25.12 |
| MiniMax | 51.78 | +8.46 |
| Hybrid (post V1-FIX) | 57.48 | +5.69 |
| Hybrid weights `rule=1/llm=1/ev=0` (recompute) | 57.08 | +4.84 |

Hybrid's smaller gap is partly artifact per the disagreement audit. **Stage 20C decisions should anchor on the MM gap of +8.46**, not the hybrid number.

### Queued tickets (current)

1. `T-JUDGE-HYBRID-V2` (P2, deferred) — cross-family hybrid; gated on Anthropic credit OR OpenAI key + calibration dataset.
2. `T-JUDGE-AMBIG-CALIBRATION` (P3) — build 20-30 row calibration dataset for V2.
3. `T-DATASET-HANDYMANAPP-EXPAND` (P2) — 26Q → 50-60Q for n≥40 valid records.
4. `T-STAGE19-REBENCH-N40` (P2) — re-bench under V1 with expanded dataset to confirm/falsify residual cross-stack gap.

### Independent infra issues

1. **Anthropic API key still has zero credit balance** (`apps/backend/.env`). Blocks T-JUDGE-HYBRID-V2 unless OpenAI added as alternative second family.
2. The dead `auto`-chain code in `KeypointJudge._judge_one` (lines ~517-568) is unreachable post-V1 reset. Cleanup ticket can be filed if surface area is a concern.

### What to say to continue

> 继续 Stage 20: 跑 T-DATASET-HANDYMANAPP-EXPAND（人工 + LLM 辅助扩 24-34 道题）然后 T-STAGE19-REBENCH-N40 验证 residual gap。Stage 20C 的 cards-v2 decision 要等 n≥40 数据。Anthropic credit 恢复后就可以 unblock T-JUDGE-HYBRID-V2.

### Stage 20A V2-CLI follow-up (same session)

**Discovery**: Codex CLI (ChatGPT subscription) and Claude Code CLI (Claude subscription) both satisfy "second LLM judge family" requirement WITHOUT needing API budget. The deferred V2 placeholder (`T-JUDGE-HYBRID-V2.md`) was replaced by `T-JUDGE-HYBRID-V2-CLI.md` with concrete Codex CLI integration.

**Implemented and committed** (commit `fb8afa7`, merged via `5387a21`):
- `--judge-mode hybrid_v2` mode: MM + Codex CLI co-primary AND-gated semantic judge
- 8-cell disagreement taxonomy in artifact summary
- UTF-8 stdin fix for Codex CLI subprocess (Windows cp936 → UTF-8)
- 41 tests green (5 new V2 tests)

**V2 verdict** (DECISIONS.md D-010 second amendment):
- V2 does NOT auto-promote to official (mean drift exceeds ±3 threshold)
- V2 mean drop is by design (AND-gate is mathematically conservative)
- V1 (`--judge-mode minimax`) remains official default
- V2 stays in code as `hybrid_v2` for cross-family diagnostic

**Cross-family validation outcome**:
- Dashboard 92%, handymanapp 95% MM-Codex agreement
- Cross-stack gap narrows: rule +25.12 → MM +8.46 → V2 +5.64
- 0 codex failures across 60 records post UTF-8 fix

**Most actionable diagnostic (Stage 20C decision input)**:
- `both_no_evidence_yes`: 50 kps (retrieval grounded, answer didn't articulate) → possible synthesis bottleneck
- `both_no_rule_no_evidence_no`: 62 kps (true misses) → cards-v2 target
- Both signals are similar magnitude; cards-v2 should NOT be deprioritized just because synth signal exists

**Bench artifacts (committed in this session's bench commit)**:
- `qa-rejudge-handymanapp-hybrid-v2.jsonl`
- `qa-rejudge-dashboard-hybrid-v2.jsonl`

**Open follow-up (next session)**:
- Manual sample inspection of 5-10 `both_no_evidence_yes` cases to confirm/refute the synthesis bottleneck hypothesis
- IF confirmed (≥70% real synth misses): spec synth-A/B experiment (Codex CLI as synthesizer, V1 MM judge for cross-family)
- IF refuted (≤30% real synth misses): proceed with cards-v2 narrow scope per Stage 20C
- T-DATASET-HANDYMANAPP-EXPAND remains queued (P1)
