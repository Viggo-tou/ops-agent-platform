from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.sandbox import ExecutionSandbox  # noqa: E402
from app.services.test_pipeline import TestPipeline, TestPipelineError  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="test-pipeline-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    # Python's tempfile uses 0o700, which is not writable in this Windows sandbox.
    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="test-pipeline-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _python_command(code: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([sys.executable, "-c", code])
    return shlex.join([sys.executable, "-c", code])


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class TestPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_dir = _writable_mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _sandbox(self, task_id: str = "task-1") -> ExecutionSandbox:
        sandbox = ExecutionSandbox(task_id=task_id, base_dir=str(self.base_dir))
        sandbox.work_dir.mkdir(parents=True, exist_ok=False)
        return sandbox

    def _write_tests_yaml(self, sandbox: ExecutionSandbox, steps: list[dict[str, object]]) -> None:
        lines = ["steps:"]
        for step in steps:
            required = "true" if bool(step.get("required", True)) else "false"
            lines.extend(
                [
                    f"  - name: {step['name']}",
                    f"    command: {_yaml_quote(str(step['command']))}",
                    f"    timeout_seconds: {step.get('timeout_seconds', 10)}",
                    f"    required: {required}",
                ]
            )
        (sandbox.work_dir / "tests.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_pipeline_all_pass(self) -> None:
        sandbox = self._sandbox()
        self._write_tests_yaml(
            sandbox,
            [
                {"name": "first", "command": _python_command('import sys; sys.stdout.write("ok")')},
                {"name": "second", "command": _python_command('import sys; sys.stdout.write("ok")')},
            ],
        )

        result = TestPipeline(sandbox).run()

        self.assertTrue(result.overall_passed)
        self.assertEqual(result.total_steps, 2)
        self.assertEqual(result.passed_count, 2)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.skipped_count, 0)

    def test_pipeline_required_step_fails(self) -> None:
        sandbox = self._sandbox()
        self._write_tests_yaml(
            sandbox,
            [
                {"name": "unit", "command": _python_command("import sys; sys.exit(1)")},
                {"name": "integration", "command": _python_command('import sys; sys.stdout.write("ok")')},
            ],
        )

        result = TestPipeline(sandbox).run()

        self.assertFalse(result.overall_passed)
        self.assertEqual(result.total_steps, 2)
        self.assertEqual(result.passed_count, 0)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(len(result.steps), 1)

    def test_pipeline_optional_step_fails(self) -> None:
        sandbox = self._sandbox()
        self._write_tests_yaml(
            sandbox,
            [
                {
                    "name": "integration",
                    "command": _python_command("import sys; sys.exit(1)"),
                    "required": False,
                },
                {"name": "unit", "command": _python_command('import sys; sys.stdout.write("ok")')},
            ],
        )

        result = TestPipeline(sandbox).run()

        self.assertTrue(result.overall_passed)
        self.assertEqual(result.passed_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.skipped_count, 0)

    def test_pipeline_missing_config(self) -> None:
        sandbox = self._sandbox()

        with self.assertRaises(TestPipelineError):
            TestPipeline(sandbox).run()

    def test_pipeline_empty_steps(self) -> None:
        sandbox = self._sandbox()
        (sandbox.work_dir / "tests.yaml").write_text("steps: []\n", encoding="utf-8")

        result = TestPipeline(sandbox).run()

        self.assertTrue(result.overall_passed)
        self.assertEqual(result.total_steps, 0)
        self.assertEqual(result.passed_count, 0)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.skipped_count, 0)


if __name__ == "__main__":
    unittest.main()
