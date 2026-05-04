from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote


RepoType = Literal["android_gradle", "python", "node_js", "node_ts", "rust_cargo", "go", "unknown"]
Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class CompileError:
    file_path: str | None
    line_number: int | None
    column: int | None
    message: str
    severity: Severity

    def to_dict(self, repo_type: str) -> dict[str, object]:
        return {
            "file": self.file_path,
            "line": self.line_number,
            "column": self.column,
            "error": self.message,
            "message": self.message,
            "severity": self.severity,
            "type": repo_type,
        }


@dataclass(frozen=True)
class VerificationProfile:
    repo_type: RepoType
    compile_command: list[str] | None
    syntax_only_command: list[str] | None
    test_command: list[str] | None
    timeout_seconds: int
    detection_evidence: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_type": self.repo_type,
            "compile_command": self.compile_command,
            "syntax_only_command": self.syntax_only_command,
            "test_command": self.test_command,
            "timeout_seconds": self.timeout_seconds,
            "detection_evidence": list(self.detection_evidence),
        }


@dataclass(frozen=True)
class CompileCheckResult:
    passed: bool
    status: Literal["passed", "failed", "skipped"]
    repo_type: str
    command: list[str] | None
    output: str
    errors: list[dict[str, object]]
    timed_out: bool
    duration_ms: int
    reason: str | None = None

    def summary(self) -> str:
        if self.passed:
            if self.status == "skipped":
                return f"Compile verification skipped: {self.reason or 'not applicable'}."
            return "Compile verification passed."
        messages = [
            f"{error.get('file') or 'unknown'}: {error.get('error') or error.get('message')}"
            for error in self.errors[:5]
        ]
        return "Compile verification failed: " + "; ".join(messages)


ANDROID_KOTLIN_ERROR = re.compile(r"^e:\s+(?:file://)?(.+):(\d+):(\d+)\s+(.*)$", re.MULTILINE)
PYTHON_TUPLE_ERROR = re.compile(r"\*\*\*.*?\('([^']+)',\s*(\d+)", re.MULTILINE | re.DOTALL)
PYTHON_FILE_LINE_ERROR = re.compile(r'File "([^"]+)", line (\d+)')
TS_ERROR = re.compile(r"^([^(]+)\((\d+),(\d+)\):\s+error\s+(\S+):\s+(.*)$", re.MULTILINE)
RUST_LOCATION = re.compile(r"^\s*-->\s+([^:]+):(\d+):(\d+)", re.MULTILINE)
GO_ERROR = re.compile(r"^([^:\s]+\.go):(\d+):(?:(\d+):)?\s+(.*)$", re.MULTILINE)
GRADLE_WRAPPERS = {"gradlew", "gradlew.bat"}


