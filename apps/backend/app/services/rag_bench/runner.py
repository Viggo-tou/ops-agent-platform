"""Run a strategy against the benchmark and aggregate metrics."""
from __future__ import annotations

import time
from typing import Iterable

from app.services.rag_bench.dataset import BenchmarkQuestion
from app.services.rag_bench.metrics import (
    EvaluationReport,
    QuestionResult,
    aggregate,
    citation_precision,
    mean_reciprocal_rank,
    recall_at_k,
)
from app.services.rag_bench.strategies.base import RetrievalStrategy


def evaluate_strategy(
    *,
    strategy: RetrievalStrategy,
    questions: Iterable[BenchmarkQuestion],
    top_k: int = 10,
) -> EvaluationReport:
    """Run ``strategy`` over every question; aggregate the metrics."""
    per_q: list[QuestionResult] = []
    for q in questions:
        t0 = time.perf_counter()
        retrieved = tuple(strategy.retrieve(question=q, top_k=top_k))
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        expected = q.expected_files
        per_q.append(QuestionResult(
            question_id=q.id,
            tier=q.tier,
            source_name=q.source_name,
            expected=expected,
            retrieved=retrieved,
            elapsed_ms=elapsed_ms,
            recall_at_1=recall_at_k(retrieved, expected, k=1),
            recall_at_3=recall_at_k(retrieved, expected, k=3),
            recall_at_10=recall_at_k(retrieved, expected, k=10),
            mrr=mean_reciprocal_rank(retrieved, expected),
            citation_precision_at_5=citation_precision(
                retrieved, expected, k=5
            ),
        ))
    return aggregate(strategy.name, per_q)


def run_all(
    *,
    strategies: Iterable[RetrievalStrategy],
    questions: Iterable[BenchmarkQuestion],
    top_k: int = 10,
) -> list[EvaluationReport]:
    """Convenience: evaluate each strategy and return reports in order."""
    questions_list = list(questions)
    return [
        evaluate_strategy(
            strategy=s, questions=questions_list, top_k=top_k,
        )
        for s in strategies
    ]
