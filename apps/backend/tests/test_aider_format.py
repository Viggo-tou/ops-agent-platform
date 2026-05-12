"""Tests for Aider search/replace format parser, applier, diff
conversion.

The Aider format is the LLM-friendly alternative to unified diff for
codegen. These tests pin the parsing rules + apply semantics + diff
conversion contract that the codegen integration depends on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.aider_format import (
    AiderBlock,
    AiderParseError,
    aider_blocks_to_unified_diff,
    apply_aider_blocks,
    apply_aider_blocks_in_memory,
    parse_aider_blocks,
)


# --- parser ------------------------------------------------------------------


def test_parse_single_block():
    text = """\
foo.py
<<<<<<< SEARCH
old code
=======
new code
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].file == "foo.py"
    assert blocks[0].search == "old code"
    assert blocks[0].replace == "new code"
    assert not blocks[0].is_new_file


def test_parse_multiple_blocks_same_file():
    text = """\
foo.py
<<<<<<< SEARCH
A
=======
A1
>>>>>>> REPLACE

<<<<<<< SEARCH
B
=======
B1
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert len(blocks) == 2
    assert all(b.file == "foo.py" for b in blocks)
    assert blocks[0].search == "A"
    assert blocks[1].search == "B"


def test_parse_multiple_files():
    text = """\
foo.py
<<<<<<< SEARCH
old
=======
new
>>>>>>> REPLACE

bar.py
<<<<<<< SEARCH
also_old
=======
also_new
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert len(blocks) == 2
    assert blocks[0].file == "foo.py"
    assert blocks[1].file == "bar.py"


def test_parse_new_file_marker():
    text = """\
### NEW FILE: app/new.py
app/new.py
<<<<<<< SEARCH
=======
print("hello")
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].is_new_file
    assert blocks[0].search == ""
    assert blocks[0].replace == 'print("hello")'


def test_parse_empty_replace_means_delete():
    text = """\
foo.py
<<<<<<< SEARCH
delete me
=======
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert blocks[0].replace == ""


def test_parse_empty_search_means_append():
    text = """\
foo.py
<<<<<<< SEARCH
=======
appended
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert blocks[0].search == ""
    assert blocks[0].replace == "appended"


def test_parse_preserves_internal_whitespace_in_search():
    text = """\
foo.py
<<<<<<< SEARCH
def f():
    return 1
=======
def f():
    return 2
>>>>>>> REPLACE
"""
    blocks = parse_aider_blocks(text)
    assert blocks[0].search == "def f():\n    return 1"
    assert blocks[0].replace == "def f():\n    return 2"


def test_parse_missing_divider_errors():
    text = """\
foo.py
<<<<<<< SEARCH
old
>>>>>>> REPLACE
"""
    with pytest.raises(AiderParseError, match="missing ======="):
        parse_aider_blocks(text)


def test_parse_missing_tail_errors():
    text = """\
foo.py
<<<<<<< SEARCH
old
=======
new
"""
    with pytest.raises(AiderParseError, match="missing >>>>>>> REPLACE"):
        parse_aider_blocks(text)


def test_parse_search_without_filename_errors():
    text = """\
