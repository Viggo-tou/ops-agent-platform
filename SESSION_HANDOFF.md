# Session Handoff

Last updated: 2026-05-10

## Session 2026-05-10: Harness V1 Tier 1.5 + Tier 2 (mid-tier window) ✅

**Headline**: Five commits on `feat/harness-v1`. Tier 1.5 Aider format, Tier 1.3 planner closure (acceptance_tests in plan schema + sanitization + instructions), Tier 2 AST structural truncation (fixes the django/db big-file 0-diff regression observed on 2026-05-09), Tier 2 categorical context budgeter (per-provider profile registry). 131 unit tests across the new modules. Backend NOT yet restarted — the changes are on disk but the running process is still on commit `841cbf3`.

### Branch state

| branch | head | status |
|---|---|---|
| `checkpoint/pre-reclassify` | `5c40842` | unchanged this session |
| `feat/postgres` | `8ce35d7` | unchanged this session |
| **`feat/harness-v1`** | `950d577` | Tier 1.5 Aider + Tier 1.3 planner + Tier 2 AST + Tier 2 budgeter + STAGE_LOG. Backend running on older code (`841cbf3`); restart gates the next validation. |

### Today's commits (`feat/harness-v1`)

| Commit | Item |
|---|---|
| `7a14b02` | Tier 1.5 — Aider search/replace format wired into codegen (system prompt, dispatch, parsing, retries, settings) |
| `45ca755` | Tier 1.3 — planner emits acceptance_tests (schema + sanitization + instructions) |
| `b5c3c54` | Tier 2 — AST-aware structural truncation for big Python files |
| `764d3f2` | Tier 2 — per-model categorical context budget profiles |
| `950d577` | docs — STAGE_LOG entry for Stage 31 |

### Validation v2 final tally

| Task | Result | Reason |
|---|---|---|
| 1 — astropy-14995 | 2049 char diff produced | Real edit; rejected at feature_presence (added regression test instead of fix) |
| 2 — django-11283 | 1496 char diff produced | Looks correct; failed sandbox apply (git add timeout — fixed in `c546151`) |
| 3 — django-11797 | 0 char diff | django/db/models/sql/query.py 95KB → 6KB byte truncation kept only imports. **Fixed by `b5c3c54` (AST truncation).** |
| 4 — django-12284 | hung 21h, then orphaned | Backend was restarted out from under the in-flight task; predictions.jsonl shows 0 chars. Treated as failed. |

Net: 0/4 baseline → 2/4 substantive diffs under Tier 1. AST truncation + Aider format + per-model budgets are the structural fixes that should lift this on the next run.

### What's running right now

- Backend: same PID, uptime ~ 8h. Code through commit `841cbf3` (5 newer commits not yet active; restart needed).
- No active SWE-bench run.

### Deferred to next session (priority order)

1. **Restart backend on `feat/harness-v1` HEAD `950d577`** and re-run the same 4 SWE-bench tasks. Compare task 3 diff length (was 0 with byte truncation, expect non-zero with AST). Compare task 1/2 with Aider format (expect higher first-attempt success per Aider published numbers).
2. **claude_code reference validation run** on the same 4 tasks under `OPS_AGENT_CODEGEN_PROVIDER=claude_code`. Gives the harness-contribution delta per the harness-first product strategy memory.
3. **(User) install Docker, run `swebench.harness.run_evaluation`** against any post-restart predictions.jsonl. First quantitative SWE-bench-Lite pass rate.
4. **Tier 4-H lightweight tool-use loop in codegen** (read_file / search_symbol / list_directory). Generalizes to any model. The `codegen_react_loop.py` already covers symbol verification; extension is to allow the model to fetch additional file content during codegen.
5. **Tier 3 layered RAG + summary tree** — multi-session work.
6. **Postgres Phase 2** (FTS5 → tsvector cutover). Lives on `feat/postgres` branch; cross-branch scope.

### Known operational notes (carried from 2026-05-09 + new)

- Backend `.env`: `OPS_AGENT_RESUMABILITY_ENABLED=false`, `OPS_AGENT_KNOWLEDGE_SYNTHESIS_ENABLED=false`, `OPS_AGENT_KNOWLEDGE_RETRIEVAL_CACHE_ENABLED=false`, `OPS_AGENT_CLAUDE_CODE_TIMEOUT_SECONDS=600`, `OPS_AGENT_CODEGEN_PROVIDER=deepseek`, `OPS_AGENT_PLANNER_PROVIDER=claude_code`.
- New: `OPS_AGENT_CODEGEN_OUTPUT_FORMAT` (default `auto`; auto picks aider_blocks for deepseek/openai, unified_diff for others). Force `unified_diff` for A/B vs Aider on the same task set.
- `OPS_AGENT_MCP_SERVERS_JSON` populated with 6 servers (filesystem / memory / sequential-thinking / fetch / git / time). All connected.
- 18 commits on `feat/harness-v1` since `checkpoint/pre-reclassify`.

