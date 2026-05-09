"""Tests for codegen_playbooks router.

Each test uses a temporary playbook directory so we never depend on
the live docs/ tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.codegen_playbooks import (
    CodegenPlaybook,
    _parse_frontmatter,
    all_playbooks,
    rebuild_index,
    render_for_prompt,
    select_playbooks,
)


def _write_playbook(
    dir_: Path,
    name: str,
    *,
    language: str = "any",
    applies_to: list[str] | None = None,
    priority: str = "medium",
    body: str = "rule body",
) -> Path:
    applies_to = applies_to or ["any"]
    applies_lines = "\n".join(f"  - {pat}" for pat in applies_to)
    text = (
        "---\n"
        f"language: {language}\n"
        "applies_to:\n"
        f"{applies_lines}\n"
        "audience: codegen-llm\n"
        f"priority: {priority}\n"
        "---\n\n"
        f"{body}\n"
    )
    path = dir_ / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- frontmatter parser -------------------------------------------------------


def test_frontmatter_parses_scalars_and_lists():
    text = (
        "---\n"
        "language: python\n"
        "applies_to:\n"
        "  - '*.py'\n"
        "  - pyproject.toml\n"
        "priority: high\n"
        "---\n"
        "body content"
    )
    meta, body = _parse_frontmatter(text)
    assert meta["language"] == "python"
    assert meta["applies_to"] == ["*.py", "pyproject.toml"]
    assert meta["priority"] == "high"
    assert body == "body content"


def test_frontmatter_missing_returns_empty():
    text = "no frontmatter here\nplain content"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_frontmatter_strips_quotes():
    text = "---\nname: \"quoted\"\n---\nbody"
    meta, _ = _parse_frontmatter(text)
    assert meta["name"] == "quoted"


# --- index build --------------------------------------------------------------


def test_rebuild_index_skips_files_without_frontmatter(tmp_path):
    _write_playbook(tmp_path, "good", language="python")
    (tmp_path / "bare.md").write_text("just text, no frontmatter\n", encoding="utf-8")
    count = rebuild_index(tmp_path)
    assert count == 1
    names = {p.name for p in all_playbooks()}
    assert names == {"good"}


def test_rebuild_index_normalizes_unknown_language(tmp_path):
    _write_playbook(tmp_path, "weird", language="cobol")
    rebuild_index(tmp_path)
    [pb] = all_playbooks()
    assert pb.language == "any"


def test_rebuild_index_handles_missing_directory(tmp_path):
    nonexistent = tmp_path / "nope"
    count = rebuild_index(nonexistent)
    assert count == 0
    assert all_playbooks() == []


# --- selection ----------------------------------------------------------------


def test_select_high_any_always_included(tmp_path):
    _write_playbook(tmp_path, "rules", language="any", priority="high")
    _write_playbook(tmp_path, "extra", language="any", priority="medium")
    rebuild_index(tmp_path)

    selected = select_playbooks(language="python")
    names = [p.name for p in selected]
    assert "rules" in names
    assert "extra" not in names  # medium-any is not auto-included


def test_select_language_match(tmp_path):
    _write_playbook(tmp_path, "py", language="python")
    _write_playbook(tmp_path, "kt", language="kotlin")
    rebuild_index(tmp_path)

    selected = select_playbooks(language="python")
    assert [p.name for p in selected] == ["py"]


def test_select_glob_match_via_applies_to(tmp_path):
    _write_playbook(
        tmp_path,
        "django",
        language="python",
        applies_to=["*/models.py", "*/views.py"],
    )
    _write_playbook(tmp_path, "other", language="python")
    rebuild_index(tmp_path)

    selected = select_playbooks(
        language="python", file_paths=["app/users/models.py"]
    )
    names = {p.name for p in selected}
    assert "django" in names
    assert "other" in names  # also matches via language=python


def test_select_priority_ordering(tmp_path):
    _write_playbook(tmp_path, "low_one", language="python", priority="low")
    _write_playbook(tmp_path, "high_one", language="python", priority="high")
    _write_playbook(tmp_path, "med_one", language="python", priority="medium")
    rebuild_index(tmp_path)

    selected = select_playbooks(language="python")
    assert [p.name for p in selected] == ["high_one", "med_one", "low_one"]


def test_select_no_language_still_returns_high_any(tmp_path):
    _write_playbook(tmp_path, "global", language="any", priority="high")
    rebuild_index(tmp_path)

    selected = select_playbooks(language="")
    assert [p.name for p in selected] == ["global"]


def test_glob_pattern_any_does_not_alone_match(tmp_path):
    _write_playbook(tmp_path, "x", language="kotlin", applies_to=["any"])
    rebuild_index(tmp_path)

    selected = select_playbooks(language="python", file_paths=["foo.py"])
    assert selected == []  # "any" is not a glob, so no file-pattern match


def test_render_for_prompt_outputs_section_headers(tmp_path):
    _write_playbook(
        tmp_path, "diff", language="any", priority="high", body="rule about diff format"
    )
    _write_playbook(
        tmp_path, "py", language="python", priority="medium", body="python rule"
    )
    rebuild_index(tmp_path)
    selected = select_playbooks(language="python")
    rendered = render_for_prompt(selected)
    assert "## Playbook: diff" in rendered
    assert "## Playbook: py" in rendered
    # high priority comes first
    assert rendered.index("## Playbook: diff") < rendered.index("## Playbook: py")
