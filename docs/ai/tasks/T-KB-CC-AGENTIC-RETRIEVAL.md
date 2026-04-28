# T-KB-CC-AGENTIC-RETRIEVAL — Claude Code CLI as the primary RAG retrieval mechanism

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium-high -->
<!-- Executor: codex -->

**Status:** todo (P0 — replaces T-KB-AST-CHUNKING as Phase 3.0 primary)
**Priority:** P0 (Phase 3.0 hard prerequisite under 2026-04-28 roadmap revision)
**Created:** 2026-04-28
**Replaces priority of:** T-KB-AST-CHUNKING (kept as fallback-only spec)

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Wire Claude Code CLI's `Glob` / `Grep` / `Read` tools as the primary retrieval mechanism for the knowledge / QA pipeline. An agent loop dispatches CC tool calls, collects results into the existing `EvidenceItem` schema, and feeds the synthesizer. RAG (BM25 / embedding) becomes a deterministic fallback when the CC path is unavailable, fails, or hits rate limits.

This is the 2026-04-28 priority replacement for the planned AST chunking work. Rationale: the active KB (`D:/项目/HostedDashboard/handyman-admin-dashboard`) is small (~few dozen files); CC running ripgrep + targeted reads is faster end-to-end (~6-10s) than current single-path RAG (13-18s) and solves D-tier multi-hop questions that AST chunking can't touch.

## Background

### Why now

Live baseline (from `docs/ai/benchmarks/qa-baseline-2026-04-23.md` + multi-run-log) is **27.06%** mean (multi-sample N=3). Top failure modes:
- A-05 ("locate ExportReportButton"): retrieval pulled license + bundle outputs instead of the source file
- D-01 / D-04: multi-hop, retrieval missed cross-file targets
- Login.js handleLogin trace: retrieval grabbed L1-5 imports instead of L35-82 function body

A direct CC `grep -r 'ExportReportButton' --include='*.js'` returns the source file in <100ms. A directed CC `Read Login.js` returns the whole file (with the LLM picking out the function). Multi-hop questions can use a 2-3 round agent loop. Predicted gains:
- A: 35 → 60+
- B: 11 → 35+
- D: 22 → 40+

AST chunking (the original Phase 3.0) helps A/B partly but doesn't touch C/D. AST is now reframed as fallback-only (see T-KB-AST-CHUNKING spec).

### Provider chain (mandatory, 2026-04-28 user-specified)

The agent loop's decision LLM (the model that decides "next action: cc_glob / cc_grep / cc_read / done") uses this chain in order:

1. **`claude_code` CLI** (preferred) — invoked via `npx @anthropic-ai/claude-code`
2. **`codex` CLI** (alternative) — invoked via `codex` binary
3. **`minimax` API** (last resort) — only if both CLIs fail

**`anthropic` API is NOT in this chain** — user does not budget for it. If `_resolve_provider_chain` defaults to including anthropic, this agent uses a separately configured chain (`cc_agent_provider_chain`) that omits it.

If all three fail, fallback to deterministic RAG (current path). Never silently degrade — log the fallback at WARN level.

### Dependencies

- `EvidenceItem` schema (from T-WS-FS-WORKSPACE) already defines `cc_glob` / `cc_grep` / `cc_read` source variants ✅
- Claude Code CLI on PATH (`npx @anthropic-ai/claude-code` works) ✅
- Codex CLI on PATH (`codex --version` returns `codex-cli 0.125.0`) ✅
- MiniMax API key in `.env` ✅

## Design

### A. CC tool wrapper module

New module: `apps/backend/app/services/cc_agent.py`

```python
@dataclass
class CCFileMatch:
    path: str          # repo-relative
    line: int | None   # for grep results

@dataclass
class CCToolResult:
    tool: Literal["glob", "grep", "read"]
    args: dict[str, Any]      # {pattern, file_glob, line_range, ...}
    matches: list[CCFileMatch]
    raw_text: str | None      # full output (for cc_read)
    duration_ms: int
    error: str | None         # when CC CLI returned non-zero or stderr noise


def cc_glob(pattern: str, *, cwd: Path, timeout_s: float = 10.0) -> CCToolResult: ...
def cc_grep(
    pattern: str, *, cwd: Path, file_glob: str | None = None,
    case_insensitive: bool = True, timeout_s: float = 20.0,
) -> CCToolResult: ...
def cc_read(
    path: str, *, cwd: Path, line_range: tuple[int, int] | None = None,
    timeout_s: float = 15.0,
) -> CCToolResult: ...
```