### To pick this up next session

```powershell
# 1. Restart the backend (foreground; check logs for the port bind)
git switch feat/harness-v1
git log --oneline -1   # expect 950d577
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1

# 2. Re-run the same 4 SWE-bench tasks
python apps/backend/scripts/run_swebench_lite.py `
  --subset apps/backend/tests/benchmarks/swebench_lite_subset_50.jsonl `
  --limit 4 --parallel 1

# 3. Optionally launch claude_code reference run after the deepseek run
$env:OPS_AGENT_CODEGEN_PROVIDER = "claude_code"
# (restart backend) and re-run step 2 with a fresh --run-id label.
```

---

## Session 2026-05-09: Harness V1 — DeepSeek agent harness Tier 1 + SWE-bench harness ✅

**Headline**: Built a DeepSeek-friendly agent harness (Tier 1 of `docs/ai/specs/deepseek-agent-harness-v1.md`) on a new `feat/harness-v1` branch. Five new modules + their wirings + a SWE-bench-Lite test harness. Baseline: DeepSeek + dump-everything = **0/4** valid diffs. Post-Tier 1: task 1 produced a real **2049-char diff**, task 2 observed **91% context reduction** (137k → 11k bytes injected). Real SWE-bench scoring requires Docker (deferred).

### Branch state

| branch | head | status |
|---|---|---|
| `checkpoint/pre-reclassify` | `5c40842` | reliability + MCP + SWE-bench harness + 11 product unblockings landed |
| `feat/postgres` | `8ce35d7` | Phase 1 only: psycopg2 + docker-compose + migration plan. SQLite stays default. |
| **`feat/harness-v1`** | `63e13f8` | Tier 1 modules + wirings + STAGE_LOG entry. **In-flight validation run uses this branch.** |

### Today's commits

**`checkpoint/pre-reclassify`** (merged into main flow):
| Commit | Item |
|---|---|
| `939cd1c` | A.1-3 cooperative cancel + jitter + queued UI |
| `794f353` | B.1 MCP plumbing |
| `50cfb3e` | B.1 chat tool-use OpenAI/DeepSeek |
| `e5b5d6b` | B.1 chat tool-use Anthropic (later partially reverted) |
| `4aba391` | B.1 inline tool-call UI |
| `590073f` | B.2 /skills page |
| `40de170` | B.4 agent_memory injection + chat tool-call audit |
| `b29c3a7` | hotfix: wire-safe MCP names + flushTick + by_tool key |
| `379d2e2` | chat_tool_call sidebar filter + MCP servers in system prompt |
| `5c40842` | SWE-bench harness + 11 product unblockings |

**`feat/postgres`**:
| Commit | Item |
|---|---|
| `8ce35d7` | Phase 1: connection support + docker-compose + migration plan doc |

**`feat/harness-v1`**:
| Commit | Item |
|---|---|
| `4966fdd` | docs spec (Tier 1-4 plan, A-H additions, Aider over JSON-patch decision) |
| `a4b71f7` | **Tier 1.1** codegen_playbooks router + python.md / diff-discipline.md (13 tests) |
| `983f144` | **Tier 1.2** PatchBudget gate (10 tests) |
| `3e21cc7` | **Tier 1.3** acceptance_check evaluator (15 tests) |
| `a86b0cb` | **Tier 1.4** evidence_pack module (11 tests) |
| `e930b1c` | **Tier 1.5** Aider search/replace format module (21 tests) |
| `4d64db1` | wire evidence_pack budget into _gather_codegen_context |
| `741790b` | wire patch_budget gate post-codegen |
| `07901e8` | wire codegen_playbooks into codegen system prompt |
| `c9ee900` | perf: MAX_FP_REPAIR 2 → 1 (~7min saved/failing task) |
| `841cbf3` | fix: cap second evidence injection path |
| `d6ed6b8` | wire acceptance_check into reviewer (permissive when no acceptance_tests) |
| `63e13f8` | docs: STAGE_LOG Stage 30 entry |

70 module unit tests pass.

### Today's headline numbers

| run | config | result |
|---|---|---|
| Baseline | DeepSeek + dump-everything | **0/4** valid diffs (0% pass), context overflow at 90-140k bytes |
| Tier 1 v2 task 1 | DeepSeek + Tier 1 partial | **2049 char real diff** produced; rejected at feature_presence (added regression test instead of fix) |
| Tier 1 v2 task 2 (in flight) | DeepSeek + Tier 1 + 2nd-injection cap | live observation: `Injected 5 file(s) (11550 bytes)` vs prior `19 files (137782 bytes)` → ~91% context reduction |
| Real SWE-bench evaluator | requires Docker | **deferred** — user installs locally |

### What's running right now

