from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.sandbox import ExecutionSandbox, SandboxError  # noqa: E402


def _writable_mkdtemp() -> Path:
    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    # Python's tempfile uses 0o700, which is not writable in this Windows sandbox.
    tempfile._os.mkdir = mkdir_with_write_access
    try:
        candidate_roots = []
        if os.environ.get("OPS_AGENT_TEST_SANDBOX_ROOT"):
            candidate_roots.append(Path(os.environ["OPS_AGENT_TEST_SANDBOX_ROOT"]))
        candidate_roots.extend([Path(tempfile.gettempdir()), Path.home() / ".ops-agent-sandbox-patch-tests", BACKEND_ROOT])

        for root in candidate_roots:
            try:
                str(root).encode("ascii")
            except UnicodeEncodeError:
                continue
            try:
                root.mkdir(parents=True, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="sandbox-patch-test-", dir=str(root)))
            except OSError:
                continue

        return Path(tempfile.mkdtemp(prefix="sandbox-patch-test-", dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


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


class ExecutionSandboxApplyPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_dir = _writable_mkdtemp()
        self.settings = get_settings()
        self._original_sandbox_external_root = self.settings.sandbox_external_root
        self.settings.sandbox_external_root = None

    def tearDown(self) -> None:
        self.settings.sandbox_external_root = self._original_sandbox_external_root
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _sandbox_with_repo(self, task_id: str = "task-1") -> ExecutionSandbox:
        sandbox = ExecutionSandbox(task_id=task_id, base_dir=str(self.base_dir))
        sandbox.work_dir.mkdir(parents=True, exist_ok=False)
        _git(sandbox.work_dir, "init")
        _git(sandbox.work_dir, "config", "user.email", "sandbox-tests@example.com")
        _git(sandbox.work_dir, "config", "user.name", "Sandbox Tests")
        (sandbox.work_dir / "message.txt").write_text("hello\n", encoding="utf-8")
        _git(sandbox.work_dir, "add", "message.txt")
        _git(sandbox.work_dir, "commit", "-m", "Initial commit")
        return sandbox

    @staticmethod
    def _patch_with_line(line: str) -> str:
        return f"""diff --git a/message.txt b/message.txt
--- a/message.txt
+++ b/message.txt
@@ -1 +1,2 @@
 hello
+{line}
"""

    def test_apply_patch_success(self) -> None:
        sandbox = self._sandbox_with_repo()

        result = sandbox.apply_patch(
            self._patch_with_line("patched"),
            commit=True,
            commit_message="Apply sandbox patch",
        )

        self.assertNotEqual(result["before_sha"], result["after_sha"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["method"], "git_apply")
        self.assertIn("patched", (sandbox.work_dir / "message.txt").read_text(encoding="utf-8"))

    def test_apply_patch_with_crlf(self) -> None:
        sandbox = self._sandbox_with_repo()
        patch_text = self._patch_with_line("patched with crlf").replace("\n", "\r\n")

        result = sandbox.apply_patch(patch_text, commit=False)

        self.assertEqual(result["method"], "git_apply")
        self.assertIn("patched with crlf", (sandbox.work_dir / "message.txt").read_text(encoding="utf-8"))

    def test_apply_patch_missing_trailing_newline(self) -> None:
        sandbox = self._sandbox_with_repo()
        patch_text = self._patch_with_line("patched without newline").rstrip("\n")

        result = sandbox.apply_patch(patch_text, commit=False)

        self.assertEqual(result["method"], "git_apply")
        self.assertIn("patched without newline", (sandbox.work_dir / "message.txt").read_text(encoding="utf-8"))

    def test_apply_patch_writes_lf_patch_file_for_multifile_diff(self) -> None:
        sandbox = self._sandbox_with_repo()
        (sandbox.work_dir / "one.txt").write_text("old\n", encoding="utf-8")
        (sandbox.work_dir / "two.txt").write_text("alpha\nkeep\nbeta\n", encoding="utf-8")
        _git(sandbox.work_dir, "add", "one.txt", "two.txt")
        _git(sandbox.work_dir, "commit", "-m", "Add multi-file fixtures")
        patch_text = """diff --git a/one.txt b/one.txt
--- a/one.txt
+++ b/one.txt
@@ -1,1 +1,1 @@
-old
+new
diff --git a/two.txt b/two.txt

--- a/two.txt
+++ b/two.txt
@@ -1,3 +1,4 @@
 alpha
 keep
 beta
+after
"""

        result = sandbox.apply_patch(patch_text, commit=False)

        self.assertEqual(result["method"], "git_apply")
        self.assertEqual((sandbox.work_dir / "one.txt").read_text(encoding="utf-8"), "new\n")
        self.assertEqual(
            (sandbox.work_dir / "two.txt").read_text(encoding="utf-8"),
            "alpha\nkeep\nbeta\nafter\n",
        )

    def test_apply_patch_fallback_to_relaxed(self) -> None:
        sandbox = self._sandbox_with_repo()
        results = [
            {"success": False, "method": "git_apply", "error": "strict failed", "stdout": ""},
            {"success": False, "method": "git_apply", "error": "recount failed", "stdout": ""},
            {"success": False, "method": "git_apply", "error": "3way failed", "stdout": ""},
            {"success": True, "method": "git_apply", "error": "", "stdout": ""},
        ]

        with patch.object(sandbox, "_try_git_apply", side_effect=results) as git_apply:
            result = sandbox.apply_patch(self._patch_with_line("relaxed"), commit=False)

        self.assertEqual(result["method"], "git_apply_relaxed")
        self.assertEqual(git_apply.call_count, 4)
        self.assertEqual(
            git_apply.call_args_list[3].kwargs["extra_args"],
            ["--ignore-whitespace", "--whitespace=nowarn", "--recount"],
        )

    def test_apply_patch_bad_diff(self) -> None:
        sandbox = self._sandbox_with_repo()

        with self.assertRaises(SandboxError):
            sandbox.apply_patch("this is not a valid unified diff\n")

    def test_apply_patch_no_sandbox(self) -> None:
        sandbox = ExecutionSandbox(task_id="missing-task", base_dir=str(self.base_dir))

        with self.assertRaises(SandboxError):
            sandbox.apply_patch(self._patch_with_line("patched"))

    def test_apply_patch_no_commit(self) -> None:
        sandbox = self._sandbox_with_repo()

        result = sandbox.apply_patch(self._patch_with_line("uncommitted"), commit=False)

        self.assertEqual(result["before_sha"], result["after_sha"])
        self.assertFalse(result["committed"])
        self.assertIn("uncommitted", (sandbox.work_dir / "message.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
