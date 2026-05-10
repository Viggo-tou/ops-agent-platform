"""Tests for Tier 4-H bounded tool-use loop."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.codegen_tool_loop import (
    GapRequest,
    fulfil_requests,
    parse_evidence_gap_requests,
    render_spans_for_prompt,
)


# --- request parsing --------------------------------------------------------


def test_parse_basic_block():
    text = """\
## EVIDENCE_GAP_REQUEST
file: django/db/models/fields/__init__.py
symbol: contribute_to_class
why: need closure binding site for _get_FIELD_display
"""
    reqs = parse_evidence_gap_requests(text)
    assert len(reqs) == 1
    assert reqs[0].file == "django/db/models/fields/__init__.py"
    assert reqs[0].symbol == "contribute_to_class"
    assert "closure" in reqs[0].why


def test_parse_multiple_in_one_block_split_by_blank_lines():
    text = """\
## EVIDENCE_GAP_REQUEST
file: a.py
symbol: foo

file: b.py
symbol: bar
why: secondary need
"""
    reqs = parse_evidence_gap_requests(text)
    assert len(reqs) == 2
    assert reqs[0].file == "a.py" and reqs[0].symbol == "foo"
    assert reqs[1].file == "b.py" and reqs[1].symbol == "bar"


def test_parse_capped_at_max_hits():
    blocks = "\n\n".join(
        [
            "## EVIDENCE_GAP_REQUEST\n" + "\n\n".join(
                f"file: f{i}.py\nsymbol: sym{i}" for i in range(8)
            )
        ]
    )
    reqs = parse_evidence_gap_requests(blocks)
    assert len(reqs) <= 4  # _MAX_REQUEST_HITS


def test_parse_no_request_block_returns_empty():
    """Pure EVIDENCE_GAP without identifiers or paths still yields empty."""
    text = "## EVIDENCE_GAP: just plain prose, no structured request."
    assert parse_evidence_gap_requests(text) == []


def test_parse_implicit_extracts_backtick_idents_and_path():
    """V11 regression: DeepSeek emitted plain EVIDENCE_GAP with prose
    naming a backtick-quoted symbol + a .py path. Implicit path should
    synthesise a structured GapRequest so Tier 4-H still fires."""
    text = (
        "## EVIDENCE_GAP: the full implementation of "
        "`_arithmetic_mask` in astropy/nddata/mixins/ndarithmetic.py "
        "is required to produce the SEARCH block."
    )
    reqs = parse_evidence_gap_requests(text)
    assert len(reqs) == 1
    assert reqs[0].file == "astropy/nddata/mixins/ndarithmetic.py"
    assert reqs[0].symbol == "_arithmetic_mask"
    assert "implicit" in reqs[0].why


def test_parse_implicit_multiple_symbols_share_path():
    text = (
        "## EVIDENCE_GAP: I need both `foo_helper` and `bar_helper` "
        "from app/utils.py to construct the diff."
    )
    reqs = parse_evidence_gap_requests(text)
    syms = {r.symbol for r in reqs}
    assert syms == {"foo_helper", "bar_helper"}
    assert all(r.file == "app/utils.py" for r in reqs)


def test_parse_implicit_no_extraction_when_no_evidence_gap_marker():
    """Identifiers alone without an EVIDENCE_GAP marker shouldn't
    synthesise requests — that text isn't a gap signal."""
    text = "Random prose with `foo` and src/x.py mentioned."
    assert parse_evidence_gap_requests(text) == []


def test_parse_structured_takes_precedence_over_implicit():
    """When both formats appear, structured requests win (their
    why-line carries planner intent; implicit guesses are noisy)."""
    text = (
        "## EVIDENCE_GAP: also missing `extra_helper` from foo.py\n"
        "## EVIDENCE_GAP_REQUEST\n"
        "file: app/main.py\n"
        "symbol: handle_request\n"
    )
    reqs = parse_evidence_gap_requests(text)
    assert len(reqs) == 1
    assert reqs[0].file == "app/main.py"
    assert reqs[0].symbol == "handle_request"


