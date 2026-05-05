# Demo Script

A 5-minute walkthrough for a recruiter / interviewer. The story is
**architectural primitives**, not "autonomous code shipping."

## Pre-flight checklist

```powershell
# 1. Backend
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1

# 2. Wait for healthy
curl http://127.0.0.1:8000/health

# 3. Frontend
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1 -Dev
```

Open `http://127.0.0.1:5173`. Log in as `viggo` / `team_lead`.

## Demo flow (5 min)

### Beat 1 — "What is this?" (30s)

> "This is an enterprise ops agent platform. Think of it as the layer
> *above* Claude Code or Codex CLI: it adds governance, audit, retrieval,
> failure-feeding memory, and a gate stack — so you can route Jira
> tickets, repository questions, and ops actions through LLMs in a way
> that's safe to operate."

Show the workbench. Point out:
- Sidebar — recent conversations, role badge.
- Knowledge tab — KB sources, FTS5 indexing.
- Memory tab — agent memory.
- Settings — provider/model controls.

### Beat 2 — "It actually answers questions about a real codebase" (60s)

In the chat:

> Q: *"How does the customer signup flow work in the handyman app?"*

Watch the timeline:
1. Semantic translation (MiniMax)
2. Plan generation (claude_code)
3. Knowledge retrieval (Plan A FTS5)
4. Synthesis (deepseek)
5. Final answer with **clickable citations** to specific files + line ranges.

Talking points:
- "Retrieval here is FTS5 with CamelCase-aware tokenization. Before this
  fix anchor recall was 9.2%; now it's 91.5% on the same dogfood corpus."
- "Every citation is a `(file, line_start, line_end, snippet)` tuple
  persisted as evidence."

### Beat 3 — "Show me the gate stack catching cheating" (90s)

This is the differentiating story. Open the task list, find a recent
Jira-issue-develop task that *failed*. Show the event timeline:

> "When the LLM tries to make the gate happy without doing the work,
> we want to catch that, not paper over it. Three failure modes
> we've observed and addressed:"

#### Cheat 1: comment stuffing
- Show task v8 (or a reproduction): codegen put required tokens in
  `// comments`.
- Show `_strip_comments` step in the pipeline replaying the file with
  comment bytes zeroed.

#### Cheat 2: shell-only edits
- Show task v10b: `<EditText>` UI shell + comment-only Kotlin edit.
- Show G2 token derivation: required tokens are CamelCase / snake_case
  identifiers only, plain English ('home', 'address') dropped.
- Show: scan scope is **diff-added lines**, not full file. Gate catches
  the cheat → REVIEW_FAILED.

#### Cheat 3: ref without decl
- Open `apps/backend/app/services/symbol_graph/`.
- Show the `SymbolExtractor` Protocol — 1 file, 40 LOC.
- Show `python_extractor.py`, `kotlin_extractor.py`, `xml_extractor.py`
  — three independent plug-ins for three different parsers.
- Show the test
  `test_v9_failure_pattern_reproduced` — AndroidManifest references
  `@string/google_maps_api_key` but `strings.xml` lacks the decl.
  Gate flags `no_decl_found` for that ref before compile_gate even
  starts (saves 60-180s).

### Beat 4 — "It learns from failures" (45s)

Open `apps/backend/app/services/memory.py`.

> "Every gate failure — `REVIEW_FAILED`, `COMPILE_FAILED`, `TOOL_FAILED`
> — is fed to AgentMemory. Stored with scope (`tool:jira`,
> `gate:compile_gate`) and indexed in FTS5. Future tasks that hit
> similar code paths see relevant past failures in their planner
> context."

Show the memory page in the UI. Filter by scope. Point out provenance
links back to the originating task.

### Beat 5 — "How do I add a new language?" (30s)

Open `docs/ARCHITECTURE.md` → SymbolGraph section.

> "Adding TypeScript is one new file: a 50-line extractor that
> implements the Protocol. The orchestrator code does not change.
> Files with no registered extractor are gracefully skipped — they
> never crash the pipeline."

Show the registry pattern in `registry.py`.

### Beat 6 — "What's the audit story?" (30s)

Open the OpenAPI explorer at `http://127.0.0.1:8000/docs`. Hit:

```
GET /api/tasks/{task_id}/events
GET /api/tasks/{task_id}/tool-executions
GET /api/health
```

Show the `tool_failure_rate_1h` and `external_api_recent_failures_5min`
fields.

> "Every state transition is an event row. Every tool invocation is a
> `ToolExecution` row with attempt log + retry history. Postmortem on
> any task is reconstructable from the database alone — no log scraping."

## Closing pitch

> "The platform itself is the deliverable. Frontier autonomous codegen
> on multi-file Android features isn't solved by anyone in 2026. But
> the **architectural primitives** — retrieval, gates, memory, governance
> — are reusable across any LLM agent product. This codebase is a
> system-design study at production scale (1300+ tests, 8 LLM providers,
> RBAC, audit trail)."

## Q&A prep

| Question | Answer |
|---|---|
| Why SQLite + FTS5 not pgvector? | KB is < 200 docs; FTS5 lexical recall is 62-74%, top-1 rank is 1.0 when found. Embedding rerank is on the roadmap but not the bottleneck. |
| Why not just use Devin? | Devin is single-tenant, interactive, no audit. We sit *above* such tools as the governance layer; codegen step can dispatch to Claude Code or Codex CLI. |
| What about scaling beyond SQLite? | The data layer is SQLAlchemy — Postgres swap is configuration, not refactor. FTS5 → tantivy / Postgres FTS / Elasticsearch is a service-side change behind the same `KnowledgeService` interface. |
| Worst failure mode you saw? | Codegen reward-hacking via `// comments`. We caught it via `_strip_comments` (Stage X.8.b) → then they evolved to UI-shell + comments → caught by G2 (diff-scoped strict tokens) → next they added Manifest refs without strings.xml decls → caught by SymbolGraph. The stack is layered against successive evasions. |
| Test count? | ~1300 unit tests across services, gateway, orchestrator, knowledge, evidence, symbol_graph, memory. New retrieval/gate primitives have 50-80 tests each. |

## After the demo

Files to share:
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DEMO.md` (this file)
- A 2-minute screen capture of the chat flow.
- `git log --oneline -30` — shows commit hygiene.
