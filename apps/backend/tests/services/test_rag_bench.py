"""Tests for the RAG bench framework + FTS5 baseline strategy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.rag_bench import (  # noqa: E402
    BenchmarkQuestion,
    citation_precision,
    evaluate_strategy,
    mean_reciprocal_rank,
    recall_at_k,
)
from app.services.rag_bench.dataset import load_questions  # noqa: E402
from app.services.rag_bench.metrics import _path_matches  # noqa: E402
from app.services.rag_bench.strategies.base import RetrievalStrategy  # noqa: E402


def _q(qid: str = "Q1", expected=("src/A.kt",), tier="A") -> BenchmarkQuestion:
    return BenchmarkQuestion(
        id=qid, tier=tier, source_name="myapp",
        question="where is foo",
        expected_citations=tuple(f"myapp/{p}" for p in expected),
        expected_answer_keypoints=(),
    )


# --- _path_matches ---------------------------------------------------------

def test_path_match_equal():
    assert _path_matches("src/A.kt", "src/A.kt")


def test_path_match_suffix_tolerant():
    assert _path_matches("workdir/src/A.kt", "src/A.kt")
    assert _path_matches("src/A.kt", "workdir/src/A.kt")


def test_path_match_basename_fallback():
    assert _path_matches("a/b/c/Same.kt", "x/y/Same.kt")


def test_path_match_diff_basename_no_match():
    assert not _path_matches("src/A.kt", "src/B.kt")


# --- recall_at_k -----------------------------------------------------------

def test_recall_at_k_full_hit():
    assert recall_at_k(("src/A.kt", "src/B.kt"), ("src/A.kt",), k=1) == 1.0


def test_recall_at_k_partial():
    assert recall_at_k(
        ("src/X.kt", "src/A.kt", "src/B.kt"),
        ("src/A.kt", "src/B.kt"),
        k=3,
    ) == 1.0


def test_recall_at_k_misses_below_k():
    # expected at rank 5; recall@3 = 0
    retrieved = ("a", "b", "c", "d", "src/A.kt")
    assert recall_at_k(retrieved, ("src/A.kt",), k=3) == 0.0


def test_recall_empty_expected_returns_zero():
    assert recall_at_k(("a",), (), k=3) == 0.0


# --- mean_reciprocal_rank --------------------------------------------------

def test_mrr_first_position():
    assert mean_reciprocal_rank(("src/A.kt",), ("src/A.kt",)) == 1.0


def test_mrr_third_position():
    rr = mean_reciprocal_rank(("x", "y", "src/A.kt"), ("src/A.kt",))
    assert pytest.approx(rr, rel=1e-6) == 1 / 3


def test_mrr_not_found_zero():
    assert mean_reciprocal_rank(("x", "y"), ("src/A.kt",)) == 0.0


def test_mrr_two_expected_average():
    # A at rank 1, B at rank 3 -> mean(1, 1/3)
    rr = mean_reciprocal_rank(
        ("src/A.kt", "x", "src/B.kt"),
        ("src/A.kt", "src/B.kt"),
    )
    assert pytest.approx(rr, rel=1e-6) == (1.0 + 1 / 3) / 2


# --- citation_precision ----------------------------------------------------

def test_citation_precision_at_5_all_relevant():
    assert citation_precision(
        ("src/A.kt", "src/B.kt", "src/A.kt"),
        ("src/A.kt", "src/B.kt"), k=5,
    ) == 1.0


def test_citation_precision_at_5_mixed():
    # 1 hit out of 3 retrieved
    assert citation_precision(
        ("src/A.kt", "noise.kt", "more_noise.kt"),
        ("src/A.kt",), k=5,
    ) == pytest.approx(1 / 3, rel=1e-6)


# --- BenchmarkQuestion -----------------------------------------------------

def test_expected_files_strips_source_prefix():
    q = _q(expected=("app/src/Foo.kt",))
    assert q.expected_files == ("app/src/Foo.kt",)
    # The benchmark's raw citation has the source-name prefix
    assert q.expected_citations == ("myapp/app/src/Foo.kt",)


# --- load_questions --------------------------------------------------------

def test_load_questions_parses_jsonl(tmp_path: Path):
    p = tmp_path / "b.jsonl"
    p.write_text(
        json.dumps({
            "id": "Q1", "tier": "A", "source_name": "x",
            "question": "?", "expected_citations": ["x/a.kt"],
            "expected_answer_keypoints": ["..."],
        }) + "\n",
        encoding="utf-8",
    )
    qs = load_questions(p)
    assert len(qs) == 1
    assert qs[0].id == "Q1"
    assert qs[0].expected_files == ("a.kt",)


def test_load_questions_tier_filter(tmp_path: Path):
    p = tmp_path / "b.jsonl"
    rows = [
        {"id": "A1", "tier": "A", "source_name": "x", "question": "?",
         "expected_citations": []},
        {"id": "C1", "tier": "C", "source_name": "x", "question": "?",
         "expected_citations": []},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = load_questions(p, tier_filter=("A",))
    assert [q.id for q in out] == ["A1"]


# --- evaluate_strategy with a fake strategy -------------------------------

class _PerfectStrategy:
    @property
    def name(self) -> str:
        return "perfect"

    def retrieve(self, *, question, top_k=10) -> tuple[str, ...]:
        # Always return exactly the expected_files
        return question.expected_files


class _AlwaysWrongStrategy:
    @property
    def name(self) -> str:
        return "always_wrong"

    def retrieve(self, *, question, top_k=10) -> tuple[str, ...]:
        return ("noise.txt",)


def test_evaluate_perfect_strategy_achieves_full_recall():
    qs = [_q("Q1", expected=("src/A.kt",))]
    rep = evaluate_strategy(strategy=_PerfectStrategy(), questions=qs)
    assert rep.mean_recall_at_1 == 1.0
    assert rep.mean_mrr == 1.0
    assert rep.n_questions == 1


def test_evaluate_wrong_strategy_zero_recall():
    qs = [_q("Q1", expected=("src/A.kt",))]
    rep = evaluate_strategy(strategy=_AlwaysWrongStrategy(), questions=qs)
    assert rep.mean_recall_at_1 == 0.0
    assert rep.mean_mrr == 0.0


def test_evaluate_summary_dict_serializes():
    qs = [_q("Q1", expected=("src/A.kt",))]
    rep = evaluate_strategy(strategy=_PerfectStrategy(), questions=qs)
    d = rep.to_summary_dict()
    json.dumps(d)
    assert d["recall@1"] == 1.0
    assert d["strategy"] == "perfect"
