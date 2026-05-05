# Architecture

A walkthrough of the design decisions and component boundaries.
Companion to [`README.md`](../README.md) and [`DEMO.md`](DEMO.md).

## Design goals (in priority order)

1. **Auditability** — every state transition emits an event, every tool
   invocation logs its attempts. A retroactive postmortem on any task
   should be reconstructable from the database alone.
2. **Fail-safe** — gates fail closed, not open. A gate that crashes does
   not silently allow the change through.
3. **Language- and repo-agnostic primitives** — components that depend
   on a specific repo (Android paths, Kotlin syntax) belong in plug-ins,
   not in the core.
4. **Adversarial-aware** — the LLM is *not* a trusted collaborator.
   Treat every gate as something the LLM will try to game.

## Pipeline stages

Each task transitions through these workflow stages, persisted on the
`Task` row and emitted as `TASK_STATUS_CHANGED` events:

| Stage | What happens |
|---|---|
| `INTAKE` | Persist the user request, run semantic translation. |
| `PLANNING` | Planner LLM produces structured `Plan` (objective, must_touch_files, steps, …). |
| `KNOWLEDGE` | Build `EvidenceBundle` from KB FTS5 retrieval. |
| `ACTION` / `EXECUTION` | Invoke tools (codegen, Jira write, Slack post, internal API/DB). |
| `REVIEW` | Run gate stack on the produced diff. |
| `APPROVAL` | Human-in-loop sign-off when policy requires it. |
| `DONE` | Final outcome persisted; writeback or summary emitted. |

## Components in the develop pipeline (the most complex flow)

### Semantic translator
- Provider: MiniMax-M2.7 by default; mock fallback.
- Inputs: raw user request.
- Outputs: `intent`, `normalized_request`, `grounding_terms`, `source_name`,
  `search_queries`. Stored in `task.translation_json`.

### Planner
- Provider chain: `claude_code` → `codex` → `anthropic` → `deepseek`
  → `openai` → `minimax` → `ollama` → `mock`.
- Outputs structured plan with `objective`, `affected_code_locations`,
  `must_touch_files`, `expected_new_files`, `steps[]`, `requires_approval`.

### EvidenceBundle (Plan A + B2)
- File: [`apps/backend/app/services/evidence_bundle.py`](../apps/backend/app/services/evidence_bundle.py).
- Inputs: planner anchors + grounding terms + `source_tree` path
  + DB session (for FTS5).
- Strategy per anchor:
  1. **`fts5_and`** — tokenize anchor (CamelCase split + drop stopwords),
     query knowledge_document_fts with `(t1 AND t2 ...) OR concat(t1t2)`.
     The `OR concat` arm catches CamelCase compounds that the porter
     tokenizer keeps as a single token.
  2. **`fts5_or`** — fallback for recall when AND returns 0.
  3. **`substring`** — last-resort scan for files not in the KB index.
- Output: `EvidenceBundle(verdict, must_touch_files, anchor_hits,
  anchor_strategy, coverage_score, ...)`.
- **B2 rule**: when `coverage_score == 0` AND `planner_must_touch == []`,
  verdict becomes `"insufficient"` → orchestrator fail-closes the task.

Empirical impact on 24 dogfood tasks:

| Metric | Before | After Plan A |
|---|---|---|
| Anchor recall | 9.2% (14/153) | 91.5% (140/153) |
| Tasks with 0 anchor hits | 79% (19/24) | 0% (0/24) |

### Codegen
- File: [`apps/backend/app/services/codegen.py`](../apps/backend/app/services/codegen.py).
- Provider chain configurable per task.
- For Kotlin/`.kts` context, prompt is augmented with
  `CODEGEN_KOTLIN_GUIDANCE` (8 stable Kotlin constraints) — language-
  scoped prompt augmentation.
- Self-validation: `git apply --check` + per-language syntax check
  (`py_compile` for Python, `node --check` for JS). Single retry on
  failure.
- Output: unified diff string + `files_changed` list.

### Gate stack (post-codegen, pre-action)

Order matters: cheap deterministic checks run before heavy compile.

1. **feature_presence_check (G2)**
   - File: [`apps/backend/app/services/feature_presence_check.py`](../apps/backend/app/services/feature_presence_check.py).
   - Token derivation strict: only CamelCase / snake_case identifiers
     from `objective` + `grounding_terms` + `must_touch` basenames.
     Generic English (`'home'`, `'address'`, `'user'`, `'Implement'`,
     `'Jira'`, …) dropped.
   - Scan scope: **diff-added lines** (post strip-comments), not full
     file body. Pre-existing identifiers cannot satisfy the gate.
   - Threshold: `≥ ceil(0.5 * len(required_tokens))` per file.
