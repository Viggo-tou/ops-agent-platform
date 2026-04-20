"""Request refinement gate — converts vague user input into precise code operation instructions.

Uses already-fetched Jira ticket content and semantic translation to produce
a refined, actionable request for the planner.  Follows the same CLI / API
dual-backend pattern used by the codegen service.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RefinedRequest:
    """Immutable result of request refinement."""
    refined_text: str
    confidence: float
    raw_response: str


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_REFINEMENT_SYSTEM_PROMPT = """\
You are an intent resolution engine.  Your job is to translate an external
task description (Jira ticket, user shorthand, etc.) into precise code
operation instructions that a planning agent can act on.

Rules:
1. Output ONLY the refined request as plain text.  No JSON, no markdown
   fences, no commentary.
2. The refined text must be a self-contained instruction that a code planner
   can execute without seeing the original Jira ticket.
3. Preserve every concrete detail from the external task context: file paths,
   function names, UI labels, acceptance criteria, edge cases.
4. If the external task context includes acceptance criteria or a checklist,
   convert each item into a concrete implementation instruction.
5. Use imperative voice ("Add …", "Update …", "Remove …").
6. Keep the output between 80 and 800 characters.
7. ALWAYS write the refined request in English, regardless of the input language.
"""


def _build_refinement_prompt(
    *,
    user_input: str,
    jira_context: dict[str, Any] | None,
    translation: dict[str, Any] | None,
    source_tree_summary: str | None,
) -> str:
    """Build the user-facing prompt sent to the refinement LLM."""
    sections: list[str] = []

    # --- User Request ---
    sections.append("=== User Request ===")
    sections.append(user_input.strip())

    # --- External Task Context (Jira) ---
    if jira_context:
        sections.append("")
        sections.append("=== External Task Context ===")
        key = jira_context.get("key") or jira_context.get("issue_key") or ""
        summary = jira_context.get("summary") or ""
        description = str(jira_context.get("description") or "")
        status = jira_context.get("status") or jira_context.get("issue_status") or ""
        issue_type = jira_context.get("issue_type") or ""
        priority = jira_context.get("priority") or ""

        if key:
            sections.append(f"Issue Key: {key}")
        if summary:
            sections.append(f"Summary: {summary}")
        if status:
            sections.append(f"Status: {status}")
        if issue_type:
            sections.append(f"Type: {issue_type}")
        if priority:
            sections.append(f"Priority: {priority}")
        if description:
            # Pass full description (up to 4000 chars), NOT truncated to 1200
            sections.append(f"Description:\n{description[:4000]}")

    # --- Semantic Translation ---
    if translation:
        sections.append("")
        sections.append("=== Semantic Translation ===")
        for field in ("normalized_request", "intent", "work_type", "objective"):
            val = translation.get(field)
            if val:
                sections.append(f"{field}: {val}")
        constraints = translation.get("constraints") or []
        if constraints:
            sections.append("constraints: " + "; ".join(str(c) for c in constraints))

    # --- Repository File Tree ---
    if source_tree_summary:
        sections.append("")
        sections.append("=== Repository File Tree ===")
        sections.append(source_tree_summary)

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI backend  (claude -p)
# ---------------------------------------------------------------------------

def refine_request_cli(
    *,
    user_input: str,
    jira_context: dict[str, Any] | None,
    translation: dict[str, Any] | None,
    source_tree_summary: str | None,
    claude_command: str = "npx",
    claude_args: str = "--yes @anthropic-ai/claude-code",
    timeout_seconds: float = 120.0,
) -> RefinedRequest:
    """Run the refinement prompt via ``claude -p`` CLI subprocess.

    Follows the same temp-file + subprocess pattern used by
    ``CodeGenerator._call_claude_code`` in ``codegen.py``.
    """
    claude_cmd = shutil.which(claude_command)
    if not claude_cmd:
        raise RuntimeError(f"Claude Code CLI not found: {claude_command}")

    prompt_text = _build_refinement_prompt(
        user_input=user_input,
        jira_context=jira_context,
        translation=translation,
        source_tree_summary=source_tree_summary,
    )
    full_prompt = f"{_REFINEMENT_SYSTEM_PROMPT}\n\n{prompt_text}"

    workdir = tempfile.mkdtemp(prefix="ops_refinement_")
    prompt_file_path: str | None = None
    try:
        # Write prompt to temp file (Windows pipe buffer workaround)
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        prompt_file.write(full_prompt)
        prompt_file.close()
        prompt_file_path = prompt_file.name

        # Build CLI args
        args = claude_args.split()
        if "-p" not in args and "--print" not in args:
            args.append("--print")

        cmd = [claude_cmd, *args, "-"]

        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)
        # Windows: Claude Code CLI needs git-bash path
        if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
            git_bash_candidates = [
                "D:\\Git\\bin\\bash.exe",
                "C:\\Program Files\\Git\\bin\\bash.exe",
                "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
            ]
            for candidate in git_bash_candidates:
                if os.path.isfile(candidate):
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                    break

        _logger.info("Refinement CLI cmd: %s (cwd=%s)", cmd, workdir)

        max_retries = 1
        last_error: Exception | None = None
        stdout: str = ""
        stderr: str = ""

        for attempt in range(1 + max_retries):
            if attempt > 0:
                _logger.info("Refinement CLI retry %d/%d", attempt, max_retries)
                import time as _time
                _time.sleep(2)

            with open(prompt_file_path, "r", encoding="utf-8") as stdin_f:
                proc = subprocess.Popen(
                    cmd,
                    stdin=stdin_f,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workdir,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
                    last_error = RuntimeError(
                        f"Refinement CLI timed out after {timeout_seconds}s (attempt {attempt + 1})"
                    )
                    _logger.warning("Refinement CLI timed out (attempt %d)", attempt + 1)
                    continue

            if proc.returncode != 0:
                stderr_text = (stderr or "").strip()[:500]
                last_error = RuntimeError(
                    f"Refinement CLI failed (rc={proc.returncode}): {stderr_text}"
                )
                _logger.warning("Refinement CLI failed rc=%d (attempt %d)", proc.returncode, attempt + 1)
                continue

            last_error = None
            break

        if last_error is not None:
            raise last_error

        raw = (stdout or "").strip()
        if len(raw) < 20:
            raise ValueError(
                f"Refinement response too short ({len(raw)} chars): {raw!r}"
            )

        return RefinedRequest(
            refined_text=raw,
            confidence=0.8,
            raw_response=raw,
        )
    finally:
        if prompt_file_path:
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass
        try:
            import shutil as _shutil
            _shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API backend  (Anthropic Messages API)
# ---------------------------------------------------------------------------

def refine_request_api(
    *,
    user_input: str,
    jira_context: dict[str, Any] | None,
    translation: dict[str, Any] | None,
    source_tree_summary: str | None,
    api_key: str,
    base_url: str = "https://api.anthropic.com",
    model: str = "claude-sonnet-4-20250514",
    timeout_seconds: float = 60.0,
) -> RefinedRequest:
    """Run the refinement prompt via Anthropic Messages API (httpx)."""
    prompt_text = _build_refinement_prompt(
        user_input=user_input,
        jira_context=jira_context,
        translation=translation,
        source_tree_summary=source_tree_summary,
    )

    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": _REFINEMENT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.2,
    }

    try:
        response = httpx.post(url, json=body, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()
        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Refinement API error: {exc}") from exc

    raw = content.strip()
    if len(raw) < 20:
        raise ValueError(
            f"Refinement response too short ({len(raw)} chars): {raw!r}"
        )

    return RefinedRequest(
        refined_text=raw,
        confidence=0.8,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Source tree summary builder
# ---------------------------------------------------------------------------

_NOISE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", "coverage", ".idea", ".vscode", ".DS_Store",
    "egg-info", ".eggs",
})


def _build_source_tree_summary(
    source_path: Path | str,
    max_depth: int = 3,
    max_entries: int = 200,
) -> str:
    """List source tree files, skipping noise directories.

    Returns a newline-separated list of relative paths suitable for
    inclusion in an LLM prompt.
    """
    root = Path(source_path)
    if not root.is_dir():
        return ""

    entries: list[str] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth or len(entries) >= max_entries:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for child in children:
            if child.name in _NOISE_DIRS:
                continue
            # Skip hidden files/dirs (except common config files)
            if child.name.startswith(".") and child.name not in {".env.example", ".gitignore"}:
                continue
            rel = child.relative_to(root)
            if child.is_dir():
                entries.append(f"{rel}/")
                if len(entries) < max_entries:
                    _walk(child, depth + 1)
            else:
                entries.append(str(rel))
            if len(entries) >= max_entries:
                return

    _walk(root, 1)
    if len(entries) >= max_entries:
        entries.append(f"... (truncated at {max_entries} entries)")
    return "\n".join(entries)
