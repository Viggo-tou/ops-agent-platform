from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.core.config import get_settings
from app.services.diff_repair import repair_diff


logger = logging.getLogger(__name__)


class SandboxError(Exception):
    pass


def _sanitize_diff(raw_diff: str) -> str:
    """Clean common LLM diff formatting issues before applying a patch."""
    lines = raw_diff.split("\n")
    cleaned = [line.rstrip("\r") for line in lines]
    result = "\n".join(cleaned)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _is_ascii_path(path: Path) -> bool:
    """Return True if every component of path is pure ASCII (Android plugin compat)."""
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


class ExecutionSandbox:
    """Manages an isolated working directory for a single task."""

    def __init__(
        self,
        task_id: str,
        *,
        base_dir: str = "data/sandboxes",
        sandbox_external_root: str | None = None,
    ):
        if not task_id.strip():
            raise SandboxError("task_id is required for sandbox execution.")

        self.task_id = task_id
        configured_external_root = sandbox_external_root
        if configured_external_root is None:
            configured_external_root = get_settings().sandbox_external_root

        if configured_external_root:
            external_root = Path(configured_external_root)
            if not external_root.is_absolute():
                raise ValueError(
                    "sandbox_external_root must be an absolute path when set "
                    f"(got {configured_external_root!r})."
                )
            if not _is_ascii_path(external_root):
                logger.warning(
                    "sandbox external root contains non-ASCII characters; Android Gradle plugin may reject builds",
                    extra={"sandbox_external_root": str(external_root)},
                )
            self.base_dir = external_root
        else:
            self.base_dir = Path(base_dir)
            effective_base_dir = self.base_dir if self.base_dir.is_absolute() else Path.cwd() / self.base_dir
            if not _is_ascii_path(effective_base_dir):
                logger.info(
                    "sandbox fallback path contains non-ASCII characters; set OPS_AGENT_SANDBOX_EXTERNAL_ROOT "
                    "to an ASCII absolute path to avoid Android Gradle plugin failures",
                    extra={"sandbox_base_dir": str(effective_base_dir)},
                )
        self.sandbox_dir = self.base_dir / task_id
        self._cloned = False
        self._validate_sandbox_dir()

    @property
    def work_dir(self) -> Path:
        return self.sandbox_dir

    # Directories that pollute the sandbox without ever needing to be
    # patched. Skipping them at copy time is the second half of the
    # speedup (the first half is hardlinking). Mirrors knowledge.py's
    # IGNORED_PARTS but keeps a local copy so sandbox isolation doesn't
    # depend on the retrieval module.
    _SKIP_AT_COPY = frozenset({
        ".git", ".gradle", ".idea", "__pycache__", "node_modules",
        "build", "dist", ".next", "out", "target", "bin", "obj",
        ".cache", ".turbo", ".parcel-cache", ".vite", ".svelte-kit",
        "coverage", ".nyc_output", ".venv", "venv", ".tox",
    })

    @classmethod
    def _copytree_with_hardlinks(cls, src: Path, dst: Path) -> None:
        """copytree variant that uses os.link for files (where supported)
        and skips known-noise directories. Two-axis speedup vs the
        previous shutil.copytree + byte copy:

        1. Hardlinks: ~10× faster per file copied. Safety contract:
           sandbox writes go through ``git apply`` which uses atomic
           rename → new inode → source repo untouched. Direct in-place
           ``open(...,'w')`` would propagate to source — none of our
           code does this today.
        2. Skip node_modules / build / .git / cache dirs at copy time.
           These are never patched and walking them dominates the copy
           cost on JS/TS repos (Handyman ships ~78k entries dominated
           by node_modules; skipping cuts to ~600 entries).

        Falls back to shutil.copy2 on per-file hardlink errors
        (cross-volume, permission, ReFS dedup conflict). Degrades
        gracefully if hardlinking fails entirely.
        """
        def _copy_one(s: str, d: str) -> str:
            try:
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)
            return d

        def _ignore(_dir: str, names: list[str]) -> list[str]:
            return [n for n in names if n in cls._SKIP_AT_COPY]

        shutil.copytree(
            str(src),
            str(dst),
            copy_function=_copy_one,
            ignore=_ignore,
            dirs_exist_ok=True,
        )

    def clone(
        self,
        repo_url_or_path: str,
        *,
        branch: str | None = None,
        timeout_seconds: float = 120,
    ) -> dict[str, object]:
        """Clone or copy source code into the sandbox directory. Returns source metadata."""
        source_path = Path(repo_url_or_path)

        if source_path.is_dir() and not (source_path / ".git").is_dir():
            # Use hardlinks instead of byte-by-byte copy where supported.
            # Speedup is ~10× on local filesystems (NTFS ReFS / ext4 / APFS).
            # Safety: subsequent sandbox writes go through `git apply` which
            # writes via temp-file + rename → creates a new inode and breaks
            # the hardlink, leaving the source file untouched. `git init/
            # add/commit` only read files into the index. As long as nothing
            # in this codebase mutates sandbox files via in-place open()+
            # write(), the source repo is safe.
            self._copytree_with_hardlinks(source_path, self.sandbox_dir)
            for command, step_timeout in (
                ("git init", 30),
                ("git add .", 60),
                (
                    'git -c user.email=sandbox@example.local -c user.name="Sandbox" '
                    'commit -m "initial sandbox baseline"',
                    30,
                ),
            ):
                step_result = self.run(command, timeout_seconds=step_timeout)
                if step_result["exit_code"] != 0:
                    raise SandboxError(f"{command} failed: {str(step_result['stderr'])[:500]}")
            self._cloned = True
            return {
                "method": "copytree_hardlink",
                "source": str(source_path),
                "branch": branch,
                "sandbox_dir": str(self.sandbox_dir),
            }

        if self.sandbox_dir.exists():
            raise SandboxError(f"Sandbox directory already exists: {self.sandbox_dir}")

        self.sandbox_dir.mkdir(parents=True, exist_ok=False)
        cmd = ["git", "clone"]
        if source_path.is_dir():
            cmd.append("--local")
        else:
            cmd.extend(["--depth", "1"])
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([repo_url_or_path, str(self.sandbox_dir)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"git clone timed out after {timeout_seconds}s") from exc

        if result.returncode != 0:
            if source_path.is_dir() and (source_path / ".git").is_dir() and branch is None:
                shutil.rmtree(self.sandbox_dir, ignore_errors=True)
                shutil.copytree(str(source_path), str(self.sandbox_dir), dirs_exist_ok=True)
                self._cloned = True
                return {
                    "method": "git_clone_copytree_fallback",
                    "repo_url": repo_url_or_path,
                    "branch": branch,
                    "sandbox_dir": str(self.sandbox_dir),
                    "clone_error": result.stderr[:500],
                }
            raise SandboxError(f"git clone failed: {result.stderr[:500]}")

        self._cloned = True
        return {
            "method": "git_clone",
            "repo_url": repo_url_or_path,
            "branch": branch,
            "sandbox_dir": str(self.sandbox_dir),
        }

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float = 60,
        max_output_bytes: int = 64 * 1024,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Run a shell command inside the sandbox. Returns structured result."""
        work_dir = self._resolve_work_dir(cwd)
        max_output_chars = max(0, int(max_output_bytes))
        # JVM tools (gradle / javac / kotlinc) localize error messages
        # via Locale.getDefault(), which on a zh-CN Windows machine emits
        # GBK Chinese. The compile_gate repair-codegen pipeline can't
        # parse Chinese error text reliably, so force Locale.US for ALL
        # JVM child processes via JAVA_TOOL_OPTIONS. This env var is
        # honored by every JVM started under this process tree, including
        # gradle daemon, kotlinc, javac, and any test runners.
        jvm_locale_opts = "-Duser.language=en -Duser.country=US -Dfile.encoding=UTF-8"
        existing_jto = os.environ.get("JAVA_TOOL_OPTIONS", "")
        if existing_jto:
            jvm_locale_opts = f"{existing_jto} {jvm_locale_opts}"
        run_env = {**os.environ, "JAVA_TOOL_OPTIONS": jvm_locale_opts}
        if env:
            # Caller-supplied env wins, but we still want to PREPEND our
            # JVM locale flags to caller's JAVA_TOOL_OPTIONS rather than
            # let the caller silently drop them.
            caller_jto = env.get("JAVA_TOOL_OPTIONS", "")
            merged_jto = f"{jvm_locale_opts} {caller_jto}".strip() if caller_jto else jvm_locale_opts
            run_env = {**run_env, **env, "JAVA_TOOL_OPTIONS": merged_jto}

        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                # Explicit UTF-8 with replacement: on Windows, default text mode
                # uses GBK which can't decode Gradle / git / other UTF-8 stderr;
                # the reader thread then raises UnicodeDecodeError and leaves
                # result.stderr as None, blowing up downstream subscript.
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                cwd=str(work_dir),
                env=run_env,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            stdout_text = result.stdout or ""
            stderr_text = result.stderr or ""
            return {
                "exit_code": result.returncode,
                "stdout": stdout_text[:max_output_chars],
                "stderr": stderr_text[:max_output_chars],
                "duration_ms": duration_ms,
                "timed_out": False,
                "command": command,
                "cwd": str(work_dir),
            }
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout_seconds}s",
                "duration_ms": duration_ms,
                "timed_out": True,
                "command": command,
                "cwd": str(work_dir),
            }

    def apply_patch(
        self,
        patch: str,
        *,
        context_files: dict[str, str] | None = None,
        commit: bool = True,
        commit_message: str = "Applied patch via sandbox",
        timeout_seconds: float = 30,
    ) -> dict[str, object]:
        """Apply a unified diff to the sandbox repo. Returns before/after SHAs."""
        if not self.exists():
            raise SandboxError(f"Sandbox does not exist: {self.sandbox_dir}")

        before_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(self.sandbox_dir),
            timeout=10,
        )
        before_sha = before_result.stdout.strip() if before_result.returncode == 0 else ""

        repair_result = repair_diff(patch, context_files=context_files)
        sanitized_patch = _sanitize_diff(repair_result.repaired_diff)
        strategies: list[tuple[str, list[str]]] = [
            ("git_apply", []),
            ("git_apply_3way", ["--3way"]),
            ("git_apply_relaxed", ["--ignore-whitespace", "--whitespace=nowarn"]),
        ]

        apply_result: dict[str, object] | None = None
        for method, extra_args in strategies:
            result = self._try_git_apply(
                sanitized_patch,
                extra_args=extra_args,
                timeout_seconds=timeout_seconds,
            )
            result["method"] = method
            logger.info(
                "sandbox patch strategy attempted",
                extra={"task_id": self.task_id, "method": method, "success": result["success"]},
            )
            if result["success"]:
                apply_result = result
                break
            apply_result = result

        if apply_result is None or not apply_result["success"]:
            result = self._try_python_patch(sanitized_patch)
            result["method"] = "python_patch"
            logger.info(
                "sandbox patch strategy attempted",
                extra={"task_id": self.task_id, "method": "python_patch", "success": result["success"]},
            )
            if result["success"]:
                apply_result = result

        if apply_result is None or not apply_result["success"]:
            result = self._try_patch_command(sanitized_patch, timeout_seconds=timeout_seconds)
            result["method"] = "patch_p1"
            logger.info(
                "sandbox patch strategy attempted",
                extra={"task_id": self.task_id, "method": "patch_p1", "success": result["success"]},
            )
            apply_result = result

        if not apply_result["success"]:
            raise SandboxError(f"All patch strategies failed. Last error: {str(apply_result['error'])[:500]}")

        after_sha = before_sha
        if commit:
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True,
                text=True,
                cwd=str(self.sandbox_dir),
                timeout=10,
            )
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_message, "--allow-empty"],
                capture_output=True,
                text=True,
                cwd=str(self.sandbox_dir),
                timeout=10,
            )
            if commit_result.returncode == 0:
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=str(self.sandbox_dir),
                    timeout=10,
                )
                after_sha = sha_result.stdout.strip()

        return {
            "before_sha": before_sha,
            "after_sha": after_sha,
            "committed": commit,
            "method": apply_result["method"],
            "patch_stats": str(apply_result.get("stdout", ""))[:500],
            "sandbox_dir": str(self.sandbox_dir),
            "diff_repairs_applied": repair_result.repairs_applied,
            "diff_file_count": repair_result.file_count,
        }

    def _try_git_apply(
        self,
        diff: str,
        *,
        extra_args: list[str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Try git apply with optional args and return a structured status."""
        patch_file = self.sandbox_dir / ".claude_patch.diff"
        patch_file.write_text(diff, encoding="utf-8")
        try:
            result = subprocess.run(
                ["git", "apply", "--stat", "--apply", *extra_args, patch_file.name],
                capture_output=True,
                text=True,
                cwd=str(self.sandbox_dir),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "method": "git_apply",
                "error": f"git apply timed out after {timeout_seconds}s",
                "stdout": "",
            }
        finally:
            patch_file.unlink(missing_ok=True)

        if result.returncode == 0:
            return {
                "success": True,
                "method": "git_apply",
                "error": "",
                "stdout": result.stdout,
            }
        return {
            "success": False,
            "method": "git_apply",
            "error": result.stderr,
            "stdout": result.stdout,
        }

    def _try_python_patch(self, diff: str) -> dict[str, object]:
        """Pure-Python unified diff applier. Works on all platforms without external tools."""
        try:
            files_patched: list[str] = []
            current_path: str | None = None
            hunks: list[tuple[int, list[str]]] = []
            hunk_start = 0
            hunk_lines: list[str] = []

            def _flush() -> None:
                nonlocal current_path, hunks, hunk_lines, hunk_start
                if hunk_lines and current_path is not None:
                    hunks.append((hunk_start, list(hunk_lines)))
                if current_path is not None and hunks:
                    self._apply_hunks_to_file(current_path, hunks, files_patched)
                current_path = None
                hunks = []
                hunk_lines = []
                hunk_start = 0

            for line in diff.split("\n"):
                if line.startswith("+++ "):
                    # Flush previous file
                    if hunk_lines and current_path is not None:
                        hunks.append((hunk_start, list(hunk_lines)))
                    if current_path is not None and hunks:
                        self._apply_hunks_to_file(current_path, hunks, files_patched)
                    hunks = []
                    hunk_lines = []
                    raw = line[4:].strip()
                    if raw.startswith("b/"):
                        raw = raw[2:]
                    if raw == "/dev/null":
                        current_path = None
                    else:
                        current_path = raw
                elif line.startswith("@@ "):
                    if hunk_lines and current_path is not None:
                        hunks.append((hunk_start, list(hunk_lines)))
                        hunk_lines = []
                    import re
                    m = re.search(r"-(\d+)", line)
                    hunk_start = int(m.group(1)) if m else 1
                elif current_path is not None and (
                    line.startswith("+") or line.startswith("-") or line.startswith(" ")
                ):
                    hunk_lines.append(line)

            _flush()

            if not files_patched:
                return {"success": False, "error": "No files matched for patching", "stdout": ""}

            return {
                "success": True,
                "error": "",
                "stdout": f"Patched {len(files_patched)} file(s): {', '.join(files_patched)}",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "stdout": ""}

    def _apply_hunks_to_file(
        self,
        rel_path: str,
        hunks: list[tuple[int, list[str]]],
        files_patched: list[str],
    ) -> None:
        """Apply parsed hunks to a single file in the sandbox."""
        target = self.sandbox_dir / rel_path
        if target.exists():
            original_lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            original_lines = []

        result_lines = list(original_lines)
        offset = 0

        for hunk_start_1based, hunk_lines in hunks:
            idx = hunk_start_1based - 1 + offset
            new_segment: list[str] = []
            remove_count = 0
            for hl in hunk_lines:
                if hl.startswith("-"):
                    remove_count += 1
                elif hl.startswith("+"):
                    new_segment.append(hl[1:])
                else:
                    # context line: keep and advance
                    new_segment.append(hl[1:] if len(hl) > 1 else "")
                    remove_count += 1

            # Find the best matching position (fuzzy)
            best_idx = self._find_hunk_position(result_lines, hunk_lines, idx)
            if best_idx is not None:
                idx = best_idx

            # Replace the old lines with new
            context_and_remove = [hl for hl in hunk_lines if not hl.startswith("+")]
            old_count = len(context_and_remove)
            result_lines[idx : idx + old_count] = new_segment
            offset += len(new_segment) - old_count

        target.write_text("\n".join(result_lines), encoding="utf-8")
        files_patched.append(rel_path)

    @staticmethod
    def _find_hunk_position(
        file_lines: list[str],
        hunk_lines: list[str],
        suggested_idx: int,
    ) -> int | None:
        """Find where a hunk's context/remove lines best match in the file. Returns index or None."""
        old_lines = []
        for hl in hunk_lines:
            if hl.startswith("+"):
                continue
            old_lines.append(hl[1:] if len(hl) > 1 else "")

        if not old_lines:
            return suggested_idx

        def _matches_at(pos: int) -> bool:
            if pos < 0 or pos + len(old_lines) > len(file_lines):
                return False
            for a, b in zip(old_lines, file_lines[pos:]):
                if a.rstrip() != b.rstrip():
                    return False
            return True

        # Try suggested position first
        if _matches_at(suggested_idx):
            return suggested_idx

        # Search nearby (within 50 lines)
        for delta in range(1, 50):
            if _matches_at(suggested_idx - delta):
                return suggested_idx - delta
            if _matches_at(suggested_idx + delta):
                return suggested_idx + delta

        # Fall back to suggested even if no match (best effort)
        return suggested_idx

    def _try_patch_command(self, diff: str, *, timeout_seconds: float) -> dict[str, object]:
        """Try POSIX patch as a final fallback for systems that provide it."""
        patch_file = self.sandbox_dir / ".claude_patch.diff"
        patch_file.write_text(diff, encoding="utf-8")
        try:
            result = self.run(
                "patch -p1 < .claude_patch.diff",
                timeout_seconds=timeout_seconds,
            )
        finally:
            patch_file.unlink(missing_ok=True)

        if result["exit_code"] == 0:
            return {
                "success": True,
                "method": "patch_p1",
                "error": "",
                "stdout": result.get("stdout", ""),
            }
        return {
            "success": False,
            "method": "patch_p1",
            "error": result.get("stderr", ""),
            "stdout": result.get("stdout", ""),
        }

    def teardown(self) -> None:
        """Remove the sandbox directory."""
        if self.sandbox_dir.exists():
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    def exists(self) -> bool:
        return self.sandbox_dir.exists()

    def snapshot_id(self) -> str | None:
        """Return the current git commit for resume idempotency checks."""
        if not self.exists():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(self.sandbox_dir),
            timeout=10,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return f"git:{sha}" if sha else None

    def is_clean(self) -> bool:
        """Return True when the sandbox has no uncommitted changes."""
        if not self.exists():
            return True
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(self.sandbox_dir),
            timeout=10,
        )
        if result.returncode != 0:
            return False
        return not result.stdout.strip()

    def rollback_to_snapshot(self, snapshot_id: str) -> bool:
        """Best-effort rollback for a half-applied resume checkpoint."""
        if not self.exists():
            return False
        prefix = "git:"
        if not snapshot_id.startswith(prefix):
            return False
        sha = snapshot_id[len(prefix) :].strip()
        if not sha:
            return False
        reset = subprocess.run(
            ["git", "reset", "--hard", sha],
            capture_output=True,
            text=True,
            cwd=str(self.sandbox_dir),
            timeout=30,
        )
        if reset.returncode != 0:
            return False
        clean = subprocess.run(
            ["git", "clean", "-fd"],
            capture_output=True,
            text=True,
            cwd=str(self.sandbox_dir),
            timeout=30,
        )
        return clean.returncode == 0

    def _validate_sandbox_dir(self) -> None:
        base_dir = self.base_dir.resolve()
        sandbox_dir = self.sandbox_dir.resolve()
        if sandbox_dir == base_dir:
            raise SandboxError(f"Sandbox directory must be a child of base directory: {self.sandbox_dir}")

        try:
            sandbox_dir.relative_to(base_dir)
        except ValueError as exc:
            raise SandboxError(f"Sandbox directory is outside base directory: {self.sandbox_dir}") from exc

    def _resolve_work_dir(self, cwd: str | None) -> Path:
        if cwd is None:
            work_dir = self.sandbox_dir
        else:
            work_dir = Path(cwd)
        if cwd is not None and not work_dir.is_absolute():
            work_dir = self.sandbox_dir / work_dir

        try:
            work_dir.resolve().relative_to(self.sandbox_dir.resolve())
        except ValueError as exc:
            raise SandboxError(f"Working directory {work_dir} is outside sandbox {self.sandbox_dir}") from exc

        return work_dir
