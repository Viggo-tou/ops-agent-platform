"""IR metrics for RAG benchmarking.

Stays simple on purpose — the harness needs to be reproducible, easy
to explain in a write-up, and not tied to any heavyweight ML library.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class RetrievalResult:
    """Output of a single strategy.retrieve(question) call."""
    question_id: str
    retrieved_paths: tuple[str, ...]  # ordered, top-K
    elapsed_ms: int
    strategy_name: str
    extras: dict = field(default_factory=dict)  # strategy-specific


@dataclass(frozen=True)
class QuestionResult:
    question_id: str
    tier: str
    source_name: str
    expected: tuple[str, ...]
    retrieved: tuple[str, ...]
    elapsed_ms: int
    recall_at_1: float
    recall_at_3: float
    recall_at_10: float
    mrr: float
    citation_precision_at_5: float


@dataclass(frozen=True)
class EvaluationReport:
    strategy_name: str
    n_questions: int
    mean_recall_at_1: float
    mean_recall_at_3: float
    mean_recall_at_10: float
    mean_mrr: float
    mean_citation_precision_at_5: float
    mean_elapsed_ms: float
    per_question: tuple[QuestionResult, ...]

    def to_summary_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "n": self.n_questions,
            "recall@1": round(self.mean_recall_at_1, 3),
            "recall@3": round(self.mean_recall_at_3, 3),
            "recall@10": round(self.mean_recall_at_10, 3),
            "mrr": round(self.mean_mrr, 3),
            "citation_p@5": round(self.mean_citation_precision_at_5, 3),
            "ms_per_q": round(self.mean_elapsed_ms, 1),
        }


# --- Path matching: tolerate both `app/src/X.kt` and basename match ---

def _path_matches(retrieved: str, expected: str) -> bool:
    """True if ``retrieved`` covers ``expected``: equal, suffix-tolerant,
    or basename-equivalent for paths that may differ in their workdir
    prefix between strategy outputs."""
    a = retrieved.replace("\\", "/").strip()
    b = expected.replace("\\", "/").strip()
    if a == b:
        return True
    if a.endswith("/" + b) or b.endswith("/" + a):
        return True
    if a.split("/")[-1] == b.split("/")[-1] and a.split("/")[-1]:
        return True
    return False


# --- Per-question metrics ---------------------------------------------------

def recall_at_k(
    retrieved: Iterable[str], expected: Iterable[str], *, k: int,
) -> float:
    """% of expected items appearing in the top-K retrieved.

    Returns 0.0 when expected is empty (denominator-zero guard).
    """
    expected_list = [e for e in expected]
    if not expected_list:
        return 0.0
    top_k = list(retrieved)[: max(k, 0)]
    hits = 0
    for e in expected_list:
        if any(_path_matches(r, e) for r in top_k):
            hits += 1
    return hits / len(expected_list)


def mean_reciprocal_rank(
    retrieved: Iterable[str], expected: Iterable[str],
) -> float:
    """Mean of reciprocal ranks over expected items.

    For each expected item, find its smallest rank in retrieved (1-indexed,
    using path matching). MRR = mean(1/rank) across expected; items not
    found contribute 0.
    """
    expected_list = [e for e in expected]
    if not expected_list:
        return 0.0
    ret_list = list(retrieved)
    rrs: list[float] = []
    for e in expected_list:
        rank = 0
        for i, r in enumerate(ret_list, start=1):
            if _path_matches(r, e):
                rank = i
                break
        rrs.append(1.0 / rank if rank else 0.0)
    return statistics.mean(rrs)


def citation_precision(
    retrieved: Iterable[str], expected: Iterable[str], *, k: int = 5,
) -> float:
    """Of the top-K retrieved, how many were expected? (P@K)

    Lower precision = more noise / false positives in the top results.
    """
    top_k = list(retrieved)[: max(k, 0)]
    if not top_k:
        return 0.0
    expected_list = [e for e in expected]
    if not expected_list:
        return 0.0
    hits = sum(
        1 for r in top_k if any(_path_matches(r, e) for e in expected_list)
    )
    return hits / len(top_k)


def aggregate(
    strategy_name: str, results: Iterable[QuestionResult],
) -> EvaluationReport:
    rs = list(results)
    if not rs:
        return EvaluationReport(
            strategy_name=strategy_name,
            n_questions=0,
            mean_recall_at_1=0.0, mean_recall_at_3=0.0, mean_recall_at_10=0.0,
            mean_mrr=0.0, mean_citation_precision_at_5=0.0,
            mean_elapsed_ms=0.0,
            per_question=(),
        )
    return EvaluationReport(
        strategy_name=strategy_name,
        n_questions=len(rs),
        mean_recall_at_1=statistics.mean(r.recall_at_1 for r in rs),
        mean_recall_at_3=statistics.mean(r.recall_at_3 for r in rs),
        mean_recall_at_10=statistics.mean(r.recall_at_10 for r in rs),
        mean_mrr=statistics.mean(r.mrr for r in rs),
        mean_citation_precision_at_5=statistics.mean(
            r.citation_precision_at_5 for r in rs
        ),
        mean_elapsed_ms=statistics.mean(r.elapsed_ms for r in rs),
        per_question=tuple(rs),
    )