def resolve_verification_profile(
    source_path: Path,
    *,
    has_tests_yaml: bool,
) -> VerificationProfile:
    """Detect repo type from filesystem markers and choose compile commands."""
    source_path = Path(source_path)

    android_build = _first_existing(source_path, "app/build.gradle", "app/build.gradle.kts")
    android_manifest = _first_matching(source_path / "app", "AndroidManifest.xml")
    if android_build and android_manifest:
        gradle = _gradle_command(source_path)
        return VerificationProfile(
            repo_type="android_gradle",
            compile_command=[gradle, ":app:compileDebugKotlin", "--quiet", "--no-daemon"],
            syntax_only_command=None,
            test_command=None,
            timeout_seconds=600,
            detection_evidence=[_rel(source_path, android_build), _rel(source_path, android_manifest)],
        )

    python_marker = _first_existing(source_path, "pyproject.toml", "setup.py", "requirements.txt")
    if python_marker:
        return VerificationProfile(
            repo_type="python",
            compile_command=["python", "-m", "compileall", "."],
            syntax_only_command=["python", "-m", "compileall", "."],
            test_command=_python_test_command(source_path, has_tests_yaml),
            timeout_seconds=180,
            detection_evidence=[_rel(source_path, python_marker)],
        )

    package_json = source_path / "package.json"
    tsconfig = source_path / "tsconfig.json"
    if package_json.is_file() and tsconfig.is_file():
        scripts = _package_scripts(package_json)
        return VerificationProfile(
            repo_type="node_ts",
            compile_command=_node_ts_compile_command(scripts),
            syntax_only_command=["npx", "tsc", "--noEmit"],
            test_command=_node_test_command(scripts, has_tests_yaml),
            timeout_seconds=240,
            detection_evidence=[_rel(source_path, package_json), _rel(source_path, tsconfig)],
        )

    if package_json.is_file():
        scripts = _package_scripts(package_json)
        return VerificationProfile(
            repo_type="node_js",
            compile_command=_node_js_compile_command(source_path, scripts),
            syntax_only_command=None,
            test_command=_node_test_command(scripts, has_tests_yaml),
            timeout_seconds=180,
            detection_evidence=[_rel(source_path, package_json)],
        )

    cargo = source_path / "Cargo.toml"
    if cargo.is_file():
        return VerificationProfile(
            repo_type="rust_cargo",
            compile_command=["cargo", "check", "--quiet"],
            syntax_only_command=["cargo", "check", "--quiet"],
            test_command=["cargo", "test", "--quiet"] if has_tests_yaml or (source_path / "tests").is_dir() else None,
            timeout_seconds=240,
            detection_evidence=[_rel(source_path, cargo)],
        )

    go_mod = source_path / "go.mod"
    if go_mod.is_file():
        return VerificationProfile(
            repo_type="go",
            compile_command=["go", "test", "./...", "-run", "^$"],
            syntax_only_command=["go", "test", "./...", "-run", "^$"],
            test_command=["go", "test", "./..."] if has_tests_yaml else None,
            timeout_seconds=180,
            detection_evidence=[_rel(source_path, go_mod)],
        )

    return VerificationProfile(
        repo_type="unknown",
        compile_command=None,
        syntax_only_command=None,
        test_command=None,
        timeout_seconds=60,
        detection_evidence=[],
    )


def parse_compiler_errors(
    stdout: str,
    repo_type: str,
    *,
    repo_root: Path | None = None,
) -> list[CompileError]:
    if repo_type == "android_gradle":
        return _unique_errors(_parse_android_kotlin_errors(stdout, repo_root=repo_root))
    if repo_type == "python":
        return _unique_errors(_parse_python_errors(stdout, repo_root=repo_root))
    if repo_type in {"node_ts", "node_js"}:
        return _unique_errors(_parse_typescript_errors(stdout, repo_root=repo_root))
    if repo_type == "rust_cargo":
        return _unique_errors(_parse_rust_errors(stdout, repo_root=repo_root))
    if repo_type == "go":
        return _unique_errors(_parse_go_errors(stdout, repo_root=repo_root))
    return []


