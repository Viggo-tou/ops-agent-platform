# T-KB-FTS5-INDEX — SQLite FTS5 lexical retrieval (replaces hand-rolled BM25-ish scorer)

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P1 — Phase 3.3 foundation; rag_card and hybrid fast-path build on this)
**Priority:** P1 (Stage 15)
**Created:** 2026-04-29
**Branch:** `feat/kb-fts5-index` based on `checkpoint/pre-reclassify` HEAD `84035a8`

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform.
Backend root: `apps/backend/`. Run from there.

## Goal

Replace the hand-rolled per-query token-counting scorer in `KnowledgeService._score_document` (apps/backend/app/services/knowledge.py:948) with a SQLite **FTS5** virtual table over `knowledge_document.content + title + relative_path`. Keep the existing route preferences (extension bonus, path-term bonus, phrase bonus) as a **rerank pass** on top of FTS5's BM25 ranking.

This is foundation work for two follow-up tickets (`T-KB-RAG-CARDS-OFFLINE`, `T-KB-HYBRID-FAST-PATH`) which both use FTS5 as their index substrate.

## Background — what's there today

```
$ grep -nE "_score_document|_tokenize" apps/backend/app/services/knowledge.py
195:def _tokenize(text: str) -> list[str]
309:    query_tokens = _tokenize(query)
338:    scored = self._score_document(...)
948:    def _score_document(...)
961:        content_tokens = Counter(_tokenize(content_sample))
```

- Current path: `search_repositories` calls `_route_query` → loads `documents = list(self.db.scalars(documents_stmt))` (linear scan of all source docs) → iterates `_score_document` per doc → tops by `top_k`.
- `_score_document` does: token Counter on `content[:40_000]`, sums `path_hits * 5 + semantic_hits + phrase_bonus + extension_bonus + path_term_bonus`.
- Not actually BM25 — just additive hand-rolled bonuses. Slow on bigger repos (linear in N_docs * content_chars).

## Design

### A. New SQLite FTS5 virtual table

```sql
-- Created via SQLAlchemy raw SQL in core/db.py init or a migration step
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_document_fts USING fts5(
    document_id UNINDEXED,
    source_name UNINDEXED,
    relative_path,
    title,
    content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
```

- `document_id` and `source_name` are UNINDEXED (used for joining back, not for matching).
- `relative_path`, `title`, `content` are indexed.
- Tokenizer: porter (English stemming) + unicode61 (broad coverage) + diacritic stripping. Adequate for English code-mostly repos; we can tune later.

### B. Population: keep in sync with `KnowledgeDocument`

Two write paths today populate `KnowledgeDocument`:
1. `_sync_single_repository` (line 521) — repo scan ingest
2. `upload_documents` (line 707) — file/zip upload ingest

Both must also write to the FTS5 table. Add a small helper:

```python
def _upsert_fts(db: Session, *, document_id: str, source_name: str,
                relative_path: str, title: str, content: str) -> None:
    """Sync the FTS5 row for this document. FTS5 doesn't have a real
    UPSERT; do delete+insert keyed by document_id."""
    db.execute(text("DELETE FROM knowledge_document_fts WHERE document_id = :id"),
               {"id": document_id})
    db.execute(text("INSERT INTO knowledge_document_fts (document_id, source_name, "
                    "relative_path, title, content) VALUES (:id, :src, :rp, :t, :c)"),
               {"id": document_id, "src": source_name, "rp": relative_path,
                "t": title, "c": content})
```

Call this helper after each insert/update of `KnowledgeDocument` in:
- `_sync_single_repository` after both the new-document insert (line 559) and the content_hash-changed update (line 571)
- `upload_documents` after both insert (line 730) and update (line 740)
- `delete_document` and `delete_source` must also delete from FTS5 to avoid stale rows

### C. Backfill on startup

Existing DBs already have `KnowledgeDocument` rows but no FTS5 table. After the `CREATE VIRTUAL TABLE IF NOT EXISTS`, do a one-time backfill:

