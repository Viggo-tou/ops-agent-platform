"""L4g: repair-prompt cross-file context tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_l4g_attaches_related_files_for_unresolved_ref():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")
        _write(tmp / "JobPostingViewModel.kt", "val location = \"Remote\"\n")
        _write(tmp / "Job.kt", "data class Job(val location: String)\n")

        section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths={"JobPostingFragment.kt", "JobPostingViewModel.kt", "Job.kt"},
            sandbox_dir=tmp,
        )

    assert "RELATED" in section
    assert "JobPostingViewModel.kt" in section
    assert "Job.kt" in section
    assert "JobPostingFragment.kt" not in section


def test_l4g_skipped_for_non_unresolved_errors():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")
        _write(tmp / "JobPostingViewModel.kt", "val location = \"Remote\"\n")

        section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Syntax error: Expecting comma",
            allowed_paths={"JobPostingFragment.kt", "JobPostingViewModel.kt"},
            sandbox_dir=tmp,
        )

    assert section == ""


def test_l4g_skipped_when_allowed_paths_none_or_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")

        none_section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths=None,
            sandbox_dir=tmp,
        )
        empty_section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths=set(),
            sandbox_dir=tmp,
        )

    assert none_section == ""
    assert empty_section == ""


def test_l4g_truncates_long_files():
    long_content = "x" * 50000
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")
        _write(tmp / "JobPostingViewModel.kt", long_content)

        section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths={"JobPostingFragment.kt", "JobPostingViewModel.kt"},
            sandbox_dir=tmp,
        )

    assert "x" * 3000 in section
    assert "x" * 3001 not in section
    assert "(truncated, original was 50000 chars)" in section


def test_l4g_caps_at_5_related_files():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")
        related_paths = {f"Related{idx:02d}.kt" for idx in range(10)}
        for related_path in related_paths:
            _write(tmp / related_path, f"val location{related_path[7:9]} = \"Remote\"\n")

        section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths={"JobPostingFragment.kt", *related_paths},
            sandbox_dir=tmp,
        )

    assert section.count("=== RELATED ") == 5


def test_l4g_skips_missing_files():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        _write(tmp / "JobPostingFragment.kt", "fun bind() = location\n")
        _write(tmp / "JobPostingViewModel.kt", "val location = \"Remote\"\n")

        section = PrimaryOrchestrator._build_related_files_section(
            rel_path="JobPostingFragment.kt",
            error_msg="Unresolved reference 'location'.",
            allowed_paths={
                "JobPostingFragment.kt",
                "JobPostingViewModel.kt",
                "Missing.kt",
            },
            sandbox_dir=tmp,
        )

    assert "JobPostingViewModel.kt" in section
    assert "Missing.kt" not in section