Each tool spawns a `claude` CLI subprocess in a controlled sandbox (cwd locked to the source repo path), parses output, returns a `CCToolResult`. On non-zero exit / timeout / parse failure → return result with `error` set.

### B. Agent loop module

New module: `apps/backend/app/services/cc_agent_loop.py`

```python
@dataclass
class CCAgentBudget:
    max_rounds: int = 3
    max_tool_calls: int = 8
    overall_timeout_s: float = 30.0
    per_call_timeout_s: float = 20.0

@dataclass
class CCAgentResult:
    evidence_items: list[EvidenceItem]   # from any CC tool that returned data
    rounds_run: int
    tool_calls_made: int
    duration_ms: int
    decision_model: str                  # "claude_code" / "codex" / "minimax"
    fallback_reason: str | None          # set if dropped to RAG

def run_cc_agent(
    query: str, *, cwd: Path, budget: CCAgentBudget,
    provider_chain: list[str] | None = None,
) -> CCAgentResult: ...
```

Agent loop ReAct-style:
1. Build initial prompt: query + "you can call cc_glob/cc_grep/cc_read; output JSON `{thought, action: {tool, args}}` or `{thought, done: true}`"
2. Call decision LLM (try each in `provider_chain` order until one returns valid JSON)
3. Parse action; dispatch tool; convert result to EvidenceItem(s); append to context
4. Loop until `done` OR budget exhausted
5. Return all collected EvidenceItems

If decision LLM returns invalid JSON 2x in a row → terminate with current evidence (degraded mode).

### C. Pipeline integration

`apps/backend/app/services/knowledge.py::KnowledgeRetriever.retrieve()` (or equivalent QA entry point):

```python
def retrieve(query: str, source_path: Path) -> list[EvidenceItem]:
    if settings.cc_agentic_enabled:
        try:
            result = run_cc_agent(query, cwd=source_path, budget=CCAgentBudget())
            if result.evidence_items and not result.fallback_reason:
                return result.evidence_items
            logger.warning(f"cc_agent fell back to RAG: {result.fallback_reason}")
        except Exception as e:
            logger.warning(f"cc_agent crashed, falling back to RAG: {e}")

    # Fallback: existing single-path RAG
    return rag_retrieve_fallback(query, source_path)
```

The `cc_agentic_enabled` flag defaults `True` once this lands; can be flipped off for benchmark A/B comparison.

### D. Configuration

```python
# apps/backend/app/core/config.py — additions
cc_agentic_enabled: bool = True
cc_agent_provider_chain: list[str] = ["claude_code", "codex", "minimax"]
cc_agent_max_rounds: int = 3
cc_agent_max_tool_calls: int = 8
cc_agent_overall_timeout_s: float = 30.0
cc_agent_per_call_timeout_s: float = 20.0
cc_grep_default_excludes: list[str] = [
    "*.css", "*.scss", "*.svg", "*.png", "*.jpg", "*.jpeg",
    "*.gif", "*.webp", "*.lock", "*.min.js", "*.min.css",
    "node_modules/**", "dist/**", "build/**", ".git/**",
]
```

Env overrides via `OPS_AGENT_CC_AGENTIC_ENABLED=false` etc.

### E. EvidenceItem mapping

Each CC tool result emits one or more EvidenceItems:

| Tool | EvidenceItem shape |
|---|---|
| `cc_glob` | one EvidenceItem per matched file (no line range; `chunk_kind="module"`) |
| `cc_grep` | one EvidenceItem per hit (`line_start`/`line_end` from grep output; `chunk_kind="grep_hit"`) |
| `cc_read` | one EvidenceItem with full read range (`chunk_kind="line_window"` if specific range given, else `"module"`) |

`EvidenceItem.source` is set to the corresponding `cc_glob` / `cc_grep` / `cc_read` literal.

### F. Failure modes (must handle gracefully)

