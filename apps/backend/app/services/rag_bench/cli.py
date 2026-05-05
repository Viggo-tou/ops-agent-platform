"""CLI entry for the RAG bench.

Usage:
    python -m app.services.rag_bench.cli
        [--top-k 10]
        [--tier A,B,C]
        [--strategies fts5_baseline,...]
        [--output runs/rag_bench_<ts>.jsonl]

Persists per-question results to a JSONL file under
``apps/backend/tests/benchmarks/runs/`` and prints the per-strategy
summary table to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.rag_bench.dataset import (
    BenchmarkQuestion,
    load_all_default_benchmarks,
    load_questions,
)
from app.services.rag_bench.runner import evaluate_strategy
from app.core.config import get_settings
from app.services.rag_bench.strategies import (
    FTS5BaselineStrategy,
    HybridRRFStrategy,
    HydeStrategy,
)
from app.services.rag_bench.strategies import _HAS_DENSE


def _build_factory_map(db, settings):
    factories: dict = {
        "fts5_baseline": lambda: FTS5BaselineStrategy(db),
    }
    if _HAS_DENSE:
        from app.services.rag_bench.strategies.dense_embedding import (
            DenseEmbeddingStrategy,
        )
        factories["dense_embedding"] = lambda: DenseEmbeddingStrategy(db)
        factories["hybrid_rrf"] = lambda: HybridRRFStrategy(
            FTS5BaselineStrategy(db),
            DenseEmbeddingStrategy(db),
        )
    factories["hyde_fts5"] = lambda: HydeStrategy(
        FTS5BaselineStrategy(db), settings=settings,
    )
    if _HAS_DENSE:
        from app.services.rag_bench.strategies.dense_embedding import (
            DenseEmbeddingStrategy,
        )
        factories["hyde_hybrid"] = lambda: HydeStrategy(
            HybridRRFStrategy(
                FTS5BaselineStrategy(db),
                DenseEmbeddingStrategy(db),
            ),
            settings=settings,
        )
    return factories


def _build_strategies(names: list[str], db, settings) -> list:
    factories = _build_factory_map(db, settings)
    out = []
    for n in names:
        factory = factories.get(n)
        if factory is None:
            print(
                f"WARNING: unknown strategy '{n}' — available: "
                f"{sorted(factories)}",
                file=sys.stderr,
            )
            continue
        out.append(factory())
    return out


def _load(args) -> list[BenchmarkQuestion]:
    if args.dataset:
        qs = load_questions(args.dataset)
    else:
        qs = load_all_default_benchmarks()
    if args.tier:
        wanted = {t.strip().upper() for t in args.tier.split(",") if t.strip()}
        qs = [q for q in qs if q.tier in wanted]
    return qs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RAG retrieval benchmark")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--tier", default="",
                   help="Comma-separated tier filter (A,B,C)")
    p.add_argument("--strategies", default="fts5_baseline",
                   help="Comma-separated strategy names")
    p.add_argument("--db",
                   default="sqlite:///./ops_agent_platform.db",
                   help="SQLAlchemy URL for the KB DB")
    p.add_argument("--dataset", default="",
                   help="Override dataset JSONL path (default: ship-with)")
    p.add_argument("--output", default="",
                   help="Per-question JSONL output path (default: timestamped)")
    args = p.parse_args(argv)

    questions = _load(args)
    if not questions:
        print("ERROR: no benchmark questions loaded", file=sys.stderr)
        return 1

    engine = create_engine(args.db)
    SessionLocal = sessionmaker(bind=engine, future=True)
    db = SessionLocal()
    settings = get_settings()
    try:
        strats = _build_strategies(
            [s.strip() for s in args.strategies.split(",") if s.strip()],
            db, settings,
        )
        if not strats:
            print("ERROR: no strategies to run", file=sys.stderr)
            return 1

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        runs_dir = Path(__file__).resolve().parents[3] / "tests" / "benchmarks" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            Path(args.output) if args.output
            else runs_dir / f"rag_bench_{ts}.jsonl"
        )

        # Header table
        print(f"\n{'strategy':22} {'n':>4}  "
              f"{'r@1':>5}  {'r@3':>5}  {'r@10':>5}  "
              f"{'mrr':>5}  {'p@5':>5}  {'ms/q':>5}")
        print("-" * 75)

        with out_path.open("w", encoding="utf-8") as fh:
            for strat in strats:
                rep = evaluate_strategy(
                    strategy=strat, questions=questions, top_k=args.top_k,
                )
                d = rep.to_summary_dict()
                print(
                    f"{d['strategy']:22} {d['n']:>4}  "
                    f"{d['recall@1']:>5}  {d['recall@3']:>5}  "
                    f"{d['recall@10']:>5}  {d['mrr']:>5}  "
                    f"{d['citation_p@5']:>5}  {d['ms_per_q']:>5}"
                )
                # Persist per-question rows
                for q in rep.per_question:
                    fh.write(json.dumps({
                        "strategy": rep.strategy_name,
                        "question_id": q.question_id,
                        "tier": q.tier,
                        "source_name": q.source_name,
                        "expected": list(q.expected),
                        "retrieved": list(q.retrieved),
                        "recall_at_1": q.recall_at_1,
                        "recall_at_3": q.recall_at_3,
                        "recall_at_10": q.recall_at_10,
                        "mrr": q.mrr,
                        "citation_p_at_5": q.citation_precision_at_5,
                        "elapsed_ms": q.elapsed_ms,
                        "timestamp_utc": ts,
                    }) + "\n")
        print(f"\nPer-question results -> {out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
