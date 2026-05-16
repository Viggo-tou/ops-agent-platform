from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import verification_profile as verification_profile_service  # noqa: E402
from app.services.verification_profile import (  # noqa: E402
    NODE_DEPENDENCY_INFRA_REASONS,
    VerificationProfile,
    _resolve_executable,
    parse_compiler_errors,
    resolve_verification_profile,
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
    assert profile.compile_command == ["python", "-m", "compileall", "-q", "-x", r"\\.git", "."]


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


def test_parse_node_eslint_block_extracts_file_line(work_dir: Path) -> None:
    rel = Path("src/pages/UserVerification.js")
    target = work_dir / rel
    target.parent.mkdir(parents=True)
    target.write_text("export default function UserVerification() {}\n", encoding="utf-8")
    output = f"""Failed to compile.

[eslint]
{rel.as_posix()}
  Line 42:41:  React Hook "useState" is called conditionally. React Hooks must be called in the exact same order in every component render  react-hooks/rules-of-hooks
  Line 53:3:  React Hook "useEffect" is called conditionally. React Hooks must be called in the exact same order in every component render  react-hooks/rules-of-hooks
"""

    errors = parse_compiler_errors(output, "node_js", repo_root=work_dir)

    assert [error.file_path for error in errors] == [rel.as_posix(), rel.as_posix()]
    assert [error.line_number for error in errors] == [42, 53]
    assert errors[0].column == 41
    assert "rules-of-hooks" in errors[0].message


def test_parse_node_eslint_flat_summary_extracts_file_line(work_dir: Path) -> None:
    rel = Path("src/pages/UserVerification.js")
    target = work_dir / rel
    target.parent.mkdir(parents=True)
    target.write_text("export default function UserVerification() {}\n", encoding="utf-8")
    output = (
        "[eslint] src\\pages\\UserVerification.js "
        "Line 42:41: React Hook \"useState\" is called conditionally. "
        "React Hooks must be called in the exact same order in every component render "
        "react-hooks/rules-of-hooks "
        "Line 53:3: React Hook \"useEffect\" is called conditionally. "
        "React Hooks must be called in the exact same order in every component render "
        "react-hooks/rules-of-hooks Search for the keywords to learn more about each error."
    )

    errors = parse_compiler_errors(output, "node_js", repo_root=work_dir)

    assert [error.file_path for error in errors] == [rel.as_posix(), rel.as_posix()]
    assert [error.line_number for error in errors] == [42, 53]
    assert errors[1].column == 3
    assert "useEffect" in errors[1].message


def test_parse_node_eslint_syntax_error_extracts_file_line(work_dir: Path) -> None:
    rel = Path("src/data/mockUsers.js")
    target = work_dir / rel
    target.parent.mkdir(parents=True)
    target.write_text("const mockUsers = [\n  {\n];\n", encoding="utf-8")
    output = (
        '[eslint] src\\data\\mockUsers.js Syntax error: Unexpected token, '
        'expected "," (22:0) (22:undefined)'
    )

    errors = parse_compiler_errors(output, "node_js", repo_root=work_dir)

    assert len(errors) == 1
    assert errors[0].file_path == rel.as_posix()
    assert errors[0].line_number == 22
    assert errors[0].column == 0
    assert errors[0].message == 'Syntax error: Unexpected token, expected ","'


def test_parse_react_default_import_error_locates_import_site(work_dir: Path) -> None:
    source = work_dir / "src" / "pages" / "Dashboard.js"
    source.parent.mkdir(parents=True)
    source.write_text(
        'import UserContext from "../context/UserContext";\n'
        "export default function Dashboard() { return null; }\n",
        encoding="utf-8",
    )
    context = work_dir / "src" / "context" / "UserContext.js"
    context.parent.mkdir(parents=True)
    context.write_text("export const useUser = () => null;\n", encoding="utf-8")
    output = (
        "Failed to compile. Attempted import error: '../context/UserContext' "
        "does not contain a default export (imported as 'UserContext')."
    )

    errors = parse_compiler_errors(output, "node_js", repo_root=work_dir)

    assert len(errors) == 1
    assert errors[0].file_path == "src/pages/Dashboard.js"
    assert errors[0].line_number == 1
    assert errors[0].column == 1
    assert "does not contain a default export" in errors[0].message


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


def test_resolve_executable_finds_repo_local_gradlew(work_dir: Path) -> None:
    wrapper = work_dir / "gradlew"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    profile = _compile_profile("android_gradle", ["./gradlew", ":app:compileDebugKotlin"])

    resolved = _resolve_executable(profile, work_dir)

    assert resolved == str(wrapper.resolve())


def test_resolve_executable_returns_none_for_missing_wrapper(work_dir: Path) -> None:
    profile = _compile_profile("android_gradle", ["gradlew.bat", ":app:compileDebugKotlin"])

    resolved = _resolve_executable(profile, work_dir)

    assert resolved is None


def test_resolve_executable_uses_shutil_which_for_plain_command(
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
) -> None:
    resolved_python = str(work_dir / "python.exe")
    profile = _compile_profile("python", ["python", "-m", "compileall", "."])

    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        lambda executable: resolved_python if executable == "python" else None,
    )

    resolved = _resolve_executable(profile, work_dir)

    assert resolved == resolved_python


