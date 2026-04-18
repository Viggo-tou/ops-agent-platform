"""Unit tests for compile_gate service (T-040 defense line 5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.compile_gate import run_compile_gate


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


def test_valid_js_passes(sandbox: Path) -> None:
    (sandbox / "src" / "ok.js").write_text("const x = 1;\n", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/ok.js"])
    assert result.passed


def test_invalid_js_fails(sandbox: Path) -> None:
    (sandbox / "src" / "bad.js").write_text("const x = {;\n", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/bad.js"])
    assert not result.passed
    assert result.errors[0]["file"] == "src/bad.js"
    assert result.errors[0]["type"] == "js"


def test_valid_py_passes(sandbox: Path) -> None:
    (sandbox / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/ok.py"])
    assert result.passed


def test_invalid_py_fails(sandbox: Path) -> None:
    (sandbox / "src" / "bad.py").write_text("def f(\n", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/bad.py"])
    assert not result.passed
    assert result.errors[0]["file"] == "src/bad.py"
    assert result.errors[0]["type"] == "py"


def test_nonexistent_file_skipped(sandbox: Path) -> None:
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/ghost.js"])
    assert result.passed
    assert result.errors == []


def test_unsupported_extension_skipped(sandbox: Path) -> None:
    (sandbox / "src" / "data.json").write_text("{bad json", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/data.json"])
    assert result.passed


def test_mixed_valid_and_invalid(sandbox: Path) -> None:
    (sandbox / "src" / "ok.js").write_text("const x = 1;\n", encoding="utf-8")
    (sandbox / "src" / "bad.py").write_text("def f(\n", encoding="utf-8")
    result = run_compile_gate(
        sandbox_dir=sandbox,
        changed_files=["src/ok.js", "src/bad.py"],
    )
    assert not result.passed
    assert len(result.errors) == 1
    assert result.errors[0]["file"] == "src/bad.py"


def test_empty_changed_files(sandbox: Path) -> None:
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=[])
    assert result.passed


def test_summary_on_pass(sandbox: Path) -> None:
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=[])
    assert "passed" in result.summary().lower()


def test_summary_on_fail(sandbox: Path) -> None:
    (sandbox / "src" / "bad.py").write_text("def (\n", encoding="utf-8")
    result = run_compile_gate(sandbox_dir=sandbox, changed_files=["src/bad.py"])
    assert "failed" in result.summary().lower()
