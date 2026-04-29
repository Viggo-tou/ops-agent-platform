# T-KB-RAG-CARDS-OFFLINE — LLM-generated file-level cards (B/C/D tier quality lever)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium-high -->
<!-- Executor: codex -->

**Status:** todo (P0 — Phase 3.3 main quality lever; expected mean +10-15)
**Priority:** P0 (Stage 16; blocks reaching mean 60+)
**Created:** 2026-04-29
**Branch:** `feat/kb-rag-cards` based on `feat/kb-fts5-index` (depends on FTS5 substrate)

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

For every indexed `KnowledgeDocument`, generate a short LLM-written **markdown card** describing:
- What the file does (1 sentence)
- Key symbols exported (function names, class names, components)
- Relationships (what other files it imports / is imported by, key external libs)
- Domain category (auth / dashboard / data-fetching / utility / route / etc)

Store the card per-document, mirror to FTS5 so retrieval BM25 picks up card text. Synthesis sees `(path, title, content_excerpt, card_text)` instead of `(path, title, content_excerpt)`.

**Why this is the lever** (per Stage 15 strategic analysis): A-tier is at dataset ceiling; B-tier ("how does X work") and D-tier ("trace the X pipeline") need structural understanding the raw content sample doesn't give. Cards encode that structure once-per-file, reused across every query that hits the file.