```python
def _backfill_fts_if_empty(db: Session) -> int:
    """Populate FTS5 from existing KnowledgeDocument if FTS5 is empty.
    Idempotent: returns rows inserted."""
    count = db.execute(text("SELECT COUNT(*) FROM knowledge_document_fts")).scalar() or 0
    if count > 0:
        return 0
    inserted = 0
    for doc in db.execute(select(KnowledgeDocument)).scalars():
        _upsert_fts(db, document_id=doc.id, source_name=doc.source_name,
                    relative_path=doc.relative_path, title=doc.title, content=doc.content)
        inserted += 1
    db.commit()
    return inserted
```

Wire into `core/db.py` init step so it runs once after table creation.

### D. Query path: FTS5 → top candidate set → rerank

Replace the linear scan in `search_repositories`:

```python
# OLD (around line 287-337):
documents = list(self.db.scalars(documents_stmt))
... iterate _score_document on every document ...

# NEW:
fts_query = _build_fts5_query(query_tokens, expanded_tokens)
candidates = self._fts5_topk(
    source_names=source_names,
    fts_query=fts_query,
    pool_size=max(top_k * 5, 20),  # over-fetch; rerank narrows
)
scored_documents = [
    self._score_document(
        document=document,
        query=query,
        query_tokens=query_tokens,
        expanded_tokens=expanded_tokens,
        route=route,
    ) for document in candidates
]
```

The rerank STILL applies the route bonuses (path / phrase / extension) so query routing keeps working. The change is the **candidate selection**: FTS5 BM25 instead of linear scan + hand-rolled scoring on every doc.

### E. FTS5 query builder

```python
def _build_fts5_query(query_tokens: list[str], expanded_tokens: set[str]) -> str:
    """Build an FTS5 MATCH expression from tokens. Use OR semantics with
    column boosting (relative_path 3x, title 2x, content 1x)."""
    safe_tokens = [_escape_fts_token(t) for t in {*query_tokens, *expanded_tokens} if t and len(t) >= 2]
    if not safe_tokens:
        return "NEAR(unlikely-token-12345)"  # produces empty result safely
    or_expr = " OR ".join(safe_tokens)
    # Column weighting via FTS5 column qualifier:
    return f"(relative_path:({or_expr}) OR title:({or_expr}) OR content:({or_expr}))"


def _escape_fts_token(token: str) -> str:
    """FTS5 uses double-quotes for literal strings; escape internal quotes."""
    safe = token.replace('"', '""')
    return f'"{safe}"'
```

The `bm25(fts_table, w_path, w_title, w_content)` function does column-weighted BM25 scoring at query time. We pass weights so path matches get boosted.

### F. Top-K query

```python
def _fts5_topk(self, *, source_names: list[str], fts_query: str, pool_size: int) -> list[KnowledgeDocument]:
    placeholders = ",".join(f":s{i}" for i in range(len(source_names)))
    sql = text(f"""
        SELECT fts.document_id
        FROM knowledge_document_fts fts
        WHERE fts.knowledge_document_fts MATCH :q
          AND fts.source_name IN ({placeholders})
        ORDER BY bm25(knowledge_document_fts, 3.0, 2.0, 1.0)  -- (path, title, content)
        LIMIT :k
    """)
    params = {"q": fts_query, "k": pool_size}
    for i, name in enumerate(source_names):
        params[f"s{i}"] = name
    rows = self.db.execute(sql, params).all()
    if not rows:
        return []
    ids = [row[0] for row in rows]
    docs = self.db.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.id.in_(ids))
    ).scalars().all()
    # preserve FTS5 order
    by_id = {d.id: d for d in docs}
    return [by_id[i] for i in ids if i in by_id]
```

### G. Trace fields (for benchmark debug)

Add to `KnowledgeAnswerTrace`:

```python
fts5_pool_size: int | None = Field(default=None, description="FTS5 candidate pool size before rerank")
fts5_match_count: int | None = Field(default=None, description="FTS5 actual match count returned")
fts5_query: str | None = Field(default=None, description="FTS5 MATCH expression used")
```

So benchmark traces show whether FTS5 found good candidates or whether the rerank was operating on a starved pool.

### H. Setting

