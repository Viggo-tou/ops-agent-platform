"""RAG benchmarking framework.

Reproducible A/B harness for retrieval strategies over the project's
QA benchmark JSONL datasets. Each strategy implements the
``RetrievalStrategy`` Protocol from
``app.services.rag_bench.strategies.base``; the harness in
``runner.py`` evaluates them with consistent metrics
(recall@K, MRR, citation precision).

Public entrypoints:
    from app.services.rag_bench import (
        load_questions, evaluate_strategy, run_all,
        RetrievalResult, EvaluationReport,
    )

The shipped strategies live under ``strategies/``:
    * fts5_baseline — current production Plan A retriever
    * (later) dense_embedding, hybrid_rrf, cross_encoder_rerank, ...
"""
from app.services.rag_bench.dataset import (
    BenchmarkQuestion,
    load_questions,
)
from app.services.rag_bench.metrics import (
    EvaluationReport,
    QuestionResult,
    RetrievalResult,
    recall_at_k,
    mean_reciprocal_rank,
    citation_precision,
)
from app.services.rag_bench.runner import evaluate_strategy, run_all

__all__ = [
    "BenchmarkQuestion",
    "load_questions",
    "EvaluationReport",
    "QuestionResult",
    "RetrievalResult",
    "recall_at_k",
    "mean_reciprocal_rank",
    "citation_precision",
    "evaluate_strategy",
    "run_all",
]
