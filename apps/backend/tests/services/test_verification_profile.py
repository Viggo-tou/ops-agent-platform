from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.verification_profile import (  # noqa: E402
    resolve_verification_profile,
    parse_compiler_errors,
    run_compile_check,
)


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="verification-profile-"))
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="verification-profile-"))
    finally:
        tempfile._os.mkdir = original_mkdir


@pytest.fixture()
def work_dir() -> Path:
    path = _writable_mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_resolves_android_gradle_from_app_build_gradle(work_dir: Path) -> None:
    (work_dir / "app" / "src" / "main").mkdir(parents=True)
    (work_dir / "app" / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (work_dir / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
        "<manifest />\n",
        encoding="utf-8",
    )

    profile = resolve_verification_profile(work_dir, has_tests_yaml=False)

    assert profile.repo_type == "android_gradle"
    assert profile.compile_command is not None
    assert ":app:compileDebugKotlin" in profile.compile_command
    assert "app/build.gradle" in profile.detection_evidence


def test_resolves_python_from_pyproject_toml(work_dir: Path) -> None:
    (work_dir / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    profile = resolve_verification_profile(work_dir, has_tests_yaml=False)

    assert profile.repo_type == "python"
    assert profile.compile_command == ["python", "-m", "compileall", "."]


def test_resolves_node_ts_from_tsconfig(work_dir: Path) -> None:
    (work_dir / "package.json").write_text(
        '{"scripts":{"typecheck":"tsc --noEmit"}}\n',
        encoding="utf-8",
    )
    (work_dir / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    profile = resolve_verification_profile(work_dir, has_tests_yaml=False)

    assert profile.repo_type == "node_ts"
    assert profile.compile_command == ["npm", "run", "typecheck"]


def test_resolves_unknown_when_no_markers_found(work_dir: Path) -> None:
    profile = resolve_verification_profile(work_dir, has_tests_yaml=False)

    assert profile.repo_type == "unknown"
    assert profile.compile_command is None
    assert profile.detection_evidence == []


def test_parse_kotlin_compile_error_extracts_file_line(work_dir: Path) -> None:
    rel = Path("app/src/main/java/com/example/CustomerSignup.kt")
    target = work_dir / rel
    target.parent.mkdir(parents=True)
    target.write_text("package com.example\n", encoding="utf-8")
    output = f"e: {target.as_uri()}:155:51 Unresolved reference: ImeAction\n"

    errors = parse_compiler_errors(output, "android_gradle", repo_root=work_dir)

    assert len(errors) == 1
    assert errors[0].file_path == rel.as_posix()
    assert errors[0].line_number == 155
    assert errors[0].column == 51
    assert errors[0].message == "Unresolved reference: ImeAction"


def test_parse_python_syntax_error_extracts_file_line() -> None:
    output = (
        "***   Sorry: SyntaxError: ('invalid syntax', "
        "('apps/backend/app/foo.py', 12, 1, 'bad', 12, 4))\n"
    )

    errors = parse_compiler_errors(output, "python")

    assert len(errors) == 1
    assert errors[0].file_path == "apps/backend/app/foo.py"
    assert errors[0].line_number == 12
    assert "SyntaxError" in errors[0].message


def test_compile_only_path_skipped_when_repo_type_unknown(work_dir: Path) -> None:
    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise AssertionError("unknown repo type must not execute a command")

    profile = resolve_verification_profile(work_dir, has_tests_yaml=False)

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert result.passed
    assert result.status == "skipped"
    assert result.reason == "unknown_repo_type"