<<<<<<< SEARCH
old
=======
new
>>>>>>> REPLACE
"""
    with pytest.raises(AiderParseError, match="no filename header"):
        parse_aider_blocks(text)


def test_parse_empty_input_errors():
    with pytest.raises(AiderParseError, match="no blocks"):
        parse_aider_blocks("")


# --- applier -----------------------------------------------------------------


def test_apply_single_block(tmp_path):
    file = tmp_path / "foo.py"
    file.write_text("def f():\n    return 1\n", encoding="utf-8")
    blocks = [
        AiderBlock(
            file="foo.py",
            search="def f():\n    return 1",
            replace="def f():\n    return 2",
        )
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    assert "foo.py" in result.applied_files
    assert result.errors == []
    assert file.read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_apply_anchor_not_found(tmp_path):
    (tmp_path / "foo.py").write_text("real content\n", encoding="utf-8")
    blocks = [AiderBlock(file="foo.py", search="not present", replace="x")]
    result = apply_aider_blocks(blocks, tmp_path)
    assert result.applied_files == []
    assert any("anchor_not_found" in e.reason for e in result.errors)


def test_apply_anchor_ambiguous(tmp_path):
    (tmp_path / "foo.py").write_text("dup\nfiller\ndup\n", encoding="utf-8")
    blocks = [AiderBlock(file="foo.py", search="dup", replace="x")]
    result = apply_aider_blocks(blocks, tmp_path)
    assert result.applied_files == []
    assert any("anchor_ambiguous" in e.reason for e in result.errors)


def test_apply_new_file_creates_file(tmp_path):
    blocks = [
        AiderBlock(file="created.py", search="", replace="print('new')\n", is_new_file=True)
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    assert "created.py" in result.applied_files
    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print('new')\n"


def test_apply_delete_via_empty_replace(tmp_path):
    (tmp_path / "foo.py").write_text("keep\nremove\nkeep\n", encoding="utf-8")
    blocks = [AiderBlock(file="foo.py", search="remove\n", replace="")]
    result = apply_aider_blocks(blocks, tmp_path)
    assert "foo.py" in result.applied_files
    assert (tmp_path / "foo.py").read_text(encoding="utf-8") == "keep\nkeep\n"


def test_apply_multiple_blocks_in_order(tmp_path):
    (tmp_path / "foo.py").write_text("A\nB\nC\n", encoding="utf-8")
    blocks = [
        AiderBlock(file="foo.py", search="A", replace="A1"),
        # second block depends on first having run
        AiderBlock(file="foo.py", search="B", replace="B1"),
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    assert result.errors == []
    assert (tmp_path / "foo.py").read_text(encoding="utf-8") == "A1\nB1\nC\n"


def test_apply_missing_file_errors(tmp_path):
    blocks = [AiderBlock(file="nope.py", search="old", replace="new")]
    result = apply_aider_blocks(blocks, tmp_path)
    assert any("file not found" in e.reason for e in result.errors)


# --- unified diff conversion ------------------------------------------------


def test_aider_to_unified_diff_one_file(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    blocks = [
        AiderBlock(
            file="m.py",
            search="def f():\n    return 1",
            replace="def f():\n    return 2",
        )
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    diff = aider_blocks_to_unified_diff(result)
    assert "diff --git a/m.py b/m.py" in diff
    assert "-    return 1" in diff
    assert "+    return 2" in diff


def test_aider_to_unified_diff_no_changes_returns_empty(tmp_path):
    (tmp_path / "m.py").write_text("x\n", encoding="utf-8")
    blocks = [AiderBlock(file="m.py", search="x\n", replace="x\n")]
    result = apply_aider_blocks(blocks, tmp_path)
    diff = aider_blocks_to_unified_diff(result)
    assert diff == ""


# --- placeholder-anchor rejection (Class F regression) ---------------------


def test_in_memory_apply_rejects_anchor_on_elision_marker():
    """A SEARCH block that includes the AST-truncation placeholder
    text would produce a diff that targets lines that don't exist in
    the un-truncated source (git apply rejects). Reject at apply time
    so the model retries with a real anchor.

    Regression: 2026-05-10 v8 task 1 produced a 1143-char diff with
    `# ... 45 line(s) elided by ast_truncate (regex fallback) ...`
    and `pass` as context lines."""
    originals = {
        "m.py": (
            "def foo():\n"
            "    # ... 45 line(s) elided by ast_truncate (regex fallback) ...\n"
            "    pass\n"
        )
    }
    blocks = [
        AiderBlock(
            file="m.py",
            search=(
                "def foo():\n"
                "    # ... 45 line(s) elided by ast_truncate (regex fallback) ...\n"
                "    pass"
            ),
            replace="def foo():\n    return 1",
        )
    ]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert any("anchor_on_placeholder" in e.reason for e in result.errors)
    assert result.applied_files == []


def test_in_memory_apply_rejects_anchor_on_truncation_header():
    originals = {"m.py": "# === ast_truncate header ===\nx = 1\n"}
    blocks = [
        AiderBlock(
            file="m.py",
            search="# === ast_truncate header ===",
            replace="# new",
        )
    ]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert any("anchor_on_placeholder" in e.reason for e in result.errors)


def test_filesystem_apply_rejects_anchor_on_placeholder(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        "def foo():\n    # ... 10 line(s) elided by ast_truncate ...\n    pass\n",
        encoding="utf-8",
    )
    blocks = [
        AiderBlock(
            file="m.py",
            search="    # ... 10 line(s) elided by ast_truncate ...",
            replace="    return 1",
        )
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    assert any("anchor_on_placeholder" in e.reason for e in result.errors)


# --- in-memory apply (codegen integration path) -----------------------------


def test_in_memory_apply_simple_replace():
    originals = {"m.py": "def f():\n    return 1\n"}
    blocks = [
        AiderBlock(
            file="m.py",
            search="def f():\n    return 1",
            replace="def f():\n    return 2",
        )
    ]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert result.errors == []
    diff = aider_blocks_to_unified_diff(result)
    assert "diff --git a/m.py b/m.py" in diff
    assert "+    return 2" in diff


def test_in_memory_apply_anchor_not_found():
    originals = {"m.py": "x\n"}
    blocks = [AiderBlock(file="m.py", search="zzz", replace="yyy")]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert any("anchor_not_found" in e.reason for e in result.errors)
    assert result.applied_files == []


def test_in_memory_apply_anchor_ambiguous():
    originals = {"m.py": "dup\ndup\n"}
    blocks = [AiderBlock(file="m.py", search="dup", replace="ok")]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert any("anchor_ambiguous" in e.reason for e in result.errors)


def test_in_memory_apply_file_not_in_context():
    originals = {"a.py": "x\n"}
    blocks = [AiderBlock(file="missing.py", search="x", replace="y")]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert any("not present" in e.reason for e in result.errors)


def test_in_memory_apply_new_file():
    originals: dict[str, str] = {}
    blocks = [
        AiderBlock(
            file="new.py",
            search="",
            replace="print('hi')\n",
            is_new_file=True,
        )
    ]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert result.errors == []
    diff = aider_blocks_to_unified_diff(result)
    assert "diff --git a/new.py b/new.py" in diff
    assert "+print('hi')" in diff


def test_in_memory_apply_chained_blocks():
    originals = {"m.py": "A\nB\nC\n"}
    blocks = [
        AiderBlock(file="m.py", search="A", replace="A1"),
        AiderBlock(file="m.py", search="B", replace="B1"),
    ]
    result = apply_aider_blocks_in_memory(blocks, originals)
    assert result.errors == []
    diff = aider_blocks_to_unified_diff(result)
    assert "+A1" in diff and "+B1" in diff


def test_aider_to_unified_diff_new_file(tmp_path):
    blocks = [
        AiderBlock(
            file="new.py",
            search="",
            replace="print('hi')\n",
            is_new_file=True,
        )
    ]
    result = apply_aider_blocks(blocks, tmp_path)
    diff = aider_blocks_to_unified_diff(result)
    assert "diff --git a/new.py b/new.py" in diff
    assert "+print('hi')" in diff