| Failure | Response |
|---|---|
| `claude` CLI not on PATH | move to next provider in chain |
| `claude` CLI returns non-zero | log stderr; move to next provider |
| `claude` CLI timeout (>per_call_timeout_s) | kill subprocess; return error result; agent loop counts this as a tool failure |
| Decision LLM returns invalid JSON | retry once; if still bad, terminate with collected evidence (degraded) |
| Decision LLM hits rate limit | move to next provider in chain |
| Agent budget exhausted | terminate; use collected evidence; log "budget_exhausted" |
| Source repo path doesn't exist | hard fail (caller's bug) |

## Files to create

1. `apps/backend/app/services/cc_agent.py` — CC tool wrappers (glob/grep/read)
2. `apps/backend/app/services/cc_agent_loop.py` — agent loop + provider chain dispatch
3. `apps/backend/app/services/cc_agent_prompts.py` — agent decision prompt templates
4. `apps/backend/tests/services/test_cc_agent.py` — tool wrapper unit tests (mock subprocess)
5. `apps/backend/tests/services/test_cc_agent_loop.py` — agent loop integration tests (mock LLM + mock tools)

## Files to edit

1. `apps/backend/app/services/knowledge.py` — `retrieve()` wrap with CC-first / RAG-fallback logic
2. `apps/backend/app/core/config.py` — 6 new settings (section D above)
3. `apps/backend/app/services/codegen.py::_resolve_provider_chain` — leave alone (codegen is separate); but read it as reference for the CC agent's own provider resolution
4. `apps/backend/requirements.txt` — no new deps (subprocess + json suffices)

## Tests

### Unit tests (test_cc_agent.py)

1. `test_cc_glob_parses_claude_output_to_filematches` — mock claude CLI with canned glob output, assert `CCToolResult.matches` has correct paths
2. `test_cc_grep_parses_with_line_numbers` — mock grep output `path:line:content`, assert each hit has `line` populated
3. `test_cc_read_returns_full_text_when_no_range` — mock read whole file, assert `raw_text` equals mock content
4. `test_cc_read_with_line_range` — mock read with specific range, assert raw_text bounded
5. `test_cc_glob_timeout_returns_error_result` — mock subprocess that hangs > timeout_s, assert result has `error`
6. `test_cc_grep_nonzero_exit_returns_error` — mock subprocess returning rc=2 + stderr; assert error captured
7. `test_cc_tool_default_excludes_filter_resources` — mock grep with default file_glob excludes; assert `*.css` etc not searched

### Agent loop tests (test_cc_agent_loop.py)

8. `test_agent_first_provider_succeeds` — mock claude_code returns valid JSON action `{"action": {"tool": "glob", "args": {"pattern": "*.js"}}, "thought": "..."}`; assert evidence emitted, `decision_model == "claude_code"`
9. `test_agent_falls_back_to_codex_on_claude_fail` — mock claude_code raises; codex returns valid; assert `decision_model == "codex"`, `fallback_reason` not set (codex is in primary chain)
10. `test_agent_falls_back_to_minimax_when_both_clis_fail` — mock both CLIs raise; minimax returns valid; assert `decision_model == "minimax"`, `fallback_reason` not set
11. `test_agent_returns_fallback_to_rag_when_all_providers_fail` — all 3 raise; assert returned `CCAgentResult.evidence_items == []` and `fallback_reason == "all_providers_failed"`
12. `test_agent_terminates_on_done_action` — mock LLM returns `{"done": true}` after 1 round; assert agent terminates with collected evidence
13. `test_agent_terminates_on_budget_exhausted` — mock LLM returns valid actions indefinitely; agent terminates at `max_rounds`
14. `test_agent_handles_invalid_json_then_recovers` — mock LLM returns malformed JSON once, valid second time; assert agent retries once and proceeds
15. `test_agent_terminates_after_two_consecutive_invalid_json` — both attempts malformed; assert degraded-mode termination
16. `test_agent_evidence_items_have_correct_source_field` — assert glob → `cc_glob`, grep → `cc_grep`, read → `cc_read` source values
17. `test_anthropic_not_in_default_chain` — assert default `cc_agent_provider_chain` does not include `"anthropic"` (user-specified constraint)

### Integration tests (test_cc_agent_loop.py)

