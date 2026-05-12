"""R-C: Hybrid retrieval via Reciprocal Rank Fusion.

Combines two (or more) underlying strategies' rankings into a single
list using the RRF formula:

    score(doc) = sum_over_strategies(1 / (k + rank_in_strategy_i))

where ``k`` defaults to 60 (the original Cormack et al. value).
RRF needs no score calibration — works on RAW ranks — and is the
standard cheap-and-strong fusion baseline in IR.

Composes any RetrievalStrategy implementations; here we ship the
FTS5 + dense_embedding combination as the canonical hybrid.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from app.services.rag_bench.dataset import BenchmarkQuestion
from app.services.rag_bench.strategies.base import RetrievalStrategy


class HybridRRFStrategy:
    def __init__(
        self,
        *strategies: RetrievalStrategy,
        k: int = 60,
        per_strategy_top_k: int = 50,
    ) -> None:
        if not strategies:
            raise ValueError("HybridRRFStrategy requires >= 1 strategy")
        self._strategies = strategies
        self._k = k
        self._per_strategy_top_k = per_strategy_top_k

    @property
    def name(self) -> str:
        inner = "+".join(s.name for s in self._strategies)
        return f"hybrid_rrf({inner})"

    def retrieve(
        self, *, question: BenchmarkQuestion, top_k: int = 10,
    ) -> tuple[str, ...]:
        scores: dict[str, float] = defaultdict(float)
        first_seen_order: dict[str, int] = {}
        global_idx = 0
        for s in self._strategies:
            ranked = s.retrieve(
                question=question, top_k=self._per_strategy_top_k,
            )
            for rank, path in enumerate(ranked, start=1):
                scores[path] += 1.0 / (self._k + rank)
                if path not in first_seen_order:
                    first_seen_order[path] = global_idx
                    global_idx += 1
        # Sort by RRF score desc; tiebreak by first-seen rank for determinism
        ordered = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], first_seen_order.get(kv[0], 0)),
        )
        return tuple(path for path, _ in ordered[:top_k])
