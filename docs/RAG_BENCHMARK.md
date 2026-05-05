# RAG Retrieval Benchmark

A reproducible A/B harness for retrieval strategies over the project's
two real-world Android codebases (handymanapp, hosteddashboard).
Plug in a new strategy by adding one file under
[`apps/backend/app/services/rag_bench/strategies/`](../apps/backend/app/services/rag_bench/strategies/).

## Why

Most "we use RAG" claims are unmeasured. This harness:
- Locks today's production retrieval as a reference baseline (R-A).
- Defines reproducible IR metrics (recall@K, MRR, P@K).
- Persists per-question results so any strategy can be re-evaluated
  later without re-running prior baselines.
- Lets future work (dense embedding, hybrid, rerank, HyDE, AST chunking)
  be compared apples-to-apples on the same 60-question dataset.

## Dataset

| File | Questions | Source |
|---|---|---|
| `tests/benchmarks/qa_benchmark_dataset_handymanapp.jsonl` | 26 | Customer-facing Android app |
| `tests/benchmarks/qa_benchmark_dataset_hosteddashboard.jsonl` | 34 | Internal admin dashboard (React) |
| **Total** | **60** | |

Each row:
```json
{
  "id": "C-09",
  "tier": "A" | "B" | "C",
  "source_name": "handymanapp",
  "question": "...",
  "expected_citations": ["handymanapp/app/src/.../Foo.kt", ...],
  "expected_answer_keypoints": ["..."]
}
```

Tier indicates difficulty:
- **A**: single-file lookup, well-named symbol in question.
- **B**: multi-file but shallow (consumer + helper).
- **C**: cross-file or conceptual ("which fragments consume X?").

## Metrics

| Metric | Definition |
|---|---|
| **recall@K** | % of `expected_citations` appearing in top-K retrieved. |
| **MRR** | Mean reciprocal rank: 1/rank for the first hit per expected item, averaged. |
| **citation_p@K** | P@K — what fraction of the top-K retrieved are in `expected_citations`. |
| **ms/q** | Mean per-question latency. |

Path matching is suffix- and basename-tolerant — see
[`metrics._path_matches`](../apps/backend/app/services/rag_bench/metrics.py).

## Running the harness

```bash
cd apps/backend
python -m app.services.rag_bench.cli \
    --strategies fts5_baseline \
    --top-k 10
```

Output:
```
strategy                  n    r@1    r@3   r@10    mrr    p@5   ms/q
---------------------------------------------------------------------------
fts5_baseline            60  0.383  0.646  0.818  0.532   0.34    0.1

Per-question results -> tests/benchmarks/runs/rag_bench_<ts>.jsonl
```

Filter by tier (e.g. focus on hard questions):
```bash
python -m app.services.rag_bench.cli --strategies fts5_baseline --tier C
```

## R-A baseline (current production: Plan A FTS5)

Measured 2026-05-05 against the live KB.

### Overall

| Metric | Score |
|---|---|
| **recall@1** | **38.3%** |
| **recall@3** | **64.6%** |
| **recall@10** | **81.8%** |
| MRR | 0.532 |
| citation_p@5 | 34.0% |
| ms/q | 0.9 (synchronous SQLite FTS5) |

### Per tier

| Tier | n | recall@1 | recall@3 | MRR |
|---|---|---|---|---|
| A (single-file) | 18 | 50.0% | 72.2% | 0.624 |
| B (medium) | 18 | 55.6% | 86.1% | 0.694 |
| **C (cross-file)** | 14 | **14.9%** | **52.0%** | **0.367** |

**Reading**: Plan A is solid on Tier A/B (>70% recall@3) but weak on
Tier C (cross-file conceptual queries). The 50pp recall gap between
Tier A and Tier C is where dense embedding + reranking should help
most. This is the central hypothesis the R-B/C/D experiments will test.

## Strategy roadmap

| Tag | Strategy | Status | Hypothesis |
|---|---|---|---|
| **R-A** | FTS5 baseline (production Plan A) | ✅ measured | Reference |
| R-B | Dense embedding (sentence-transformers MiniLM) | planned | +Tier-C recall, slower |
| R-C | Hybrid: FTS5 BM25 + dense, RRF fusion | planned | Best of both, monotonic improvement |
| R-D | + cross-encoder rerank top-K | planned | Higher precision, more latency |
| R-E | + HyDE query rewriting (LLM hypothetical answer → search) | planned | Helps conceptual Tier C questions |
| R-F | AST-aware chunking (function/class boundaries) | planned | Tighter rerank candidates |

Each strategy plugs into the same harness; results land in
`tests/benchmarks/runs/rag_bench_<ts>.jsonl` for cross-comparison.

## Implementation notes

- **Strategy contract**:
  [`strategies.base.RetrievalStrategy`](../apps/backend/app/services/rag_bench/strategies/base.py)
  is a single-method Protocol — `retrieve(question, top_k) -> tuple[str, ...]`.
  Free to be a SQL query, an HTTP call, an in-memory FAISS index, etc.
- **Determinism**: the harness re-loads the dataset from disk and
  evaluates each strategy independently; no shared state.
- **Path tolerance**: matching is suffix + basename so a strategy that
  emits `app/src/.../Foo.kt` matches benchmark expectations of
  `handymanapp/app/src/.../Foo.kt`.

## Tests

```bash
python -m pytest tests/services/test_rag_bench.py -q
```

20 unit tests cover metric correctness, dataset loading, and
strategy-protocol conformance with mock perfect/wrong strategies.