def run_compile_check(
    *,
    sandbox: object,
    profile: VerificationProfile,
    timeout_seconds: int,
    max_output_bytes: int = 64 * 1024,
) -> CompileCheckResult:
    if profile.repo_type == "unknown" or not profile.compile_command:
        return CompileCheckResult(
            passed=True,
            status="skipped",
            repo_type=profile.repo_type,
            command=profile.compile_command,
            output="",
            errors=[],
            timed_out=False,
            duration_ms=0,
            reason="unknown_repo_type",
        )

    sandbox_workdir = Path(getattr(sandbox, "work_dir"))
    resolved_executable = _resolve_executable(profile, sandbox_workdir)
    if resolved_executable is None:
        return CompileCheckResult(
            passed=True,
            status="skipped",
            repo_type=profile.repo_type,
            command=profile.compile_command,
            output="",
            errors=[],
            timed_out=False,
            duration_ms=0,
            reason=_missing_executable_reason(profile.compile_command[0]),
        )

    compile_command = list(profile.compile_command)
    if _is_windows_batch_wrapper(resolved_executable):
        compile_command = ["cmd.exe", "/d", "/c", resolved_executable, *compile_command[1:]]
    else:
        compile_command[0] = resolved_executable

    command = subprocess.list2cmdline(compile_command)
    env = {"GRADLE_OPTS": "-Dorg.gradle.daemon=false"} if profile.repo_type == "android_gradle" else None
    raw_result = sandbox.run(
        command,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        env=env,
    )
    stdout = str(raw_result.get("stdout", ""))
    stderr = str(raw_result.get("stderr", ""))
    output = (stdout + ("\n" if stdout and stderr else "") + stderr)[:max_output_bytes]
    exit_code = int(raw_result.get("exit_code", -1))
    timed_out = bool(raw_result.get("timed_out", False))
    passed = exit_code == 0 and not timed_out
    errors = [
        error.to_dict(profile.repo_type)
        for error in parse_compiler_errors(output, profile.repo_type, repo_root=sandbox_workdir)
    ]
    if not passed and not errors:
        excerpt = _first_output_excerpt(output)
        errors = [
            {
                "file": None,
                "line": None,
                "column": None,
                "error": excerpt or f"compile command exited {exit_code}",
                "message": excerpt or f"compile command exited {exit_code}",
                "severity": "error",
                "type": profile.repo_type,
            }
        ]
    return CompileCheckResult(
        passed=passed,
        status="passed" if passed else "failed",
        repo_type=profile.repo_type,
        command=list(profile.compile_command),
        output=output,
        errors=errors,
        timed_out=timed_out,
        duration_ms=int(raw_result.get("duration_ms", 0)),
        reason=None,
    )


def _resolve_executable(profile: VerificationProfile, sandbox_workdir: Path) -> str | None:
    command = profile.compile_command
    if not command:
        return None

    executable = command[0]
    if _is_repo_local_executable(executable) or _is_gradle_wrapper(executable):
        candidate = _repo_local_executable_path(sandbox_workdir, executable)
        if candidate.exists():
            return str(candidate.resolve())
        # Do not fall back to system Gradle for wrapper commands: the wrapper pins
        # the project toolchain, and a missing wrapper is an environment skip.
        return None

    return shutil.which(executable)


def _is_repo_local_executable(executable: str) -> bool:
    return (
        executable.startswith(("./", ".\\", "/", "\\"))
        or "/" in executable
        or "\\" in executable
    )


def _repo_local_executable_path(sandbox_workdir: Path, executable: str) -> Path:
    if executable.startswith(("/", "\\")):
        executable = executable.lstrip("/\\")
    return sandbox_workdir / executable


def _is_gradle_wrapper(executable: str) -> bool:
    return Path(executable).name.casefold() in GRADLE_WRAPPERS


def _is_windows_batch_wrapper(executable: str) -> bool:
    return Path(executable).suffix.casefold() == ".bat"


def _missing_executable_reason(executable: str) -> Literal["toolchain_missing", "wrapper_missing"]:
    if _is_repo_local_executable(executable) or _is_gradle_wrapper(executable):
        return "wrapper_missing"
    return "toolchain_missing"


def _parse_android_kotlin_errors(stdout: str, *, repo_root: Path | None) -> list[CompileError]:
    errors: list[CompileError] = []
    for match in ANDROID_KOTLIN_ERROR.finditer(stdout):
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=int(match.group(3)),
                message=match.group(4).strip(),
                severity="error",
            )
        )
    return errors


def _parse_python_errors(stdout: str, *, repo_root: Path | None) -> list[CompileError]:
    errors: list[CompileError] = []
    syntax_message = _first_matching_line(stdout, "SyntaxError") or _first_output_excerpt(stdout)
    for match in PYTHON_TUPLE_ERROR.finditer(stdout):
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=None,
                message=syntax_message or "Python syntax error",
                severity="error",
            )
        )
    if errors:
        return errors
    for match in PYTHON_FILE_LINE_ERROR.finditer(stdout):
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=None,
                message=syntax_message or "Python syntax error",
                severity="error",
            )
        )
    return errors


