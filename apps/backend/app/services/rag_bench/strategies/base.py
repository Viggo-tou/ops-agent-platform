"""Strategy Protocol.

Every retrieval strategy implements this single contract; the harness
treats them interchangeably. Add a new strategy = a new file that
implements ``RetrievalStrategy``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.rag_bench.dataset import BenchmarkQuestion


@runtime_checkable
class RetrievalStrategy(Protocol):
    """Return ranked file paths (top-K) for a benchmark question."""

    @property
    def name(self) -> str:
        """Short stable identifier used in result tables."""
        ...

    def retrieve(
        self, *, question: BenchmarkQuestion, top_k: int = 10,
    ) -> tuple[str, ...]:
        """Return up to ``top_k`` candidate file paths, most-relevant first.

        Path format should match the benchmark's ``expected_files`` (i.e.
        repo-relative without the source-name prefix). The eval metrics
        use suffix-tolerant + basename matching, so minor path-prefix
        drift is OK but the file extension and basename must match.
        """
        ...
