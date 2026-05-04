# T-KB-RETRIEVAL-CACHE — KB retrieval cache (cost / latency reduction)

<!-- Effort: 3-5 days -->
<!-- Executor: DeepSeek-V4-Pro via deepseek_agent.py wrapper -->
<!-- Stage: 28 -->
<!-- Phase: 3.5 ish (PreIndex precursor) -->

**Status:** todo (Stage 27 parallel)
**Priority:** A (cost + latency for repeat dogfood, not moat)
**Created:** 2026-05-04
**Branch:** `feat/kb-retrieval-cache` based on `checkpoint/pre-reclassify@be97b03`

## Override

Skip session-boundary protocol. Workdir auto-set; no cd. MAX 12 rounds. Call final_report by round 11 latest.

## Background

Same KB query (e.g. dogfood replay of P69-19) currently re-runs full retrieval pipeline every time:
1. RAG keyword/FTS5/BM25 score (cheap)
2. Cards retrieval (cheap, offline-built)
3. CC agentic (LLM rounds, expensive)
4. Synth answer (MM API call, expensive)

For repeated dogfood (same query within minutes/hours), 1+2 are deterministic and 3+4 are stochastic. Caching 1+2's output saves CPU; caching 3+4's output saves $$ + wall-clock.

## Goal

Add a content-hash-keyed cache layer between query input and retrieval output. Hit cache → skip retrieval pipeline; miss → run normally + populate cache.

## Design

### Cache key

