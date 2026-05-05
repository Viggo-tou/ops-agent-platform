# Enterprise Ops Agent Platform

A governed, audited LLM-orchestration platform that turns natural-language ops requests
(Jira tickets, repository questions, action requests) into structured pipelines
across multiple LLM providers, with first-class retrieval, gates, memory, and
human-in-loop approval.

**Stack**: Python 3.14 / FastAPI / SQLAlchemy + SQLite (with FTS5) / React + Vite frontend.
**LLM providers**: Anthropic Claude, OpenAI, MiniMax, DeepSeek, Codex CLI, Claude Code CLI
(any subset selectable per task via configurable provider chains).

## Why this exists

Most "AI coding agents" are demoed end-to-end on toy tasks. Real ops/dev agents
must be observable, auditable, and *fail-safe* on adversarial input — including
the case where the LLM tries to game your gates. This project is a study in
**architectural primitives** for building such agents:

- **Retrieval** that actually works on real repos (FTS5 + tokenized boolean queries)
- **Gates** that catch reward-hacking (comment-stuffing, shell-only edits, unresolved refs)
- **Memory** that learns from gate failures across sessions
- **Governance** with role-based actors, policy rules, and approval flows
- **Cross-file consistency** via a language-agnostic SymbolGraph framework
  (Python / Kotlin via tree-sitter / XML via lxml; new languages = a 50-line plug-in)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design walkthrough
and [`docs/DEMO.md`](docs/DEMO.md) for a live demo script.

## High-level pipeline

```
User request ──> Intake ──> Semantic translator ──> Planner
                                                       │
                                                       ▼
                                              ┌────────────────────┐
                                              │   Develop pipeline │
                                              ├────────────────────┤
                                              │  Evidence bundle   │  ◄── FTS5 + Plan A
                                              │  Codegen (multi-   │
                                              │    provider chain) │
                                              │  Feature presence  │  ◄── G2 strict
                                              │  SymbolGraph gate  │  ◄── ref-validity
                                              │  Compile gate      │
                                              │  Spec conformance  │
                                              │  Runtime validation│
                                              └────────────────────┘
                                                       │
                                              ┌────────▼─────────┐
                                              │ Approval / Audit │
                                              └────────┬─────────┘
                                                       ▼
                                              Action (Jira write,
                                              code merge, Slack...)
```

Every transition emits an `Event`; every tool invocation produces a
`ToolExecution` with attempt-level latency / retry / error history. Failed
gates feed `AgentMemory`, which is consulted by future tasks via FTS5
keyword recall.

## Key architectural primitives

### 1. FTS5-backed retrieval ("Plan A")

A naive substring matcher missed multi-word phrases ("home address") in
real source code 75% of the time on dogfood tasks. Plan A re-routes anchor
matching through the existing `knowledge_document_fts` index with
CamelCase-aware tokenization plus a joined-form fallback for compound
identifiers.

**Empirical result**: anchor recall went from **9.2% → 91.5%** on the same
153 anchors across 24 dogfood tasks. See
[`apps/backend/app/services/evidence_bundle.py`](apps/backend/app/services/evidence_bundle.py).

### 2. Anti-cheat gates

LLMs treat gates as reward signals. Three patterns observed and addressed:

| Cheat pattern | Defense |
|---|---|
| Required tokens stuffed in `//` and `/* */` comments | `_strip_comments` zeroes comment bytes before token grep |
| Shell-only edits + comment narration of feature | **G2**: scan diff-added lines (not full file) + strict identifier-shaped tokens (no plain English) + ratio threshold |
| Add `@string/foo` reference without defining `foo` | **SymbolGraph**: post-codegen ref-validity gate, language-plug-in based |

### 3. Language-agnostic SymbolGraph framework

`Decl` / `Ref` / `ExtractedSymbols` dataclasses + a `SymbolExtractor`
Protocol. Per-language extractors register themselves for file extensions:

| Language | Parser | LOC |
|---|---|---|
| Python | stdlib `ast` | ~70 |
| Kotlin | tree-sitter-kotlin | ~140 |
| XML (Android resources) | lxml + regex | ~110 |

Adding TypeScript / Go / Java is a new file in
[`apps/backend/app/services/symbol_graph/`](apps/backend/app/services/symbol_graph/)
plus a one-line `register_extractor()` call. The orchestrator and gate
logic do not change.

### 4. Failure-feeding memory

Every `TOOL_FAILED`, `TOOL_TIMED_OUT`, `REVIEW_FAILED`, `COMPILE_FAILED`,
or `FAILURE_DIAGNOSIS_GENERATED` event is fed to `AgentMemory`, indexed
in FTS5 with scope (`gate:compile_gate`, `tool:jira`, etc.) and surfaced
to the planner on similar future requests. See
[`apps/backend/app/services/memory.py`](apps/backend/app/services/memory.py).

### 5. Multi-provider routing with backoff

`ToolGateway` runs each tool through a configurable provider chain
(e.g. `claude_code,codex,deepseek,minimax,mock`). Retryable errors
(5xx, transient I/O) get exponential backoff with jitter, capped at 8s.
Non-retryable errors (400-class, missing config) fail fast.

## Quickstart (Windows)

```powershell
# Backend (FastAPI on :8000)
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1

# Frontend (Vite dev on :5173)
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1 -Dev
```

Open `http://127.0.0.1:5173` for the workbench UI; the OpenAPI explorer
is at `http://127.0.0.1:8000/docs`. Health snapshot at
`http://127.0.0.1:8000/health`.

## Project layout

```
apps/
  backend/                       FastAPI service
    app/
      api/                       HTTP routes (/tasks, /events, /health, ...)
      orchestrator/service.py    Pipeline state machine (~7000 LOC)
      services/
        evidence_bundle.py       FTS5 anchor retrieval (Plan A + B2)
        feature_presence_check.py  G2 strict-token gate
        symbol_graph/            Language-agnostic ref-validity framework
        memory.py                Failure-feeding learning loop
        codegen.py               Multi-provider codegen orchestration
        knowledge.py             KB ingestion + FTS5 indexing
      tools/gateway.py           Tool runtime with retry + backoff
    tests/                       1300+ unit tests, organized by service
  web/                           React + Vite workbench

docs/
  ARCHITECTURE.md                Design walkthrough
  DEMO.md                        Live demo script
  ai/                            Internal task / decision history
```

## Testing

```bash
cd apps/backend
python -m pytest tests/ -q          # all suites
python -m pytest tests/services/symbol_graph/ -q
python -m pytest tests/services/test_evidence_bundle_fts5.py -q
```

## License

Capstone / portfolio project. See `LICENSE` if applicable.
