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

from app.core.config import get_settings  # noqa: E402
from app.services.sandbox import ExecutionSandbox, SandboxError, _is_ascii_path, _sanitize_diff  # noqa: E402


def _writable_mkdtemp() -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    # Python's tempfile uses 0o700, which is not writable in this Windows sandbox.
    tempfile._os.mkdir = mkdir_with_write_access
    try:
        candidate_roots = []
        if os.environ.get("OPS_AGENT_TEST_SANDBOX_ROOT"):
            candidate_roots.append(Path(os.environ["OPS_AGENT_TEST_SANDBOX_ROOT"]))
        candidate_roots.extend([Path(tempfile.gettempdir()), Path.home() / ".ops-agent-sandbox-tests", BACKEND_ROOT])

        for root in candidate_roots:
            if not _is_ascii_path(root):
                continue
            try:
                root.mkdir(parents=True, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=str(root)))
            except OSError:
                continue

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
        self.settings = get_settings()
        self._original_sandbox_external_root = self.settings.sandbox_external_root
        self.settings.sandbox_external_root = None

    def tearDown(self) -> None:
        self.settings.sandbox_external_root = self._original_sandbox_external_root
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _sandbox(self, task_id: str = "task-1") -> ExecutionSandbox:
        sandbox = ExecutionSandbox(task_id=task_id, base_dir=str(self.base_dir))
        sandbox.work_dir.mkdir(parents=True, exist_ok=False)
        return sandbox

    def test_sandbox_uses_external_root_when_configured(self) -> None:
        external_root = "D:/TestSandbox" if os.name == "nt" else "/tmp/TestSandbox"
        self.settings.sandbox_external_root = external_root

        sandbox = ExecutionSandbox(task_id="external-task", base_dir="data/sandboxes")

        self.assertEqual(sandbox.work_dir, Path(external_root) / "external-task")

    def test_sandbox_falls_back_to_relative_when_no_external_root(self) -> None:
        self.settings.sandbox_external_root = None

        sandbox = ExecutionSandbox(task_id="relative-task", base_dir="data/sandboxes")

        self.assertEqual(sandbox.work_dir, Path("data/sandboxes") / "relative-task")

    def test_external_root_must_be_absolute_path(self) -> None:
        self.settings.sandbox_external_root = "relative/sandboxes"

        with self.assertRaises(ValueError):
            ExecutionSandbox(task_id="relative-external-root", base_dir=str(self.base_dir))

    def test_ascii_path_helper_detects_chinese(self) -> None:
        self.assertFalse(_is_ascii_path(Path("D:/项目")))
        self.assertTrue(_is_ascii_path(Path("D:/projects")))

    def test_warning_logged_for_non_ascii_external_root(self) -> None:
        external_root = "D:/项目" if os.name == "nt" else "/tmp/项目"
        self.settings.sandbox_external_root = external_root

        with self.assertLogs("app.services.sandbox", level="WARNING") as logs:
            sandbox = ExecutionSandbox(task_id="non-ascii-root", base_dir=str(self.base_dir))

        self.assertEqual(sandbox.work_dir, Path(external_root) / "non-ascii-root")
        self.assertIn("non-ASCII", "\n".join(logs.output))

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

        self.assertEqual(result["method"], "copytree_hardlink")
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