Expected per-tier delta from cards alone:
- A: +0 to +3 (might help with "default export" type literal keypoints)
- B: **+15 to +25** (the big win — directly addresses "how does X work")
- C: +5 to +10 (better grounding for "which pages reuse X")
- D: +5 to +12 (helps but doesn't fully solve multi-hop)
- Expected mean: **49.65 → 58-62**

## Background

After T-KB-FTS5-INDEX:
- `KnowledgeDocument` has the raw file content
- `knowledge_document_fts` is FTS5 with (path, title, content)
- Search returns top-N candidates ranked by FTS5 BM25, then re-scored by `_score_document`
- Synthesis gets per-citation snippets (capped at `knowledge_synthesis_max_snippet_chars`)

The synthesizer currently sees raw code with no semantic summary. For "how does Login work?", it gets line 1-65 of Login.js (imports + JSX header) and has to infer the answer. Cards give it "Login.js: React component handling admin auth via Firebase, exports `Login` as default, validates email/password, calls `handleLogin` to authenticate against /admin node, redirects to /Dashboard on success" — directly answer-shaped.

## Design

### A. New table: `knowledge_card`

```sql
CREATE TABLE knowledge_card (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE,  -- one card per document
    source_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    card_text TEXT NOT NULL,           -- markdown card body
    card_version TEXT NOT NULL,        -- e.g. "v1-claude-haiku"; bump when prompt or model changes
    model_name TEXT NOT NULL,          -- which LLM wrote it
    generated_at TIMESTAMP NOT NULL,
    content_hash TEXT NOT NULL,        -- hash of doc.content at gen time; regen if hash changes
    FOREIGN KEY (document_id) REFERENCES knowledge_document(id) ON DELETE CASCADE
);
CREATE INDEX ix_knowledge_card_source ON knowledge_card(source_name);
CREATE INDEX ix_knowledge_card_doc ON knowledge_card(document_id);
```

### B. Extend FTS5 to mirror card text

Add `card_text` column to `knowledge_document_fts`:

```sql
-- FTS5 doesn't support ALTER TABLE ADD COLUMN. Drop + recreate (idempotent).
DROP TABLE IF EXISTS knowledge_document_fts;
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_document_fts USING fts5(
    document_id UNINDEXED,
    source_name UNINDEXED,
    relative_path,
    title,
    content,
    card_text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
```

Backfill rebuilds `card_text` from `knowledge_card.card_text` JOINed by `document_id`.

### C. Card builder — `apps/backend/scripts/build_cards.py`

```
python -m scripts.build_cards \
  --source-name hosteddashboard \
  --backend-url http://127.0.0.1:8004 \
  --provider minimax \
  --model MiniMax-M2.7 \
  --concurrency 5 \
  --skip-existing
```

Behaviour:
- For each `KnowledgeDocument` not yet carded (or whose `content_hash` changed): call LLM with content + the card prompt template
- Parallelize at concurrency level (default 5)
- Save card to `knowledge_card` + update FTS5 row
- Print progress: `[N/M] doc_id=... model=... latency=...s tokens=...`
- Final summary: total cards generated, total tokens, total cost estimate

Card prompt template (in `apps/backend/app/services/cards.py`):

```
You are summarizing a single source file from a code repository.

File: {relative_path}
Lines: {line_count}
Language: {language}

Content:
```
{content[:8000]}
```

Write a markdown card of at most 400 words describing:
1. **One-sentence purpose**: what this file does
2. **Key exports**: name and brief role of each exported symbol (function/class/component)
3. **Imports of note**: which external libs and which sibling files it depends on
4. **Domain**: one tag from [auth, dashboard, data-fetching, ui-component, page, route, util, config, test]
5. **Notable patterns**: any non-obvious responsibility (e.g. "centralizes Firebase init", "wraps Bootstrap modal")

Format:

```markdown
**File**: {relative_path}
**Purpose**: ...
**Exports**: ...
**Depends on**: ...
**Domain**: ...
**Notes**: ...
```

Be terse. No filler. If content is empty or non-code, output: `**File**: {relative_path}\n**Purpose**: (empty / non-code file)`.
```

### D. Card invocation

```python
# apps/backend/app/services/cards.py

CARD_PROMPT_VERSION = "v1-card"

class CardGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, *, document: KnowledgeDocument) -> tuple[str, str]:
        """Return (card_text, model_name). Raises CardGenerationError on failure."""
        prompt = _build_card_prompt(document)
        # MiniMax httpx call (similar to knowledge_synthesis path)
        ...
```

### E. Search integration

In `_fts5_topk` query, BM25 already considers `card_text` because it's a column in the FTS5 table. No code change to ranking. Pool size stays the same (default 20).

In synthesis snippet building: the per-citation `snippet` field stays as raw content. **Add a new field `card_text`** to `KnowledgeCitation`:

```python
# apps/backend/app/schemas/knowledge.py
class KnowledgeCitation(BaseModel):
    ...existing fields...
    card_text: str | None = Field(default=None, description="LLM-generated card summary")
```

`KnowledgeService._build_citation` populates `card_text` by joining to `knowledge_card` for the document_id.

`KnowledgeSynthesizer._format_evidence` includes the card text BEFORE the snippet:

```
{relative_path}
[CARD]
{card_text}
[CONTENT]
{snippet[:limit]}
```

This gives synthesis the structural summary first, then code as backing.

### F. Trace fields

```python
class KnowledgeAnswerTrace(BaseModel):
    ...
    cards_available_count: int | None  # how many of the citations had cards
    cards_used_count: int | None       # how many were included in synthesis prompt
```

### G. Settings

```python
# apps/backend/app/core/config.py
knowledge_cards_enabled: bool = True
knowledge_cards_provider: str = "minimax"
knowledge_cards_model: str = "MiniMax-M2.7"
knowledge_cards_max_chars: int = 400 * 6  # ~400 words; safe upper bound
knowledge_cards_concurrency: int = 5
```

### H. Sync path integration

When `KnowledgeDocument` is inserted/updated and `knowledge_cards_enabled`:
- DON'T block sync on card generation (slow). Mark the doc as "needs card" (e.g. add a NULL row in knowledge_card or a `card_status` column).
- Background script `build_cards.py` picks up "needs card" docs and processes.
- Search still works without cards (FTS5 falls back to content-only ranking).

### I. Delete cleanup

Add to `delete_document` and `delete_source`:
```python
self.db.execute(text("DELETE FROM knowledge_card WHERE document_id = :id"), {"id": doc_id})
```

## Files to edit

1. `apps/backend/app/core/db.py` — `CREATE TABLE knowledge_card` + recreate FTS5 with `card_text` column
2. `apps/backend/app/models/knowledge_card.py` — NEW SQLAlchemy model
3. `apps/backend/app/services/cards.py` — NEW (~150 LOC: CardGenerator + prompt builder)
4. `apps/backend/scripts/build_cards.py` — NEW (~120 LOC: CLI for batch card gen)
5. `apps/backend/app/services/knowledge.py` — wire `card_text` into `_build_citation`; pass to FTS5 upsert; integrate into `_format_evidence` (via knowledge_synthesis)
6. `apps/backend/app/services/knowledge_synthesis.py` — `_format_evidence` includes card before content
7. `apps/backend/app/schemas/knowledge.py` — `KnowledgeCitation.card_text` + 2 trace fields
8. `apps/backend/app/core/config.py` — 5 new settings
9. `apps/backend/tests/services/test_cards.py` — NEW (8+ tests)

## Tests

1. `test_card_generator_produces_markdown_card` — mock LLM returns card; assert format
2. `test_card_generator_handles_empty_content` — empty doc → "(empty / non-code file)" card
3. `test_card_generator_handles_llm_error` — LLM raises → CardGenerationError, no DB write
4. `test_build_cards_skips_existing_when_flag_set`
5. `test_build_cards_regens_when_content_hash_changed`
6. `test_search_includes_card_text_in_fts5_match` — query that matches only card_text returns the doc
7. `test_synthesis_format_evidence_includes_card_block` — formatted evidence has [CARD] section before [CONTENT]
8. `test_delete_document_removes_card`
9. `test_trace_records_cards_available_and_used_counts`

## Acceptance criteria

- `python -m compileall app scripts` clean
- 9+ new tests pass
- Existing tests still pass (sanity)
- Manual smoke from Tomonkyo bash:
  - Backend starts; `knowledge_card` table exists; FTS5 has `card_text` column
  - `python -m scripts.build_cards --source-name hosteddashboard --provider minimax` populates cards for all 41 hosteddashboard docs in <10 min
  - `curl -G http://127.0.0.1:8004/api/knowledge/search --data-urlencode "query=how does login authenticate"` returns Login.js with `card_text` populated in citation
  - Trace shows `cards_available_count > 0`
- **Bench from Tomonkyo bash via `run_qa_benchmark` (PINNED claude_code, samples=3) shows mean ≥ 55** (vs 49.65 baseline; +5 minimum to confirm cards are working)

## Out of scope

- Card regeneration on prompt template change beyond bumping `card_version` (no auto-rebuild loop)
- Multi-language card variants
- Showing cards in frontend UI
- Cards for `cc_agentic` retrieved snippets (CC retrieval uses a different path)
- Hybrid fast-path (separate Stage 17 ticket)

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/kb-rag-cards" -c model_reasoning_effort=medium - < docs/ai/tasks/T-KB-RAG-CARDS-OFFLINE.md
```

Worktree: NEW worktree `D:/项目/ops-worktrees/kb-rag-cards` on new branch `feat/kb-rag-cards` based on `feat/kb-fts5-index` HEAD (after FTS5 lands and merges to checkpoint).

## D-009 contract

This ticket touches benchmark scores. Per D-009:
- Commit 1 = `feat(kb): T-KB-RAG-CARDS-OFFLINE` — implementation only
- Commit 2 = `bench(kb): record cards stage results and decision` — bench artifact + per-tier deltas

Do NOT commit either half until full 34Q bench (PINNED claude_code) shows valid results AND per-tier deltas are recorded in the stage-log.

## Why this is the right next step (not hybrid fast-path first)

Codex Stage 12 critique D5 said hybrid fast-path is P0 because 176s/Q is uninteractive. Both true — but cards is the bigger quality lever (B+D tier ~+30 mean potential vs hybrid's mostly latency play). Cards land first → measure → if mean clears 55, we have headroom to also improve latency via fast-path. If we did fast-path first, we'd be optimizing latency around a still-broken-quality system.

Order: cards (this ticket) → measure → hybrid fast-path → measure → reranker.