def _parse_typescript_errors(stdout: str, *, repo_root: Path | None) -> list[CompileError]:
    errors: list[CompileError] = []
    for match in TS_ERROR.finditer(stdout):
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1).strip(), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=int(match.group(3)),
                message=f"{match.group(4)}: {match.group(5).strip()}",
                severity="error",
            )
        )
    return errors


def _parse_rust_errors(stdout: str, *, repo_root: Path | None) -> list[CompileError]:
    errors: list[CompileError] = []
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        match = RUST_LOCATION.match(line)
        if not match:
            continue
        message = "Rust compile error"
        for prior in range(index - 1, max(-1, index - 5), -1):
            if lines[prior].lstrip().startswith("error"):
                message = lines[prior].strip()
                break
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=int(match.group(3)),
                message=message,
                severity="error",
            )
        )
    return errors


def _parse_go_errors(stdout: str, *, repo_root: Path | None) -> list[CompileError]:
    errors: list[CompileError] = []
    for match in GO_ERROR.finditer(stdout):
        errors.append(
            CompileError(
                file_path=_normalize_error_path(match.group(1), repo_root=repo_root),
                line_number=int(match.group(2)),
                column=int(match.group(3)) if match.group(3) else None,
                message=match.group(4).strip(),
                severity="error",
            )
        )
    return errors


def _first_existing(root: Path, *relative_paths: str) -> Path | None:
    for relative_path in relative_paths:
        candidate = root / relative_path
        if candidate.is_file():
            return candidate
    return None


def _first_matching(root: Path, filename: str) -> Path | None:
    if not root.exists():
        return None
    try:
        for candidate in root.rglob(filename):
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def _gradle_command(root: Path) -> str:
    if (root / "gradlew.bat").is_file():
        return "gradlew.bat"
    return "./gradlew"


def _package_scripts(package_json: Path) -> dict[str, str]:
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = raw.get("scripts") if isinstance(raw, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items() if isinstance(value, str)}


def _node_ts_compile_command(scripts: dict[str, str]) -> list[str]:
    if "typecheck" in scripts:
        return ["npm", "run", "typecheck"]
    if "build" in scripts:
        return ["npm", "run", "build"]
    return ["npx", "tsc", "--noEmit"]


def _node_js_compile_command(root: Path, scripts: dict[str, str]) -> list[str]:
    if "build" in scripts:
        return ["npm", "run", "build"]
    if "lint" in scripts:
        return ["npm", "run", "lint"]
    if (root / "index.js").is_file():
        return ["node", "--check", "index.js"]
    return ["npm", "test", "--", "--runInBand"]


def _node_test_command(scripts: dict[str, str], has_tests_yaml: bool) -> list[str] | None:
    if has_tests_yaml:
        return None
    if "test" in scripts:
        return ["npm", "test"]
    return None


def _python_test_command(root: Path, has_tests_yaml: bool) -> list[str] | None:
    if has_tests_yaml:
        return None
    if (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        return ["python", "-m", "pytest"]
    return None


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _normalize_error_path(path: str, *, repo_root: Path | None) -> str:
    cleaned = unquote(path.strip()).replace("\\", "/")
    if cleaned.startswith("/") and len(cleaned) >= 4 and cleaned[2] == ":":
        cleaned = cleaned[1:]
    if repo_root is not None:
        try:
            root = repo_root.resolve()
            resolved = Path(cleaned).resolve()
            return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            root_text = repo_root.resolve().as_posix().rstrip("/")
            cleaned_lower = cleaned.casefold()
            root_lower = root_text.casefold()
            if cleaned_lower.startswith(root_lower + "/"):
                return cleaned[len(root_text) + 1 :]
    return cleaned


def _unique_errors(errors: list[CompileError]) -> list[CompileError]:
    seen: set[tuple[object, ...]] = set()
    unique: list[CompileError] = []
    for error in errors:
        key = (error.file_path, error.line_number, error.column, error.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(error)
    return unique


def _first_matching_line(output: str, needle: str) -> str | None:
    for line in output.splitlines():
        if needle in line:
            return line.strip()
    return None


def _first_output_excerpt(output: str) -> str:
    normalized = " ".join(output.strip().split())
    return normalized[:1000]