def test_run_compile_check_returns_skipped_when_wrapper_missing(work_dir: Path) -> None:
    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise AssertionError("missing wrapper must not execute a command")

    profile = _compile_profile("android_gradle", ["./gradlew", ":app:compileDebugKotlin"])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert result.passed
    assert result.status == "skipped"
    assert result.reason == "wrapper_missing"
    assert result.output == ""
    assert result.errors == []


def test_run_compile_check_returns_skipped_when_toolchain_missing(
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
) -> None:
    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise AssertionError("missing toolchain must not execute a command")

    monkeypatch.setattr(verification_profile_service.shutil, "which", lambda executable: None)
    profile = _compile_profile("python", ["python", "-m", "compileall", "."])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert result.passed
    assert result.status == "skipped"
    assert result.reason == "toolchain_missing"
    assert result.output == ""
    assert result.errors == []


def test_run_compile_check_invokes_via_cmd_exe_on_windows_bat_wrapper(work_dir: Path) -> None:
    wrapper = work_dir / "gradlew.bat"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    commands: list[str] = []

    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, command, **kwargs):  # noqa: ANN001, ANN003
            commands.append(command)
            return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False, "duration_ms": 12}

    profile = _compile_profile("android_gradle", ["gradlew.bat", ":app:compileDebugKotlin", "--quiet"])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    expected_prefix = subprocess.list2cmdline(["cmd.exe", "/d", "/c", str(wrapper.resolve())])
    assert result.passed
    assert commands[0].startswith(expected_prefix)
    assert ":app:compileDebugKotlin" in commands[0]


def test_run_compile_check_hydrates_missing_node_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
) -> None:
    (work_dir / "package.json").write_text('{"scripts":{"build":"react-scripts build"}}\n', encoding="utf-8")
    (work_dir / "package-lock.json").write_text("{}\n", encoding="utf-8")
    commands: list[str] = []

    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, command, **kwargs):  # noqa: ANN001, ANN003
            commands.append(command)
            if "npm ci" in command:
                (self.work_dir / "node_modules" / ".bin").mkdir(parents=True)
                return {"stdout": "installed", "stderr": "", "exit_code": 0, "timed_out": False, "duration_ms": 30}
            return {"stdout": "built", "stderr": "", "exit_code": 0, "timed_out": False, "duration_ms": 12}

    monkeypatch.setattr(verification_profile_service.shutil, "which", lambda executable: f"/tools/{executable}")
    profile = _compile_profile("node_js", ["npm", "run", "build"])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert result.passed
    assert len(commands) == 2
    assert "npm ci" in commands[0]
    assert "npm run build" in commands[1]


def test_run_compile_check_reports_node_dependency_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
) -> None:
    (work_dir / "package.json").write_text('{"scripts":{"build":"react-scripts build"}}\n', encoding="utf-8")
    (work_dir / "package-lock.json").write_text("{}\n", encoding="utf-8")

    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, command, **kwargs):  # noqa: ANN001, ANN003
            return {"stdout": "", "stderr": "registry unavailable", "exit_code": 1, "timed_out": False, "duration_ms": 40}

    monkeypatch.setattr(verification_profile_service.shutil, "which", lambda executable: f"/tools/{executable}")
    profile = _compile_profile("node_js", ["npm", "run", "build"])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert not result.passed
    assert result.reason in NODE_DEPENDENCY_INFRA_REASONS
    assert result.errors[0]["type"] == "node_dependency_install"


def test_run_compile_check_reports_node_build_timeout_as_infra(
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
) -> None:
    (work_dir / "package.json").write_text('{"scripts":{"build":"react-scripts build"}}\n', encoding="utf-8")
    (work_dir / "node_modules").mkdir()

    class FakeSandbox:
        def __init__(self, root: Path):
            self.work_dir = root

        def run(self, command, **kwargs):  # noqa: ANN001, ANN003
            return {
                "stdout": "Creating an optimized production build...",
                "stderr": "",
                "exit_code": -1,
                "timed_out": True,
                "duration_ms": 240000,
            }

    monkeypatch.setattr(verification_profile_service.shutil, "which", lambda executable: f"/tools/{executable}")
    profile = _compile_profile("node_js", ["npm", "run", "build"])

    result = run_compile_check(
        sandbox=FakeSandbox(work_dir),
        profile=profile,
        timeout_seconds=10,
    )

    assert not result.passed
    assert result.reason == "node_compile_timed_out"
    assert result.reason in NODE_DEPENDENCY_INFRA_REASONS


def _compile_profile(repo_type: str, compile_command: list[str]) -> VerificationProfile:
    return VerificationProfile(
        repo_type=repo_type,  # type: ignore[arg-type]
        compile_command=compile_command,
        syntax_only_command=None,
        test_command=None,
        timeout_seconds=60,
        detection_evidence=[],
    )
