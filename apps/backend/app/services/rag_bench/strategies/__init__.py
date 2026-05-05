"""Pluggable retrieval strategies for the RAG bench.

Each strategy implements ``RetrievalStrategy`` (see ``base.py``) and
returns ranked file paths for a given ``BenchmarkQuestion``.
"""
from app.services.rag_bench.strategies.base import RetrievalStrategy
from app.services.rag_bench.strategies.fts5_baseline import (
    FTS5BaselineStrategy,
)

__all__ = ["RetrievalStrategy", "FTS5BaselineStrategy"]
