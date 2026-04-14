from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.services.sandbox import ExecutionSandbox, SandboxError


class TestPipelineError(Exception):
    pass


@dataclass
class TestStepResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    required: bool
    passed: bool


@dataclass
class TestRunResult:
    steps: list[TestStepResult]
    overall_passed: bool
    total_steps: int
    passed_count: int
    failed_count: int
    skipped_count: int
    duration_ms: int


@dataclass(frozen=True)
class _TestStepConfig:
    name: str
    command: str
    timeout_seconds: float
    required: bool


class TestPipeline:
    def __init__(self, sandbox: ExecutionSandbox):
        self.sandbox = sandbox

    def run(
        self,
        *,
        config_path: str = "tests.yaml",
        max_output_bytes: int = 64 * 1024,
    ) -> TestRunResult:
        if not self.sandbox.exists():
            raise TestPipelineError(f"Sandbox does not exist: {self.sandbox.work_dir}")

        started = time.monotonic()
        steps_config = self._load_steps(config_path)
        results: list[TestStepResult] = []
        skipped_count = 0

        for index, step in enumerate(steps_config):
            try:
                raw_result = self.sandbox.run(
                    step.command,
                    timeout_seconds=step.timeout_seconds,
                    max_output_bytes=max_output_bytes,
                )
            except SandboxError as exc:
                raise TestPipelineError(str(exc)) from exc

            exit_code = int(raw_result.get("exit_code", -1))
            timed_out = bool(raw_result.get("timed_out", False))
            passed = exit_code == 0 and not timed_out
            result = TestStepResult(
                name=step.name,
                command=step.command,
                exit_code=exit_code,
                stdout=str(raw_result.get("stdout", "")),
                stderr=str(raw_result.get("stderr", "")),
                duration_ms=int(raw_result.get("duration_ms", 0)),
                timed_out=timed_out,
                required=step.required,
                passed=passed,
            )
            results.append(result)

            if step.required and not passed:
                skipped_count = len(steps_config) - index - 1
                break

        passed_count = sum(1 for result in results if result.passed)
        failed_count = sum(1 for result in results if not result.passed)
        overall_passed = not any(result.required and not result.passed for result in results)
        duration_ms = int((time.monotonic() - started) * 1000)

        return TestRunResult(
            steps=results,
            overall_passed=overall_passed,
            total_steps=len(steps_config),
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            duration_ms=duration_ms,
        )

    def _load_steps(self, config_path: str) -> list[_TestStepConfig]:
        resolved_path = self._resolve_config_path(config_path)
        if not resolved_path.exists():
            raise TestPipelineError(f"Test pipeline config not found: {config_path}")
        if not resolved_path.is_file():
            raise TestPipelineError(f"Test pipeline config is not a file: {config_path}")

        raw_config = _load_yaml_like_mapping(resolved_path.read_text(encoding="utf-8"))
        steps_value = raw_config.get("steps")
        if steps_value is None:
            raise TestPipelineError("Test pipeline config requires a 'steps' list.")
        if not isinstance(steps_value, list):
            raise TestPipelineError("Test pipeline 'steps' must be a list.")

        steps: list[_TestStepConfig] = []
        for index, step_value in enumerate(steps_value, start=1):
            if not isinstance(step_value, Mapping):
                raise TestPipelineError(f"Step {index} must be a mapping.")
            steps.append(_parse_step(step_value, index=index))
        return steps

    def _resolve_config_path(self, config_path: str) -> Path:
        cleaned_path = config_path.strip() or "tests.yaml"
        path = Path(cleaned_path)
        if path.is_absolute():
            raise TestPipelineError("config_path must be relative to the sandbox root.")

        sandbox_root = self.sandbox.work_dir.resolve()
        candidate = (sandbox_root / path).resolve()
        try:
            candidate.relative_to(sandbox_root)
        except ValueError as exc:
            raise TestPipelineError("config_path must stay inside the sandbox root.") from exc

        return candidate


def _parse_step(step_value: Mapping[str, object], *, index: int) -> _TestStepConfig:
    name = str(step_value.get("name") or "").strip()
    command = str(step_value.get("command") or "").strip()
    if not name:
        raise TestPipelineError(f"Step {index} requires a non-empty 'name'.")
    if not command:
        raise TestPipelineError(f"Step {index} requires a non-empty 'command'.")

    timeout_value = step_value.get("timeout_seconds", 60)
    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise TestPipelineError(f"Step {index} timeout_seconds must be numeric.") from exc
    if timeout_seconds <= 0:
        raise TestPipelineError(f"Step {index} timeout_seconds must be greater than zero.")

    required_value = step_value.get("required", True)
    if isinstance(required_value, bool):
        required = required_value
    elif isinstance(required_value, str) and required_value.strip().casefold() in {"true", "false"}:
        required = required_value.strip().casefold() == "true"
    else:
        raise TestPipelineError(f"Step {index} required must be a boolean.")

    return _TestStepConfig(
        name=name,
        command=command,
        timeout_seconds=timeout_seconds,
        required=required,
    )


def _load_yaml_like_mapping(raw_text: str) -> Mapping[str, object]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _parse_minimal_tests_yaml(raw_text)

    loaded = yaml.safe_load(raw_text)  # type: ignore[no-untyped-call]
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TestPipelineError("Test pipeline config must be a mapping.")
    return loaded


def _parse_minimal_tests_yaml(raw_text: str) -> Mapping[str, object]:
    steps: list[dict[str, object]] | None = None
    current_step: dict[str, object] | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("steps:"):
            _, raw_value = stripped.split(":", 1)
            value = raw_value.strip()
            if value == "":
                steps = []
                current_step = None
                continue
            if value == "[]":
                steps = []
                current_step = None
                continue
            raise TestPipelineError("Minimal YAML parser only supports 'steps:' or 'steps: []'.")

        if steps is None:
            raise TestPipelineError("Test pipeline config requires a 'steps' list.")

        if stripped.startswith("- "):
            current_step = {}
            steps.append(current_step)
            remainder = stripped[2:].strip()
            if remainder:
                key, value = _split_yaml_key_value(remainder)
                current_step[key] = _parse_minimal_yaml_scalar(value)
            continue

        if current_step is None:
            raise TestPipelineError("Step attributes must follow a list item.")

        key, value = _split_yaml_key_value(stripped)
        current_step[key] = _parse_minimal_yaml_scalar(value)

    return {"steps": steps if steps is not None else None}


def _split_yaml_key_value(raw_value: str) -> tuple[str, str]:
    if ":" not in raw_value:
        raise TestPipelineError(f"Invalid YAML line: {raw_value}")
    key, value = raw_value.split(":", 1)
    key = key.strip()
    if not key:
        raise TestPipelineError(f"Invalid YAML key in line: {raw_value}")
    return key, value.strip()


def _parse_minimal_yaml_scalar(raw_value: str) -> object:
    value = raw_value.strip()
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value == "[]":
        return []
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return _unquote_minimal_yaml_string(value)
    return value


def _unquote_minimal_yaml_string(value: str) -> str:
    inner = value[1:-1]
    if value[0] == "'":
        return inner.replace("''", "'")
    return inner.replace(r"\"", '"').replace(r"\\", "\\")