`hash(query_text + knowledge_source_name + relevant_settings)` where relevant_settings include retrieval mode flags (e.g., cc_agentic_enabled, fts5_enabled, top_k). NOT including request_id or actor_name (those vary per task but shouldn't invalidate cache).

### Cache value

Pickled or JSON-serialized retrieval result: `(citations, claims, answer_text)` plus metadata `(cached_at, hit_count)`.

### Storage

NEW table `knowledge_retrieval_cache`:

```python
class KnowledgeRetrievalCache(Base):
    __tablename__ = "knowledge_retrieval_cache"
    cache_key = Column(String(64), primary_key=True)  # SHA256 hex
    query_hash_inputs = Column(String(2000), nullable=False)  # for debug: raw inputs that hashed
    response_json = Column(Text, nullable=False)  # serialized retrieval result
    cached_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_hit_at = Column(DateTime, nullable=True)
    hit_count = Column(Integer, nullable=False, default=0)
    ttl_seconds = Column(Integer, nullable=False, default=3600)  # 1h default
```

### Cache layer

`apps/backend/app/services/knowledge_retrieval_cache.py` (NEW):

```python
class RetrievalCache:
    def __init__(self, db, settings): ...

    def get(self, query: str, source_name: str) -> dict | None:
        """Return cached result if fresh, else None."""
        key = self._compute_key(query, source_name)
        row = self.db.query(KnowledgeRetrievalCache).get(key)
        if row is None:
            return None
        if (datetime.utcnow() - row.cached_at).total_seconds() > row.ttl_seconds:
            return None  # stale
        # Update hit metadata (best-effort, not in transaction)
        row.last_hit_at = datetime.utcnow()
        row.hit_count += 1
        self.db.commit()
        return json.loads(row.response_json)

    def put(self, query: str, source_name: str, response: dict, ttl: int | None = None):
        key = self._compute_key(query, source_name)
        # Upsert
        ...

    def _compute_key(self, query: str, source_name: str) -> str:
        normalized = query.strip().lower() + "||" + source_name
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def invalidate_source(self, source_name: str) -> int:
        """Drop all cache entries for a source (e.g. after KB reindex). Returns count."""
        ...
```

### Hook into retrieval

`apps/backend/app/services/knowledge.py` (find the retrieval entry point — likely `KnowledgeService.synthesize_answer` or similar):

```python
# At the top of retrieval:
if self.settings.knowledge_retrieval_cache_enabled:
    cached = self._retrieval_cache.get(query, source_name)
    if cached:
        emit_event(EventType.KNOWLEDGE_CACHE_HIT, ...)
        return KnowledgeAnswerResult.from_cached(cached)

# ... normal retrieval pipeline ...

# At the bottom, before return:
if self.settings.knowledge_retrieval_cache_enabled:
    self._retrieval_cache.put(query, source_name, result.to_dict())
```

Add `KNOWLEDGE_CACHE_HIT` to `apps/backend/app/core/enums.py:EventType`.

### Configuration

`apps/backend/app/core/config.py`:

```python
knowledge_retrieval_cache_enabled: bool = True
knowledge_retrieval_cache_ttl_seconds: int = 3600
knowledge_retrieval_cache_max_entries: int = 1000
```

Env: `OPS_AGENT_KNOWLEDGE_RETRIEVAL_CACHE_*`.

### Invalidation

When `/api/knowledge/sync` is called for a source: invalidate all cache entries for that source. Otherwise updated KB reindexes return stale answers.

## Files to edit

| File | Change |
|---|---|
| `apps/backend/app/models/knowledge_retrieval_cache.py` | NEW (~30 lines) |
| `apps/backend/app/services/knowledge_retrieval_cache.py` | NEW (~120 lines) |
| `apps/backend/app/services/knowledge.py` | hook get/put around retrieval entry, hook invalidate on sync |
| `apps/backend/app/core/db.py` | add table in ensure_local_schema |
| `apps/backend/app/core/config.py` | 3 settings |
| `apps/backend/app/core/enums.py` | KNOWLEDGE_CACHE_HIT EventType |
| `apps/backend/tests/services/test_knowledge_retrieval_cache.py` | NEW (~6 unit tests) |

## Acceptance

1. `compileall` clean
2. Unit tests:
   - `test_cache_miss_returns_none`
   - `test_cache_put_then_get_returns_value`
   - `test_cache_get_after_ttl_returns_none`
   - `test_cache_invalidate_source_removes_entries`
   - `test_cache_disabled_via_env_flag`
   - `test_cache_key_normalization` (whitespace/case insensitive)
3. Integration:
   - Run process_question scenario twice with same query; second run shows cache_hit event + faster response
   - Submit `/api/knowledge/sync` triggers invalidation (verify cache row count drops)
4. `OPS_AGENT_KNOWLEDGE_RETRIEVAL_CACHE_ENABLED=false` disables cleanly

## Out of scope

- Cross-actor cache sharing (single-tenant; v1 shares cache regardless of actor)
- LRU eviction (defer; simple TTL + max_entries cap suffice for v1)
- Cache stats panel UI (later)
- Synth answer caching beyond retrieval (cache stores (citations, claims, answer); synth not separately cached)

## DeepSeek-specific dispatch hints

You are running via `deepseek_agent.py`. Strict round budget: 12 rounds.

Order:
1. `read_file` `apps/backend/app/services/knowledge.py` (just first 50 lines + use grep for "synthesize" / "search_repositories" entry points)
2. `read_file` an existing model file (e.g., `apps/backend/app/models/llm_usage.py`) to match table style
3. Use `replace_in_file` for orchestrator hooks (small additions to existing methods)
4. Use `write_file` for new files
5. Validate: `python -m compileall apps/backend/app/services/knowledge_retrieval_cache.py apps/backend/tests/services/test_knowledge_retrieval_cache.py`
6. Run tests: `python -m pytest apps/backend/tests/services/test_knowledge_retrieval_cache.py -x -q`
7. Commit message:
   ```
   feat(knowledge): T-KB-RETRIEVAL-CACHE — content-hash-keyed retrieval cache (Stage 28)
   
   Same query on same KB currently re-runs full retrieval (RAG + cards + CC
   agentic + synth). Add SHA256-keyed cache layer with 1h TTL. Cache hit
   skips pipeline; miss runs + populates. Invalidate on KB sync.
   
   Tests: 6 unit + 1 integration. Generated by DeepSeek-V4-Pro via
   deepseek_agent.py wrapper.
   ```

8. Call `final_report`. **Do NOT skip final_report.**

## Hard rules

- Edit ONLY listed files
- DO NOT modify orchestrator/service.py (Stage 27 may be touching it concurrently in different worktree)
- DO NOT change synthesis logic / claim_extraction / cards
- DO NOT push, DO NOT merge