18. `test_knowledge_retrieve_uses_cc_when_enabled` — mock `cc_agentic_enabled=True` + agent returning evidence; assert `retrieve()` returns CC evidence, not RAG fallback
19. `test_knowledge_retrieve_falls_back_to_rag_when_cc_returns_empty` — mock agent returning `evidence_items=[]`; assert retrieve calls `rag_retrieve_fallback`

## Acceptance criteria

- `python -m compileall app` exits 0
- All 19 tests pass (mocked CC + LLM)
- Full backend suite still green (existing failures from non-ASCII path issue allowed; no NEW failures introduced)
- Manual smoke test (after merge):
  1. Start backend on a fresh port
  2. POST `/api/tasks` with question "现在 firebase 的认证逻辑是啥" against `D:/项目/HostedDashboard/handyman-admin-dashboard`
  3. Inspect resulting `latest_result_json.result.citations`: at least one citation must come from `cc_read` source AND cover lines 35-82 of `Login.js` (the `handleLogin` body)
  4. Backend log shows `cc_agent provider_used=claude_code rounds=N tool_calls=M duration=Xs`
- Provider chain ordering verified: setting `OPS_AGENT_CC_AGENT_PROVIDER_CHAIN=codex,minimax` (omitting claude_code) → agent uses codex first
- A/B disable test: setting `OPS_AGENT_CC_AGENTIC_ENABLED=false` → agent path skipped, RAG fallback used

## Out of scope (explicitly NOT in this card)

- Changing the synthesizer (LLM that produces the final answer); keep current synthesizer
- Adding agent memory across queries (Phase 5 work)
- Pre-indexing CC results / caching (Phase 3.5 work)
- File-card retrieval (`rag_card`) — deferred to Phase 3.3-B
- AST chunking (T-KB-AST-CHUNKING) — fallback only, separate ticket
- Re-running the QA benchmark — that's a follow-up after merge

## Risks

| Risk | Mitigation |
|---|---|
| `claude` CLI flaky on Windows (npm cache, OAuth) | Codex CLI + MiniMax fallback in chain |
| Agent loop diverges (invalid JSON forever) | 2-strike retry + degraded-mode termination |
| Subprocess overhead 1-2s per CC call adds up | Budget cap (max 8 tool calls); parallel grep/read in agent batch (future optimization) |
| Different file path conventions (CC outputs absolute paths, RAG uses repo-relative) | Normalize at EvidenceItem boundary; test_path_normalization unit case |
| LLM hallucinates non-existent files in `read` action | `cc_read` returns error result if file missing; agent sees error and adapts |

## Workflow (for the executor)

<!-- Effort: medium-high -->

1. Read `apps/backend/app/services/knowledge.py` to understand current retrieve flow + how RAG fallback should plug in
2. Read `apps/backend/app/services/codegen.py::_call_claude_code_*` for reference pattern of subprocess + claude CLI invocation (already exists for codegen)
3. Read `apps/backend/app/schemas/evidence.py` to confirm EvidenceItem shape + the 3 cc_* source values
4. Implement `cc_agent.py` (3 tool wrappers); use `subprocess.run` with timeout; mirror the patterns in `codegen.py` (cwd lock, env scrubbing)
5. Implement `cc_agent_prompts.py` — keep prompts deterministic, JSON-mode where supported
6. Implement `cc_agent_loop.py` with provider chain dispatch (try each in order, catch exceptions, log fallback reason)
7. Wire `knowledge.py::retrieve()` to call `run_cc_agent()` first when `cc_agentic_enabled`
8. Write all 19 tests with mocked subprocess + mocked LLM responses
9. Run `python -m compileall app` and unit suite
10. Manual smoke test against the handyman dashboard KB

```
codex exec --full-auto -C "<worktree>" - < docs/ai/tasks/T-KB-CC-AGENTIC-RETRIEVAL.md
```

Worktree: create fresh off `checkpoint/pre-reclassify`:
`D:/项目/ops-worktrees/cc-agentic` on branch `feat/kb-cc-agentic`.

## Follow-up tickets

- `T-KB-FILE-CARDS` — Phase 3.3-B, optional optimization layer (offline LLM cards + FTS5)
- `T-KB-CC-AGENT-PARALLEL` — parallelize independent grep/read calls within a single round
- `T-KB-CC-AGENT-MEMORY` — Phase 5 memory hooks (cache "this query ran these tools" across sessions)