```python
# apps/backend/app/core/config.py
knowledge_fts5_enabled: bool = True  # safe default; flip to False to roll back
knowledge_fts5_pool_multiplier: int = 5  # candidate pool = max(top_k * this, 20)
```

The `enabled=False` switch falls back to the old linear-scan path, so we can A/B test cleanly.

## Files to edit

1. `apps/backend/app/core/db.py` — add `CREATE VIRTUAL TABLE` + backfill call after existing table creation
2. `apps/backend/app/services/knowledge.py` — add `_upsert_fts` helper + wire into 4 call sites (sync insert, sync update, upload insert, upload update); add delete-from-fts in `delete_document` / `delete_source`; add `_build_fts5_query`, `_escape_fts_token`, `_fts5_topk`; replace candidate-selection in `search_repositories` (gated by `knowledge_fts5_enabled`)
3. `apps/backend/app/core/config.py` — 2 new settings
4. `apps/backend/app/schemas/knowledge.py` — 3 new optional trace fields
5. `apps/backend/tests/services/test_knowledge_fts5.py` — NEW (8+ tests)

## Tests

1. `test_create_fts_table_idempotent` — creating twice doesn't error
2. `test_upsert_fts_inserts_and_updates` — insert then re-insert same doc_id yields one row with new content
3. `test_backfill_populates_existing_documents` — empty FTS5 + N existing KnowledgeDocument → backfill yields N FTS rows
4. `test_backfill_skips_when_already_populated` — non-empty FTS5 → no-op
5. `test_search_uses_fts5_when_enabled` — search returns docs that match query tokens, ordered by BM25
6. `test_search_falls_back_to_linear_scan_when_fts5_disabled` — `knowledge_fts5_enabled=False` → old code path
7. `test_search_respects_source_filter` — multi-source DB, query filtered to one source returns only that source's docs
8. `test_delete_document_removes_fts_row` — deleting KnowledgeDocument deletes corresponding FTS5 row
9. `test_fts5_pool_multiplier_setting_applied` — pool_size = max(top_k * setting, 20)
10. `test_fts5_query_handles_empty_token_set` — query with no usable tokens returns empty without raising

## Acceptance criteria

- `python -m compileall app` clean
- 10+ new tests pass
- All existing `pytest tests/` still passes (sanity — no regression)
- Manual smoke from Tomonkyo bash:
  - Backend starts; `knowledge_document_fts` table exists; backfill populates it from current `knowledge_document` rows.
  - `curl -G 'http://127.0.0.1:8000/api/knowledge/search' --data-urlencode 'query=admin login'` returns results.
  - `curl ... 'query=where is firebase configured'` returns Firebase-related docs in top-3.
- Trace shows non-null `fts5_pool_size` and `fts5_match_count` after a search request.

## Out of scope

- Removing the old linear-scan code path entirely (keep gated by setting for safe rollback)
- Tuning BM25 column weights beyond initial (3.0, 2.0, 1.0)
- Multi-token NEAR / phrase queries beyond OR-of-tokens
- Updating existing trace consumers (frontend) to display the new fields
- Anything related to `rag_card` or hybrid fast-path (separate tickets)
- Running the QA benchmark against the change (separate stage entry; harness now hardened, will give truthful numbers)

## Workflow

```
codex exec --full-auto --sandbox workspace-write -C "D:/项目/ops-worktrees/kb-fts5" -c model_reasoning_effort=medium - < docs/ai/tasks/T-KB-FTS5-INDEX.md
```

Worktree: NEW worktree `D:/项目/ops-worktrees/kb-fts5` on new branch `feat/kb-fts5-index` based on `checkpoint/pre-reclassify` HEAD `84035a8`.

## Why this is foundation work, not a quality lever

Codex's Stage 13 verdict: "cap tuning mainly moves errors between tiers". The actual quality levers are `rag_card` (semantic file-level summaries) and hybrid fast-path (right-tool-for-right-query). Both need an indexable table to store cards / fast-rank candidates. FTS5 is that index.

Expected mean delta from this ticket alone: small (single-digit, possibly negative on some tiers due to BM25 ranking different from the hand-rolled scorer). The point is the **substrate**, not the immediate score lift.