2. **SymbolGraph ref-validity**
   - File: [`apps/backend/app/services/symbol_graph/`](../apps/backend/app/services/symbol_graph/).
   - For every changed file, walk every `Ref` in it, verify a
     matching `Decl` exists somewhere in the repo. Distinguishes
     `no_decl_found` from `kind_mismatch`.
   - Plug-in registry maps file extensions to extractors. Files
     without a registered extractor are gracefully skipped.
3. **compile_gate**
   - Language-router decides between Gradle (Android), `python -m
     compileall` (Python), `node --check` (JS), etc.
   - Multi-round repair: gate emits errors → codegen receives them
     as repair prompt → re-runs (configurable rounds, default 3).
4. **spec_conformance**
   - Negative-keyword detection (planner anchors that should NOT
     appear in the diff).
5. **runtime_validation** (semantic post-checks)
6. **reservations_review** (legacy reservation pattern detector)

Each gate emits a single event; failures call `_fail_develop_pipeline`
which sets `TaskStatus.FAILED` with `latest_result_json` carrying
gate-specific diagnostics.

### SymbolGraph deeper dive

The framework's contract is the `SymbolExtractor` Protocol:

```python
class SymbolExtractor(Protocol):
    @property
    def language(self) -> str: ...

    def extract(self, *, path: str, source: bytes) -> ExtractedSymbols:
        ...
```

`ExtractedSymbols` is a tuple of `Decl`s and `Ref`s. A `Decl` has a
`name`, `kind` (extractor-defined string: `"function"`, `"class"`,
`"string"`, `"drawable"`, `"id"`, …), `file`, `line`. A `Ref` has a
`name`, `expected_kind` (None → match any kind, else must match).

Built-in plug-ins:

| Extractor | Backing parser | Captures |
|---|---|---|
| `python_extractor.PythonExtractor` | stdlib `ast` | top-level def/class/var decls + import / from-import refs |
| `kotlin_extractor.KotlinExtractor` | tree-sitter-kotlin | class / function / property decls + import refs |
| `xml_extractor.XmlExtractor` | lxml + regex | Android `<string>`/`<color>`/… decls + `@string/X` / `@drawable/X` refs |

The gate logic (`validate_refs`) is generic over these — Android XML
refs and Python imports are checked by the same code.

## Memory

File: [`apps/backend/app/services/memory.py`](../apps/backend/app/services/memory.py).

Failures of class `REVIEW_FAILED`, `COMPILE_FAILED`,
`FAILURE_DIAGNOSIS_GENERATED`, `TOOL_FAILED`, `TOOL_TIMED_OUT` are fed
to `MemoryService.maybe_record_gate_event`. A judge LLM (configurable;
mock fallback) decides whether the failure is novel and worth storing;
stored memories include scope (`tool:jira`, `gate:compile_gate`),
observation text, and resolution link. They are surfaced via FTS5 to
the planner on similar future tasks.

## Tool runtime + governance

File: [`apps/backend/app/tools/gateway.py`](../apps/backend/app/tools/gateway.py).

- Each registered tool has `permission_category` (`open` / `team_lead`
  / `manager` / `system` / `approval_required`). Approval-required tools
  raise `ToolApprovalRequired` on first call; the orchestrator creates
  an `Approval` row and pauses the task.
- Retry policy: `retryable=True` errors (5xx, network) loop with
  exponential backoff `0.5 * 2^attempt + jitter`, capped at 8s.
- All attempts logged in `ToolExecution.attempt_log_json`.

## Observability

- `Event` table: full state-transition + tool-invocation history.
- `ToolExecution`: per-call latency, retry count, payload, response.
- `LlmUsage`: token counts and provider/model per LLM call.
- `/health`: 1h failure-rate, last-successful-task age, worker queue
  depth, recent external-API failure counts by provider.

## Why this design and not Devin / Aider / Claude Code directly

Off-the-shelf coding agents are:
- Interactive (require human-in-loop)
- Single-tenant (no governance / RBAC)
- Auditless (no `Event` log of every state transition)
- Single-provider

This platform sits *above* them as a multi-tenant, governed orchestration
layer. The codegen step itself can dispatch *to* Claude Code or Codex CLI,
but the platform owns the audit trail, the gate stack, the multi-provider
fallback chain, and the memory feedback loop.

## Where the line is

This platform does **not** try to autonomously ship arbitrary cross-file
features (e.g. multi-file Android Maps integration). That problem is
frontier in 2026 — even Anthropic's Claude Code defaults to interactive.
Instead, this platform provides:
- **Gates that catch** when codegen produces shell code or unresolved refs.
- **Telemetry that explains** failure modes for human triage.
- **Architectural primitives** that any new repo, language, or task type
  can plug into.
