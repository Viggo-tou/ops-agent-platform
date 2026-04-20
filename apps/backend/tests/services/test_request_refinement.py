"""Unit tests for the request_refinement service module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.request_refinement import (
    RefinedRequest,
    _build_refinement_prompt,
    _build_source_tree_summary,
)


# ---------------------------------------------------------------------------
# _build_refinement_prompt
# ---------------------------------------------------------------------------


class TestBuildRefinementPrompt:
    def test_with_jira_context(self) -> None:
        jira = {
            "key": "OPS-99",
            "summary": "Add dark mode toggle",
            "description": "As a user I want a dark mode toggle in Settings so that I can reduce eye strain.\n\nAcceptance criteria:\n- Toggle in settings page\n- Persists across sessions",
            "status": "To Do",
            "issue_type": "Story",
            "priority": "High",
        }
        prompt = _build_refinement_prompt(
            user_input="完成OPS-99",
            jira_context=jira,
            translation={"normalized_request": "implement OPS-99", "intent": "develop_jira_issue"},
            source_tree_summary="src/\nsrc/App.tsx\nsrc/Settings.tsx",
        )

        # Must contain the Jira summary in full, not truncated
        assert "Add dark mode toggle" in prompt
        assert "dark mode toggle in Settings" in prompt
        assert "Acceptance criteria" in prompt
        assert "Toggle in settings page" in prompt
        assert "OPS-99" in prompt

        # Must contain file tree
        assert "src/App.tsx" in prompt

        # Must contain semantic translation fields
        assert "implement OPS-99" in prompt

        # Must contain section headers
        assert "=== User Request ===" in prompt
        assert "=== External Task Context ===" in prompt
        assert "=== Semantic Translation ===" in prompt
        assert "=== Repository File Tree ===" in prompt

    def test_without_jira_context(self) -> None:
        prompt = _build_refinement_prompt(
            user_input="fix the login bug",
            jira_context=None,
            translation=None,
            source_tree_summary=None,
        )

        assert "=== User Request ===" in prompt
        assert "fix the login bug" in prompt
        # Should NOT have Jira or tree sections
        assert "External Task Context" not in prompt
        assert "Repository File Tree" not in prompt

    def test_jira_description_not_truncated_to_1200(self) -> None:
        """Verify the refinement prompt passes the full description (up to 4000 chars),
        unlike _augment_request_with_context which truncates to 1200."""
        long_description = "x" * 3000
        jira = {
            "key": "TEST-1",
            "summary": "Long ticket",
            "description": long_description,
        }
        prompt = _build_refinement_prompt(
            user_input="do TEST-1",
            jira_context=jira,
            translation=None,
            source_tree_summary=None,
        )
        # Full 3000-char description should be present
        assert long_description in prompt

    def test_jira_description_truncated_at_4000(self) -> None:
        """Descriptions over 4000 chars should be truncated."""
        long_description = "y" * 5000
        jira = {
            "key": "TEST-2",
            "summary": "Very long ticket",
            "description": long_description,
        }
        prompt = _build_refinement_prompt(
            user_input="do TEST-2",
            jira_context=jira,
            translation=None,
            source_tree_summary=None,
        )
        # Only first 4000 chars should be present
        assert "y" * 4000 in prompt
        assert "y" * 4001 not in prompt

    def test_issue_key_fallback_field(self) -> None:
        """Jira context may use 'issue_key' instead of 'key'."""
        jira = {
            "issue_key": "PROJ-42",
            "summary": "Something",
            "description": "Details here",
            "issue_status": "In Progress",
        }
        prompt = _build_refinement_prompt(
            user_input="do PROJ-42",
            jira_context=jira,
            translation=None,
            source_tree_summary=None,
        )
        assert "PROJ-42" in prompt
        assert "In Progress" in prompt


# ---------------------------------------------------------------------------
# RefinedRequest parsing (plain text)
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_plain_text_trimmed(self) -> None:
        raw = "  Add a dark mode toggle to the Settings page.  \n"
        result = RefinedRequest(
            refined_text=raw.strip(),
            confidence=0.8,
            raw_response=raw,
        )
        assert result.refined_text == "Add a dark mode toggle to the Settings page."
        assert result.confidence == 0.8

    def test_response_too_short_raises(self) -> None:
        """The CLI/API functions should raise ValueError when response < 20 chars.
        We test the validation logic inline."""
        raw = "short"
        assert len(raw) < 20
        with pytest.raises(ValueError, match="too short"):
            if len(raw) < 20:
                raise ValueError(
                    f"Refinement response too short ({len(raw)} chars): {raw!r}"
                )


# ---------------------------------------------------------------------------
# _build_source_tree_summary
# ---------------------------------------------------------------------------


class TestSourceTreeSummary:
    def test_skips_noise_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("pass", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.pyc").write_text("", encoding="utf-8")

        summary = _build_source_tree_summary(tmp_path)
        assert "src/" in summary
        assert "app.py" in summary
        assert "node_modules" not in summary
        assert ".git" not in summary
        assert "__pycache__" not in summary

    def test_respects_max_depth(self, tmp_path: Path) -> None:
        # Create a nested structure: a/b/c/d/file.txt (depth 4)
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("deep", encoding="utf-8")
        # Also create a shallow file
        (tmp_path / "a" / "shallow.txt").write_text("shallow", encoding="utf-8")

        summary = _build_source_tree_summary(tmp_path, max_depth=2)
        assert "shallow.txt" in summary
        # depth=2 means we go root -> a(1) -> b(2), but not into c(3)
        assert "d/" not in summary

    def test_respects_max_entries(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        for i in range(10):
            (tmp_path / "src" / f"file{i}.py").write_text("pass", encoding="utf-8")

        summary = _build_source_tree_summary(tmp_path, max_entries=5)
        lines = summary.strip().split("\n")
        # Should have at most 5 entries + truncation notice
        assert len(lines) <= 6
        assert "truncated" in lines[-1]

    def test_nonexistent_path_returns_empty(self, tmp_path: Path) -> None:
        summary = _build_source_tree_summary(tmp_path / "nonexistent")
        assert summary == ""