def test_parse_handles_lowercase_and_dash_variants():
    text = "## evidence-gap-request\nfile: a.py\nsymbol: foo\n"
    reqs = parse_evidence_gap_requests(text)
    assert len(reqs) == 1
    assert reqs[0].file == "a.py"


# --- request fulfilment -----------------------------------------------------


def test_fulfil_finds_symbol_in_candidate_files():
    files = {
        "x.py": (
            "import os\n\n"
            "def foo():\n"
            "    return 1\n\n"
            "def bar(arg):\n"
            "    \"\"\"bar docstring.\"\"\"\n"
            "    return arg + 1\n"
        ),
    }
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol="bar")], candidate_files=files
    )
    assert len(spans) == 1
    assert "def bar(arg):" in spans[0].body
    assert "def foo" not in spans[0].body  # only the symbol's range


def test_fulfil_basename_match_when_full_path_off():
    files = {"deep/nested/x.py": "def target():\n    return 1\n"}
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol="target")], candidate_files=files
    )
    assert len(spans) == 1
    assert "def target():" in spans[0].body


def test_fulfil_disk_read_when_not_in_candidates(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    (sub / "m.py").write_text(
        "def needle(x):\n    return x * 2\n", encoding="utf-8"
    )
    spans = fulfil_requests(
        [GapRequest(file="pkg/m.py", symbol="needle")],
        candidate_files={},
        repo_root=repo,
    )
    assert len(spans) == 1
    assert "def needle(x):" in spans[0].body


def test_fulfil_blocks_path_traversal(tmp_path):
    """A request like file: ../secrets.py must NOT escape repo_root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.py").write_text("API_KEY = 'leak'\n", encoding="utf-8")
    spans = fulfil_requests(
        [GapRequest(file="../secret.py", symbol=None)],
        candidate_files={},
        repo_root=repo,
    )
    assert spans == []


def test_fulfil_whole_file_when_no_symbol():
    files = {"x.py": "x = 1\ny = 2\nz = 3\n"}
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol=None)], candidate_files=files
    )
    assert len(spans) == 1
    assert spans[0].body.startswith("x = 1")
    assert spans[0].symbol is None


def test_fulfil_unknown_symbol_drops_request():
    files = {"x.py": "def real():\n    pass\n"}
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol="ghost")], candidate_files=files
    )
    assert spans == []


def test_fulfil_caps_span_bytes():
    big_body = "x = 1\n" * 5000  # ~30 KB
    files = {"x.py": f"def big():\n    {big_body}    return None\n"}
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol="big")], candidate_files=files
    )
    assert len(spans) == 1
    assert len(spans[0].body) <= 4_000
    assert "truncated" in spans[0].note.lower()


def test_fulfil_handles_ast_parse_failure():
    """When the file trips ast.parse, fall back to regex extraction."""
    src = (
        '_doc = """unterminated\n'
        "def needle():\n"
        "    return 1\n"
        "def other():\n"
        "    return 2\n"
    )
    import ast as _ast
    try:
        _ast.parse(src)
    except SyntaxError:
        pass
    else:
        pytest.skip("ast accepts this source, doesn't exercise fallback")
    files = {"x.py": src}
    spans = fulfil_requests(
        [GapRequest(file="x.py", symbol="needle")], candidate_files=files
    )
    assert len(spans) == 1
    assert "def needle()" in spans[0].body
    assert "def other()" not in spans[0].body  # body capture stops at next def


# --- prompt rendering -------------------------------------------------------


def test_render_includes_filenames_and_anti_loop_directive():
    from app.services.codegen_tool_loop import FetchedSpan

    spans = [
        FetchedSpan(file="a.py", symbol="foo", body="def foo():\n    return 1\n"),
        FetchedSpan(file="b.py", symbol=None, body="x = 1\n", note="truncated"),
    ]
    text = render_spans_for_prompt(spans)
    assert "EVIDENCE FETCH" in text
    assert "a.py :: foo" in text
    assert "b.py" in text
    assert "do not re-emit evidence_gap" in text.lower()


def test_render_empty_returns_empty_string():
    assert render_spans_for_prompt([]) == ""
