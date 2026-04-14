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

from app.services.sandbox import ExecutionSandbox, SandboxError, _sanitize_diff  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    # Python's tempfile uses 0o700, which is not writable in this Windows sandbox.
    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def _python_command(code: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([sys.executable, "-c", code])
    return shlex.join([sys.executable, "-c", code])


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class ExecutionSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_dir = _writable_mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _sandbox(self, task_id: str = "task-1") -> ExecutionSandbox:
        sandbox = ExecutionSandbox(task_id=task_id, base_dir=str(self.base_dir))
        sandbox.work_dir.mkdir(parents=True, exist_ok=False)
        return sandbox

    def test_run_command_success(self) -> None:
        sandbox = self._sandbox()
        (sandbox.work_dir / "message.txt").write_text("hello\n", encoding="utf-8")

        result = sandbox.run(
            _python_command("from pathlib import Path; import sys; sys.stdout.write(Path('message.txt').read_text())")
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["stdout"])
        self.assertFalse(result["timed_out"])

    def test_run_command_nonzero_exit(self) -> None:
        sandbox = self._sandbox()

        result = sandbox.run(_python_command("import sys; sys.exit(1)"))

        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["timed_out"])

    def test_run_command_timeout(self) -> None:
        sandbox = self._sandbox()

        result = sandbox.run(_python_command("import time; time.sleep(2)"), timeout_seconds=0.5)

        self.assertEqual(result["exit_code"], -1)
        self.assertTrue(result["timed_out"])
        self.assertIn("timed out", result["stderr"])

    def test_run_command_path_traversal_blocked(self) -> None:
        sandbox = self._sandbox()

        with self.assertRaises(SandboxError):
            sandbox.run(_python_command("print('blocked')"), cwd="../..")

    def test_sandbox_teardown(self) -> None:
        sandbox = self._sandbox()
        self.assertTrue(sandbox.work_dir.exists())

        sandbox.teardown()

        self.assertFalse(sandbox.work_dir.exists())

    def test_run_command_output_truncation(self) -> None:
        sandbox = self._sandbox()

        result = sandbox.run(
            _python_command("import sys; sys.stdout.write('x' * 100)"),
            max_output_bytes=10,
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "x" * 10)

    def test_sanitize_diff_preserves_blank_context_lines(self) -> None:
        self.assertEqual(_sanitize_diff(" \n"), " \n")

    def test_clone_local_non_git_dir(self) -> None:
        source_dir = self.base_dir / "plain-source"
        source_dir.mkdir()
        (source_dir / "message.txt").write_text("hello from source\n", encoding="utf-8")
        sandbox = ExecutionSandbox(task_id="copy-task", base_dir=str(self.base_dir))

        result = sandbox.clone(str(source_dir))

        self.assertEqual(result["method"], "copytree")
        self.assertEqual(result["source"], str(source_dir))
        self.assertTrue((sandbox.work_dir / "message.txt").is_file())
        self.assertTrue((sandbox.work_dir / ".git").is_dir())

    def test_clone_local_git_dir_still_works(self) -> None:
        source_dir = self.base_dir / "git-source"
        source_dir.mkdir()
        _git(source_dir, "init")
        _git(source_dir, "config", "user.email", "sandbox-tests@example.com")
        _git(source_dir, "config", "user.name", "Sandbox Tests")
        (source_dir / "message.txt").write_text("hello from git\n", encoding="utf-8")
        _git(source_dir, "add", "message.txt")
        _git(source_dir, "commit", "-m", "Initial commit")
        sandbox = ExecutionSandbox(task_id="clone-task", base_dir=str(self.base_dir))

        result = sandbox.clone(str(source_dir))

        self.assertIn(result["method"], {"git_clone", "git_clone_copytree_fallback"})
        self.assertEqual(result["repo_url"], str(source_dir))
        self.assertTrue((sandbox.work_dir / "message.txt").is_file())
        self.assertTrue((sandbox.work_dir / ".git").is_dir())


if __name__ == "__main__":
    unittest.main()
