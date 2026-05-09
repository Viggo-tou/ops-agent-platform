"""Tests for the bounded, relevance-ranked evidence pack."""
from __future__ import annotations

import pytest

from app.services.evidence_pack import (
    EvidencePack,
    EvidencePackBudget,
    FileEvidence,
    build_evidence_pack,
    truncate_file,
)


def _ev(path: str, *, content: str = "x", priority: int = 5) -> FileEvidence:
    return FileEvidence(path=path, content=content, priority=priority)


def test_pack_keeps_files_within_byte_budget():
    files = [
        _ev("a.py", content="a" * 6000, priority=1),
        _ev("b.py", content="b" * 6000, priority=2),
        _ev("c.py", content="c" * 6000, priority=3),
        _ev("d.py", content="d" * 6000, priority=4),
        _ev("e.py", content="e" * 6000, priority=5),
    ]
    budget = EvidencePackBudget(max_files=10, max_total_bytes=18_000, max_per_file_bytes=10_000)
    pack = build_evidence_pack(files, budget)
    # 3 files × 6000 = 18000 fits; 4th would exceed.
    assert len(pack.included_files) == 3
    assert {f.path for f in pack.included_files} == {"a.py", "b.py", "c.py"}
    assert {f.path for f in pack.dropped} == {"d.py", "e.py"}


def test_pack_keeps_files_within_file_count_budget():
    files = [_ev(f"f{i}.py", content="x" * 100) for i in range(20)]
    budget = EvidencePackBudget(max_files=6, max_total_bytes=100_000, max_per_file_bytes=1_000)
    pack = build_evidence_pack(files, budget)
    assert len(pack.included_files) == 6
    assert len(pack.dropped) == 14


def test_pack_priority_order_high_first():
    files = [
        _ev("low.py", priority=10),
        _ev("med.py", priority=5),
        _ev("high.py", priority=1),
    ]
    budget = EvidencePackBudget(max_files=2, max_total_bytes=10_000, max_per_file_bytes=1_000)
    pack = build_evidence_pack(files, budget)
    paths = [f.path for f in pack.included_files]
    assert paths[:2] == ["high.py", "med.py"]
    assert pack.dropped[0].path == "low.py"


def test_pack_metrics_record_bytes_used_and_dropped():
    files = [
        _ev("a.py", content="a" * 5000, priority=1),
        _ev("b.py", content="b" * 5000, priority=2),
        _ev("c.py", content="c" * 5000, priority=3),
    ]
    budget = EvidencePackBudget(max_files=10, max_total_bytes=10_000, max_per_file_bytes=10_000)
    pack = build_evidence_pack(files, budget)
    assert pack.metrics["bytes_used"] == 10_000
    assert pack.metrics["files_included"] == 2
    assert pack.metrics["files_dropped"] == 1


def test_pack_truncates_large_individual_files():
    big = "x" * 20_000
    files = [_ev("big.py", content=big, priority=1)]
    budget = EvidencePackBudget(max_files=5, max_total_bytes=50_000, max_per_file_bytes=8_000)
    pack = build_evidence_pack(files, budget)
    assert len(pack.included_files) == 1
    assert len(pack.included_files[0].content) <= 8_000 + 200  # +marker text
    assert "truncated" in pack.included_files[0].content.lower()


def test_pack_skips_file_too_big_for_remaining_budget():
    files = [
        _ev("a.py", content="a" * 16_000, priority=1),
        _ev("b.py", content="b" * 5_000, priority=2),
        _ev("c.py", content="c" * 1_000, priority=3),
    ]
    budget = EvidencePackBudget(
        max_files=5, max_total_bytes=18_000, max_per_file_bytes=20_000
    )
    pack = build_evidence_pack(files, budget)
    paths = [f.path for f in pack.included_files]
    # a.py (16k) eats most of the budget; b.py (5k) wouldn't fit (would
    # take total to 21k); c.py (1k) fits.
    assert "a.py" in paths
    assert "c.py" in paths
    assert "b.py" not in paths


def test_pack_dropped_includes_reason():
    files = [
        _ev("a.py", content="a" * 12_000, priority=1),
        _ev("b.py", content="b" * 8_000, priority=2),
    ]
    budget = EvidencePackBudget(
        max_files=2, max_total_bytes=12_000, max_per_file_bytes=20_000
    )
    pack = build_evidence_pack(files, budget)
    assert len(pack.dropped) == 1
    assert pack.dropped[0].path == "b.py"
    assert pack.dropped[0].reason  # non-empty


def test_pack_empty_input_yields_empty_pack():
    pack = build_evidence_pack([], EvidencePackBudget())
    assert pack.included_files == []
    assert pack.dropped == []
    assert pack.metrics["bytes_used"] == 0


def test_truncate_file_under_budget_returns_unchanged():
    out = truncate_file("a" * 100, max_bytes=200)
    assert out == "a" * 100


def test_truncate_file_over_budget_appends_marker():
    out = truncate_file("a" * 5000, max_bytes=1000)
    assert len(out) <= 1100  # original cap + marker
    assert "truncated" in out.lower()
    assert out.startswith("a" * 100)


def test_pack_default_budgets_are_sane():
    # Catches drift if someone bumps a default unintentionally.
    b = EvidencePackBudget()
    assert b.max_files <= 10
    assert b.max_total_bytes <= 25_000
    assert b.max_per_file_bytes <= 8_000
