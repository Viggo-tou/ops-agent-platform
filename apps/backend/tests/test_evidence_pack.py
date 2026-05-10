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


# --- AST-aware truncation integration ---------------------------------------


def _python_module(big_fn_lines: int = 80, small_fn_count: int = 3) -> str:
    parts = ["import os\nimport sys\n\n", "ANSWER = 42\n\n"]
    for i in range(small_fn_count):
        parts.append(f"def small_{i}(arg):\n    return arg + {i}\n\n")
    body = "\n".join(f"    x_{i} = {i}" for i in range(big_fn_lines))
    parts.append(
        f"def big_helper(arg):\n    \"\"\"big helper docstring.\"\"\"\n{body}\n    return arg\n"
    )
    return "".join(parts)


def test_truncate_python_file_keeps_imports_and_signatures():
    """A 2000-line .py file truncated at 6KB should still expose imports
    and class/function signatures (the regression we hit on
    2026-05-09 with django/db/models/sql/query.py)."""
    src = _python_module(big_fn_lines=200, small_fn_count=4)
    out = truncate_file(src, max_bytes=2_000, path="big.py")
    assert "import os" in out
    assert "import sys" in out
    assert "ANSWER = 42" in out
    assert "def small_0(arg):" in out
    assert "def big_helper(arg):" in out
    # Big-function body content is dropped.
    assert "x_150 = 150" not in out


def test_truncate_python_file_byte_caps_when_ast_still_too_big():
    """When even the AST output exceeds the cap (e.g. dozens of small
    methods all pinned), the AST output is byte-truncated. Strictly
    better than truncating raw source because the AST output already
    dropped big bodies."""
    # 100 small functions — each ≤ small_body_lines, so all kept whole.
    parts = ["import os\n\n"]
    for i in range(100):
        parts.append(f"def fn_{i}(x):\n    return x + {i}\n\n")
    src = "".join(parts)
    out = truncate_file(src, max_bytes=500, path="many.py")
    assert len(out) <= 500 + 50  # cap + marker
    # The byte-capped slice still leads with imports + early functions.
    assert "import os" in out
    assert "def fn_0" in out


def test_truncate_non_python_file_uses_byte_path():
    """Kotlin / JS files don't get AST truncation; they fall back to
    byte truncation with the legacy marker."""
    src = "fun main() {\n" + ("    println(\"x\")\n" * 200) + "}\n"
    out = truncate_file(src, max_bytes=500, path="Foo.kt")
    assert len(out) <= 500 + 50
    assert "truncated" in out.lower()


def test_truncate_python_syntax_error_falls_back_to_byte():
    bad = "this is not python " + ("###\n" * 200)
    out = truncate_file(bad, max_bytes=200, path="oops.py")
    assert len(out) <= 200 + 50
    assert "truncated" in out.lower()


def test_truncate_python_keep_symbols_overshoots_max_bytes_to_preserve_body():
    """When caller pins a symbol AND the AST keeps it whole, the result
    should overshoot max_bytes rather than byte-cap mid-pinned-body.

    Regression on 2026-05-10 v5: astropy task 1 still hit EVIDENCE_GAP
    after the AST + cross-reference fixes because ndarithmetic.py's
    AST output was ~ 8 KB (with `_arithmetic_mask` body preserved at
    bytes 6500-8500) and the byte-cap at max_bytes=6000 sliced the
    pinned body off."""
    parts = ['"""docstring."""\n', "import os\n\n", "class Big:\n"]
    for i in range(10):
        parts.append(f"    def big_method_{i}(self):\n")
        parts.append(f"        \"\"\"big_method_{i}.\"\"\"\n")
        parts.extend(f"        line_{j}_of_{i} = {j}\n" for j in range(60))
        parts.append("        return None\n\n")
    parts.append("    def _target_routine(self):\n")
    parts.append("        \"\"\"target.\"\"\"\n")
    parts.extend(f"        target_line_{j} = {j}\n" for j in range(40))
    parts.append("        return self.x\n")
    src = "".join(parts)
    out = truncate_file(
        src, max_bytes=2_000, path="m.py", keep_symbols=["_target_routine"]
    )
    # The pinned body must be present in full.
    assert "target_line_25 = 25" in out
    assert "target_line_39 = 39" in out
    # The other big bodies must still be elided.
    assert "line_30_of_3 = 30" not in out
    # Output is allowed to exceed max_bytes when pin was honoured.
    # (No assertion on length — just verify the body is intact.)


def test_truncate_python_unpinned_still_byte_capped():
    """When no pin or pin not kept, byte-cap fallback still applies."""
    parts = ["import os\n\n"]
    for i in range(20):
        parts.append(f"def fn_{i}():\n    return {i}\n\n")
    src = "".join(parts)
    out = truncate_file(src, max_bytes=200, path="m.py")
    # No pin → we don't overshoot.
    assert len(out) <= 200 + 50
    assert "truncated" in out.lower()


def test_evidence_pack_python_truncation_preserves_signatures():
    """End-to-end: a big .py file in the pack arrives with function
    signatures intact, not just imports + class headers."""
    src = _python_module(big_fn_lines=200, small_fn_count=2)
    files = [_ev("django/db/models/sql/query.py", content=src, priority=1)]
    budget = EvidencePackBudget(
        max_files=1, max_total_bytes=20_000, max_per_file_bytes=2_000
    )
    pack = build_evidence_pack(files, budget)
    assert len(pack.included_files) == 1
    out = pack.included_files[0].content
    assert "def small_0(arg):" in out
    assert "def big_helper(arg):" in out
    assert "elided by ast_truncate" in out