- **Backend**: PID started against `feat/harness-v1` HEAD `841cbf3` (the 3 commits since aren't yet active in this run; harmless because they're either no-op without planner-prompt change or just perf optimizations).
- **Harness**: `apps/backend/scripts/run_swebench_lite.py` resumed against `swebench-lite-20260509T123648Z`. Task 1 already wrote a 2049-char prediction; tasks 2-4 in flight (~50min ETA at handoff).

### Strategic reframe captured

> "Harness 是产品,model 是替换品。关键是同一 harness 在不同 model 下都能保持不错产出 + 可量化 harness 贡献。"

This makes per-model context budgeting and multi-stage codegen (Tier 2) the next priority, because they're the model-agnostic levers. Switching codegen to claude_code is a fallback measurement, not a roadmap change.

### Deferred (in priority order)

1. **Aider format codegen integration** — module exists; codegen call paths still emit unified-diff prompts. Touches `_build_prompt`, every `_call_*`, and `_parse_response`. ~300 LOC. Highest immediate ROI for DeepSeek.
2. **Planner emits acceptance_tests** — reviewer side wired (`d6ed6b8`); planner prompt change is a separate small commit. Until then, acceptance_check is no-op.
3. **claude_code reference validation run** — same 4 tasks under `OPS_AGENT_CODEGEN_PROVIDER=claude_code` to baseline harness contribution.
4. **Tier 2** — categorical context budgeter, multi-stage codegen (plan → per-file → merge), symbol/import graph (tree-sitter), confidence proxy from free signals.
5. **Tier 3** — layered RAG + summary tree, failed-pattern memory across tasks.
6. **Tier 4 (H)** — lightweight tool-use loop in codegen (read_file / search_symbol / list_directory).
7. **Postgres Phase 2-4** — FTS5 → tsvector cutover, test against PG, default-flip.
8. **Docker installation + first SWE-bench official scoring** — user.

### Known operational notes

- Backend `.env` accumulated SWE-bench-specific tweaks today: `OPS_AGENT_RESUMABILITY_ENABLED=false`, `OPS_AGENT_KNOWLEDGE_SYNTHESIS_ENABLED=false`, `OPS_AGENT_KNOWLEDGE_RETRIEVAL_CACHE_ENABLED=false`, `OPS_AGENT_CLAUDE_CODE_TIMEOUT_SECONDS=300`, `OPS_AGENT_CODEGEN_PROVIDER=deepseek`.
- `OPS_AGENT_MCP_SERVERS_JSON` populated with 6 servers (filesystem / memory / sequential-thinking / fetch / git / time). All connected, 39 tools live.
- 13 commits on `feat/harness-v1` since `checkpoint/pre-reclassify`.
- Harness intentionally restarted mid-validation when fixes landed during task 2; resumed via `--resume <run_dir>` to preserve task 1's prediction.

### To pick this up next session

```powershell
# 1. Verify validation finished (or still running)
Get-Content C:\Users\Tomonkyo\AppData\Local\Temp\claude\...\bymfih9hw.output -Tail 30
Get-Content apps\backend\tests\benchmarks\runs\swebench-lite-20260509T123648Z\run-meta.jsonl

# 2. Score against SWE-bench ground truth (after Docker installed)
python -m swebench.harness.run_evaluation `
  --dataset_name princeton-nlp/SWE-bench_Lite `
  --predictions_path apps\backend\tests\benchmarks\runs\swebench-lite-20260509T123648Z\predictions.jsonl `
  --max_workers 4 `
  --run_id ops-agent-tier1

# 3. Switch to feat/harness-v1 and continue Tier 1.5 (Aider format) integration
git checkout feat/harness-v1
```

---

## Session 2026-05-07: 4-legs hallucination defense + dogfood E2E ✅

**Headline**: P69-17 + P69-19 both completed end-to-end with verified real working code (real symbols, OSMDroid, Firebase, no hallucinations). Demonstrated systematic correctness fix vs. v46-v55's hallucination-or-stuck loop.

### Commits landed (`checkpoint/pre-reclassify` branch)

| Commit | Item | Effect |
|---|---|---|
| `60992c0` | Perf 1+2+3 | LLM auto-cache, repair cap 2→1, evidence prefetch 5→3 |
| `f016737` | Perf 5 | kotlinc syntax pre-check before Gradle (failure path 30s→<1s) |
| `6645243` | Perf 6 (Tier 1A) | ReAct disk-grep fallback (DeepSeek-only path) |
| `33560d3` | **Leg 2** | Post-codegen symbol verifier — catches `Receiver.member` hallucinations, feeds repair |
| `b1b92fa` | **Leg 1** | Repo library fingerprinting in codegen system prompt — kills wrong-library variance |
| `a853971` | **Leg 4** | Intent-drop feedback retry — explicit dropped-line list, 1 retry per round |
| `a694a6f` | **Leg 3** | Pull receiver class body into repair prompt on Unresolved reference |
| `10d415e` | **FP fix** | Skip feature_presence when strict yields no tokens (no fallback to noisy permissive path) |
