"""Pluggable retrieval strategies for the RAG bench.

Each strategy implements ``RetrievalStrategy`` (see ``base.py``) and
returns ranked file paths for a given ``BenchmarkQuestion``.
"""
from app.services.rag_bench.strategies.base import RetrievalStrategy
from app.services.rag_bench.strategies.fts5_baseline import (
    FTS5BaselineStrategy,
)
from app.services.rag_bench.strategies.hybrid_rrf import HybridRRFStrategy
from app.services.rag_bench.strategies.hyde import HydeStrategy

# Optional imports — guard model2vec since it pulls a tokenizer wheel
# that may not be available everywhere.
try:
    from app.services.rag_bench.strategies.dense_embedding import (
        DenseEmbeddingStrategy,
    )
    _HAS_DENSE = True
except ImportError:  # pragma: no cover
    DenseEmbeddingStrategy = None  # type: ignore
    _HAS_DENSE = False

__all__ = [
    "RetrievalStrategy",
    "FTS5BaselineStrategy",
    "HybridRRFStrategy",
    "HydeStrategy",
    "DenseEmbeddingStrategy",
]
