from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.agents.schemas import CodegenResult
from app.core.config import Settings, get_settings
from app.services.reviewer import DiffReviewer


CODEGEN_SYSTEM_PROMPT = """You are a code generation agent. Given a task plan and source file contents, produce a unified diff.

CRITICAL RULES:
1. Output ONLY a valid unified diff. Nothing else. No explanations, no markdown fences, no commentary.
2. The very first line of your output MUST be "diff --git a/path b/path".
3. Use standard unified diff format with --- a/path, +++ b/path, and @@ hunk headers.
4. Only modify files mentioned in the plan.
5. Make minimal, focused changes.
6. For new files, use --- /dev/null.
7. Include 3 context lines around each change.

EXAMPLE OUTPUT FORMAT (your response must look exactly like this):
diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -10,7 +10,7 @@
 import os

 def greet(name):
-    return "Hello " + name
+    return f"Hello, {name}!"

 def main():
     print(greet("World"))

DO NOT output anything before "diff --git". DO NOT wrap in markdown code fences. DO NOT add explanations."""


CODEGEN_SYSTEM_PROMPT_JSON_MODE = """You are a code generation agent. Given a task plan and source file contents, produce the MODIFIED or NEW versions of the files.

CRITICAL RULES:
1. Output ONLY valid JSON. Nothing else. No markdown fences, no explanations.
2. Use this exact JSON structure:
{
  "files": [
    {
      "path": "relative/path/to/file.ext",
      "content": "full modified file content here",
      "summary": "one-line description of what changed"
    }
  ]
}
3. The "content" field must contain the COMPLETE file content after your modifications.
4. Only include files that you actually modified or newly created. Do not include unchanged files.
5. Make minimal, focused changes. Do not refactor unrelated code.
6. Preserve existing code style (indentation, naming conventions).
7. You CAN create entirely new files when the task clearly requires it. BUT: if the task text contains any of "do not create new files", "touch only", "only modify these files", "only these N files", you MUST NOT introduce any new file that is not already present in the provided FILE CONTEXT. Violating an explicit scope constraint is worse than producing an empty patch.
8. If the task declares specific target files (e.g. "delete X from src/data/mockUsers.js"), your output MUST include those files in the `files` array with the intended modification applied. Do not route the change through newly created wrapper/helper modules.
9. If you cannot satisfy the task without violating constraints (e.g. the target files are missing from FILE CONTEXT), output {"files": [], "error": "targets_not_in_context"} instead of fabricating unrelated changes.

EXAMPLE 1 (modify existing file):
Given a file app/greet.py with content:
def greet(name):
    return "Hello " + name

If the task is to use f-strings, output:
{"files":[{"path":"app/greet.py","content":"def greet(name):\\n    return f\\"Hello, {name}!\\"\\n","summary":"Use f-string for greeting"}]}

EXAMPLE 2 (create new file):
If the task requires creating a new config.json file:
{"files":[{"path":"config.json","content":"{\\n  \\"key\\": \\"value\\"\\n}\\n","summary":"Create new config file"}]}"""


class CodegenError(Exception):
    pass


class CodeGenerator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def generate_patch(
        self,
        *,
        task_id: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str = "",
    ) -> CodegenResult:
        """Generate a unified diff from a plan and file context."""
        del task_id
        providers = self._resolve_provider_chain()

        import logging as _log
        _logger = _log.getLogger("codegen.provider_chain")
        _logger.info("Provider chain: %s", providers)

        for provider_idx, provider in enumerate(providers):
            _logger.info("Trying provider %d/%d: %s", provider_idx + 1, len(providers), provider)
            try:
                result = self._try_provider(
                    provider=provider,
                    plan_json=plan_json,
                    context_files=context_files,
                    task_description=task_description,
                )
                _logger.info("Provider %s succeeded: %d files changed", provider, len(result.files_changed))
                return result
            except CodegenError as exc:
                _logger.warning("Provider %s failed: %s", provider, str(exc)[:300])
                if _is_provider_level_error(exc) and provider_idx < len(providers) - 1:
                    _logger.info("Classified as provider-level error — trying next provider")
                    continue
                raise

        raise CodegenError("No codegen provider available.")

    def _try_provider(
        self,
        *,
        provider: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
    ) -> CodegenResult:
        """Attempt codegen with a single provider, with up to 3 retries for parse errors."""
        if provider == "ollama":
            context_files = self._trim_context_for_ollama(context_files)

        prompt = self._build_prompt(
            plan_json,
            context_files,
            task_description,
            json_mode=provider in {"minimax", "ollama"},
        )

        if provider == "mock":
            return self._mock_generate(plan_json, context_files)

        max_attempts = 3
        last_error: str | None = None
        for attempt in range(max_attempts):
            call_prompt = prompt
            if attempt > 0:
                if provider in {"minimax", "ollama"}:
                    call_prompt += (
                        f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\n"
                        "You MUST output ONLY valid JSON using the required files array. "
                        "Each file entry must include path and complete modified content."
                    )
                else:
                    call_prompt += (
                        f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}\n"
                        "You MUST output ONLY a valid unified diff. No text before or after. "
                        "Start with 'diff --git'."
                    )

            try:
                if provider == "claude_code":
                    return self._call_claude_code(call_prompt, context_files=context_files)
                if provider == "codex":
                    return self._call_codex(call_prompt, context_files=context_files)
                if provider == "anthropic":
                    return self._call_anthropic(call_prompt)
                if provider == "deepseek":
                    return self._call_deepseek(call_prompt)
                if provider == "ollama":
                    return self._call_ollama(call_prompt, context_files=context_files)
                if provider == "minimax":
                    return self._call_minimax(call_prompt, context_files=context_files)
                if provider == "openai":
                    return self._call_openai(call_prompt)

                raise CodegenError(f"Unknown provider: {provider}")
            except CodegenError as exc:
                if _is_retryable_codegen_error(exc):
                    last_error = str(exc)
                    continue
                raise

        raise CodegenError(f"Failed to generate valid diff after {max_attempts} attempts. Last error: {last_error}")

    def _resolve_provider_chain(self) -> list[str]:
        """Return an ordered list of providers to try. Auto mode returns all configured providers."""
        # Explicit codegen_provider override takes priority
        codegen_override = getattr(self.settings, "codegen_provider", None)
        if codegen_override and codegen_override != "auto":
            return [codegen_override]

        provider = self.settings.primary_agent_provider
        if provider not in ("auto", "claude_code"):
            return [provider]

        chain: list[str] = []
        # Prefer codex first — its --full-auto sandbox mode reliably edits
        # files.  claude_code sandbox still needs debugging (the CLI doesn't
        # modify files when stdin/stdout are piped via --dangerously-skip-permissions).
        if shutil.which(self.settings.codex_command):
            chain.append("codex")
        if shutil.which(self.settings.claude_code_command):
            chain.append("claude_code")
        if getattr(self.settings, "anthropic_api_key", None):
            chain.append("anthropic")
        if getattr(self.settings, "deepseek_api_key", None):
            chain.append("deepseek")
        if getattr(self.settings, "openai_api_key", None):
            chain.append("openai")
        if getattr(self.settings, "minimax_api_key", None):
            chain.append("minimax")
        if self._ollama_available():
            chain.append("ollama")
        return chain if chain else ["mock"]

    def _ollama_available(self) -> bool:
        """Check if Ollama is running and reachable."""
        try:
            resp = httpx.get(f"{self.settings.ollama_base_url.replace('/v1', '')}/api/tags", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def _resolve_model_name(self, provider_name: str) -> str:
        configured_model = getattr(self.settings, "primary_agent_model", "").strip()
        if provider_name == "deepseek":
            return self.settings.deepseek_model
        if provider_name == "ollama":
            return self.settings.ollama_model
        if provider_name == "minimax" and (not configured_model or configured_model.lower().startswith("gpt")):
            return getattr(self.settings, "semantic_translator_model", "MiniMax-Text-01")
        if configured_model:
            return configured_model
        return "gpt-4o" if provider_name == "openai" else "MiniMax-Text-01"

    def _call_anthropic(self, prompt: str) -> CodegenResult:
        """Call Anthropic Messages API for code generation."""
        if not self.settings.anthropic_api_key:
            raise CodegenError("OPS_AGENT_ANTHROPIC_API_KEY is not configured.")

        model_name = self.settings.anthropic_model
        url = f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model_name,
            "max_tokens": 8192,
            "system": CODEGEN_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=120)
            response.raise_for_status()
            data = response.json()
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            usage = data.get("usage", {})
            return self._parse_response(
                content,
                provider_name="anthropic",
                model_name=model_name,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"Anthropic API error: {exc}") from exc

    def _call_claude_code(self, prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
        """Call Claude Code CLI for code generation via sandbox file editing."""
        claude_cmd = shutil.which(self.settings.claude_code_command)
        if not claude_cmd:
            raise CodegenError(f"Claude Code CLI not found: {self.settings.claude_code_command}")

        workdir = tempfile.mkdtemp(prefix="ops_claude_code_")
        try:
            # Write context files into the working directory
            for rel_path, content in context_files.items():
                full = Path(workdir) / rel_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")

            # Initialize git repo so claude trusts the directory
            subprocess.run(
                ["git", "init"], cwd=workdir,
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "add", "."], cwd=workdir,
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "-c", "user.name=ops", "-c", "user.email=ops@local",
                 "commit", "-m", "init", "--allow-empty"],
                cwd=workdir, capture_output=True, timeout=10,
            )

            # Build instruction for Claude Code
            claude_instruction = (
                "You are modifying files in this directory to implement the following task.\n"
                "Only modify or create the files described. Do not delete unrelated files.\n\n"
                + prompt
            )

            env = {**os.environ}
            # Do NOT pass ANTHROPIC_API_KEY - Claude Code CLI should use its own
            # OAuth session, not the possibly-exhausted API key.
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

            # Write prompt to temp file for stdin (Windows pipe buffer workaround)
            prompt_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8",
            )
            prompt_file.write(claude_instruction)
            prompt_file.close()

            # Run claude WITHOUT --print so it edits files directly in the sandbox
            claude_args = self.settings.claude_code_args.split()
            # Remove --print/-p if present (we want file editing, not text output)
            claude_args = [a for a in claude_args if a not in ("--print", "-p")]
            # Add --dangerously-skip-permissions so tool use works non-interactively
            # (equivalent of codex --full-auto). Safe because we run in a temp sandbox.
            if "--dangerously-skip-permissions" not in claude_args:
                claude_args.append("--dangerously-skip-permissions")
            cmd = [claude_cmd, *claude_args, "-"]
            timeout_sec = int(self.settings.claude_code_timeout_seconds)

            try:
                with open(prompt_file.name, "r", encoding="utf-8") as stdin_f:
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
                        stdout, stderr = proc.communicate(timeout=timeout_sec)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                capture_output=True, timeout=10,
                            )
                        else:
                            proc.kill()
                        proc.wait(timeout=5)
                        raise CodegenError(
                            f"Claude Code CLI timed out after {timeout_sec}s"
                        )
            finally:
                try:
                    os.unlink(prompt_file.name)
                except OSError:
                    pass

            if proc.returncode != 0:
                stderr_text = (stderr or "").strip()[:500]
                raise CodegenError(f"Claude Code CLI codegen failed (rc={proc.returncode}): {stderr_text}")

            # Diff original vs modified files (same approach as _call_codex)
            modified_files: list[dict[str, str]] = []
            work_path = Path(workdir)
            for file_path in work_path.rglob("*"):
                if file_path.is_dir():
                    continue
                rel = str(file_path.relative_to(work_path)).replace("\\", "/")
                # Skip git internals
                if rel.startswith(".git/") or rel.startswith(".git\\"):
                    continue
                try:
                    new_content = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                original_content = context_files.get(rel, "")
                if new_content != original_content:
                    modified_files.append({
                        "path": rel,
                        "content": new_content,
                        "summary": "Modified by Claude Code CLI",
                    })

            if not modified_files:
                raise CodegenError("Claude Code CLI did not modify any files.")

            diff_text, files_changed = self._generate_diff_from_files(context_files, modified_files)
            file_summaries = [{"path": f["path"], "summary": f["summary"]} for f in modified_files]
            summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"

            return CodegenResult(
                diff=diff_text,
                summary=summary,
                files_changed=files_changed,
                file_summaries=file_summaries,
                provider_name="claude_code",
                model_name="claude-code-cli",
            )
        except CodegenError:
            raise
        except subprocess.TimeoutExpired:
            raise CodegenError(f"Claude Code CLI timed out after {self.settings.claude_code_timeout_seconds}s")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _call_codex(self, prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
        """Call OpenAI Codex CLI (codex exec) for code generation.

        Strategy: write context files into a temp directory, run ``codex exec``
        there, then diff original vs modified files to produce a unified diff.
        """
        codex_cmd = shutil.which(self.settings.codex_command)
        if not codex_cmd:
            raise CodegenError(f"Codex CLI not found: {self.settings.codex_command}")

        workdir = tempfile.mkdtemp(prefix="ops_codex_")
        try:
            # Write context files into the working directory
            for rel_path, content in context_files.items():
                full = Path(workdir) / rel_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")

            # Initialize git repo so codex trusts the directory
            subprocess.run(
                ["git", "init"], cwd=workdir,
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "add", "."], cwd=workdir,
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "-c", "user.name=ops", "-c", "user.email=ops@local",
                 "commit", "-m", "init", "--allow-empty"],
                cwd=workdir, capture_output=True, timeout=10,
            )

            # Build instruction for Codex — passed via stdin to avoid
            # Windows command-line length limits
            codex_instruction = (
                "You are modifying files in this directory to implement the following task.\n"
                "Only modify or create the files described. Do not delete unrelated files.\n\n"
                f"{prompt}"
            )

            env = {**os.environ}
            if self.settings.openai_api_key:
                env["OPENAI_API_KEY"] = self.settings.openai_api_key

            cmd = [
                codex_cmd, "exec",
                "--full-auto",
                "-",  # read prompt from stdin
            ]

            result = subprocess.run(
                cmd,
                input=codex_instruction,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=int(self.settings.codex_timeout_seconds),
            )

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:500]
                raise CodegenError(f"Codex CLI failed (rc={result.returncode}): {stderr}")

            # Diff original vs modified files
            modified_files: list[dict[str, str]] = []
            work_path = Path(workdir)
            for file_path in work_path.rglob("*"):
                if file_path.is_dir():
                    continue
                rel = str(file_path.relative_to(work_path)).replace("\\", "/")
                # Skip git internals
                if rel.startswith(".git/") or rel.startswith(".git\\"):
                    continue
                try:
                    new_content = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                original_content = context_files.get(rel, "")
                if new_content != original_content:
                    modified_files.append({
                        "path": rel,
                        "content": new_content,
                        "summary": "Modified by Codex CLI",
                    })

            if not modified_files:
                raise CodegenError("Codex CLI did not modify any files.")

            diff_text, files_changed = self._generate_diff_from_files(context_files, modified_files)
            file_summaries = [{"path": f["path"], "summary": f["summary"]} for f in modified_files]
            summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"

            return CodegenResult(
                diff=diff_text,
                summary=summary,
                files_changed=files_changed,
                file_summaries=file_summaries,
                provider_name="codex",
                model_name="codex-cli",
            )
        except subprocess.TimeoutExpired:
            raise CodegenError(f"Codex CLI timed out after {self.settings.codex_timeout_seconds}s")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _build_prompt(
        self,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        json_mode: bool = False,
    ) -> str:
        """Build the LLM prompt for code generation."""
        # Extract clear objective from plan
        objective = plan_json.get("objective", "")
        change_explanation = plan_json.get("change_explanation", "")
        change_summary = plan_json.get("change_summary", "")

        if json_mode:
            parts = [
                "Generate modified or new file contents that implement the task below.",
                "",
                "IMPORTANT RULES:",
                "- ONLY include files you actually changed or newly created. Do NOT include unchanged files.",
                "- If a file has no relevant code to modify, skip it entirely.",
                "- The 'content' field must be the COMPLETE file after your modifications.",
                "- Only create new files when the task explicitly requires new files. If the task says to modify existing files, modify THOSE files — do NOT create wrapper or helper files instead.",
                "- Return only valid JSON using the required files array.",
            ]
        else:
            parts = [
                "Generate a unified diff that implements the task below.",
                "",
                "IMPORTANT RULES:",
                "- ONLY output diff hunks for files you actually changed.",
                "- If a file has no relevant code to modify, do NOT include it in the diff.",
                "- Return only the diff.",
            ]

        # Clear objective section
        parts.extend(["", "=== OBJECTIVE ==="])
        if objective:
            parts.append(objective)
        if change_summary:
            parts.append(f"Summary: {change_summary}")
        if change_explanation:
            parts.append(f"Details: {change_explanation}")

        if task_description.strip():
            parts.extend(["", "=== TASK DESCRIPTION ===", task_description.strip()])

        # Inject constraints from translation if available
        constraints = plan_json.get("constraints") or []
        if not constraints:
            # Try to extract from the task description for backwards compat
            td_lower = task_description.lower()
            if "do not create new files" in td_lower or "touch only" in td_lower:
                constraints.append("Do not create new files.")
            if "only" in td_lower and "files" in td_lower:
                import re as _re
                m = _re.search(r"touch only (?:those |these )?(\w+) files?", td_lower)
                if m:
                    constraints.append(f"Touch only the {m.group(1)} specified files.")
        if constraints:
            parts.extend(["", "=== CONSTRAINTS (MUST OBEY) ==="])
            for c in constraints:
                parts.append(f"- {c}")

        # Compact plan: only include steps, not full JSON
        steps = plan_json.get("steps", [])
        if steps:
            parts.extend(["", "=== PLAN STEPS ==="])
            for step in steps:
                title = step.get("title", "")
                expected = step.get("expected_output", "")
                parts.append(f"- {title}: {expected}")

        # Separate existing files from new files (empty content = to be created)
        existing_files = {f: c for f, c in context_files.items() if c.strip()}
        new_files = [f for f, c in context_files.items() if not c.strip()]

        if existing_files:
            parts.extend(["", "=== FILE CONTEXT (existing files) ==="])
            for filename, content in existing_files.items():
                parts.extend(
                    [
                        "",
                        f"--- BEGIN FILE {filename} ---",
                        content,
                        f"--- END FILE {filename} ---",
                    ]
                )

        if new_files:
            parts.extend([
                "",
                "=== NEW FILES TO CREATE ===",
                "The following files do NOT exist yet. You MUST create them with full content.",
            ])
            for filename in new_files:
                parts.append(f"- {filename}")

        if not existing_files and not new_files:
            parts.extend(["", "=== FILE CONTEXT ===", "(no existing files)"])

        return "\n".join(parts)

    def _mock_generate(self, plan_json: dict[str, Any], context_files: dict[str, str]) -> CodegenResult:
        """Deterministic mock for testing: produce a minimal valid diff from the first context file."""
        del plan_json
        if not context_files:
            raise CodegenError("No context files provided for code generation.")

        first_file = next(iter(context_files))
        first_line = _first_line(context_files[first_file])
        if first_line is None:
            diff = (
                f"diff --git a/{first_file} b/{first_file}\n"
                f"--- a/{first_file}\n"
                f"+++ b/{first_file}\n"
                "@@ -0,0 +1 @@\n"
                "+# Generated change for task\n"
            )
        else:
            diff = (
                f"diff --git a/{first_file} b/{first_file}\n"
                f"--- a/{first_file}\n"
                f"+++ b/{first_file}\n"
                "@@ -1,1 +1,2 @@\n"
                f" {first_line}\n"
                "+# Generated change for task\n"
            )
        return CodegenResult(
            diff=diff,
            summary=f"Mock patch: added comment to {first_file}",
            files_changed=DiffReviewer.parse_changed_files(diff),
            provider_name="mock",
            model_name="mock",
        )

    def _call_minimax(self, prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
        """Call MiniMax API for code generation."""
        if not self.settings.minimax_api_key:
            raise CodegenError("OPS_AGENT_MINIMAX_API_KEY is not configured.")

        model_name = self._resolve_model_name("minimax")
        url = f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT_JSON_MODE},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 32768,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=max(self.settings.minimax_planner_timeout_seconds, 180),
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            modified_files = self._parse_json_codegen_response(content)
            diff, files_changed = self._generate_diff_from_files(context_files, modified_files)
            file_summaries = [
                {"path": f["path"], "summary": f.get("summary", "")}
                for f in modified_files if f.get("summary")
            ]
            summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"
            return CodegenResult(
                diff=diff,
                summary=summary,
                files_changed=files_changed,
                file_summaries=file_summaries,
                provider_name="minimax",
                model_name=model_name,
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"MiniMax API error: {exc}") from exc

    def _trim_context_for_ollama(self, context_files: dict[str, str]) -> dict[str, str]:
        """Limit file count and size for local Ollama models with limited context/speed."""
        max_files = int(getattr(self.settings, "ollama_max_context_files", 2))
        max_chars = int(getattr(self.settings, "ollama_max_file_chars", 8000))
        trimmed: dict[str, str] = {}
        for path, content in list(context_files.items())[:max_files]:
            if len(content) > max_chars:
                trimmed[path] = content[:max_chars] + f"\n// ... truncated ({len(content)} chars total)\n"
            else:
                trimmed[path] = content
        return trimmed

    def _call_ollama(self, prompt: str, *, context_files: dict[str, str] | None = None) -> CodegenResult:
        """Call local Ollama server (OpenAI-compatible) for code generation in JSON mode."""
        model_name = self.settings.ollama_model
        url = f"{self.settings.ollama_base_url.rstrip('/')}/chat/completions"
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT_JSON_MODE},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            modified_files = self._parse_json_codegen_response(content)
            diff, files_changed = self._generate_diff_from_files(context_files or {}, modified_files)
            file_summaries = [
                {"path": f["path"], "summary": f.get("summary", "")}
                for f in modified_files if f.get("summary")
            ]
            summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"
            return CodegenResult(
                diff=diff,
                summary=summary,
                files_changed=files_changed,
                file_summaries=file_summaries,
                provider_name="ollama",
                model_name=model_name,
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"Ollama API error: {exc}") from exc

    def _call_deepseek(self, prompt: str) -> CodegenResult:
        """Call DeepSeek API (OpenAI-compatible) for code generation."""
        if not self.settings.deepseek_api_key:
            raise CodegenError("OPS_AGENT_DEEPSEEK_API_KEY is not configured.")

        model_name = self.settings.deepseek_model
        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=self.settings.deepseek_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return self._parse_response(
                content,
                provider_name="deepseek",
                model_name=model_name,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"DeepSeek API error: {exc}") from exc

    def _call_openai(self, prompt: str) -> CodegenResult:
        """Call OpenAI API for code generation."""
        if not self.settings.openai_api_key:
            raise CodegenError("OPS_AGENT_OPENAI_API_KEY is not configured.")

        model_name = self._resolve_model_name("openai")
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=getattr(self.settings, "primary_agent_timeout_seconds", 90),
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return self._parse_response(
                content,
                provider_name="openai",
                model_name=model_name,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"OpenAI API error: {exc}") from exc

    def _parse_response(
        self,
        content: str,
        *,
        provider_name: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> CodegenResult:
        """Extract a unified diff from an LLM response. Handles markdown code fences and preambles."""
        diff = content.strip()
        if diff.startswith("```"):
            diff = re.sub(r"^```(?:diff|patch)?\s*", "", diff)
            diff = re.sub(r"\s*```$", "", diff).strip()

        if not diff.startswith("diff --git") and not diff.startswith("---"):
            match = re.search(r"(diff --git .+)", diff, re.DOTALL)
            if match:
                diff = match.group(1).strip()

        if not diff.startswith("diff --git") and not diff.startswith("---"):
            raise CodegenError("LLM response does not contain a valid unified diff.")

        files_changed = DiffReviewer.parse_changed_files(diff)
        if not files_changed:
            raise CodegenError("LLM response did not include any changed file headers.")

        summary = f"Generated patch modifying {len(files_changed)} file(s): {', '.join(files_changed[:5])}"

        return CodegenResult(
            diff=diff,
            summary=summary,
            files_changed=files_changed,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
        )

    def _parse_json_codegen_response(self, content: str) -> list[dict[str, Any]]:
        """Parse JSON codegen response (MiniMax, Claude Code, Ollama). Handles markdown code fences."""
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # Include the first 300 chars of content for debugging
            preview = text[:300].replace("\n", "\\n")
            raise CodegenError(
                f"JSON codegen response could not be parsed: {exc} | content_preview: {preview}"
            ) from exc

        if not isinstance(data, dict):
            raise CodegenError("JSON codegen response must be an object with a files array.")

        files = data.get("files", [])
        if not files:
            raise CodegenError("JSON codegen response contains no files.")
        if not isinstance(files, list):
            raise CodegenError("JSON codegen response files field must be a list.")

        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise CodegenError("JSON codegen response has a non-object file entry.")
            if not isinstance(file_entry.get("path"), str) or not file_entry["path"].strip():
                raise CodegenError("JSON codegen response has file entry with missing path.")
            if not isinstance(file_entry.get("content"), str):
                raise CodegenError(f"JSON codegen response has no content for {file_entry['path']}.")

        return files

    def _generate_diff_from_files(
        self,
        original_files: dict[str, str],
        modified_files: list[dict[str, Any]],
    ) -> tuple[str, list[str]]:
        """Generate a unified diff from original and modified file contents."""
        diff_parts: list[str] = []
        files_changed: list[str] = []

        for modified_file in modified_files:
            path = modified_file["path"].strip()
            new_content = modified_file["content"]

            old_content = original_files.get(path, "")
            is_new_file = path not in original_files or not old_content.strip()

            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(),
                    new_content.splitlines(),
                    fromfile="/dev/null" if is_new_file else f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            if not diff_lines:
                continue

            if is_new_file:
                diff_parts.append(f"diff --git a/{path} b/{path}")
                diff_parts.append("new file mode 100644")
                diff_parts.extend(diff_lines)
            else:
                diff_parts.append(f"diff --git a/{path} b/{path}")
                diff_parts.extend(diff_lines)
            files_changed.append(path)

        if not diff_parts:
            raise CodegenError("JSON codegen response produced no files with changes.")

        return "\n".join(diff_parts) + "\n", files_changed


def _first_line(content: str) -> str | None:
    if not content.strip():
        return None
    return content.splitlines()[0]


def _is_retryable_codegen_error(exc: CodegenError) -> bool:
    message = str(exc).lower()
    return any(
        key in message
        for key in (
            "valid unified diff",
            "changed file headers",
            "json",
            "no files",
            "missing path",
            "empty output",
        )
    )


def _is_provider_level_error(exc: CodegenError) -> bool:
    """Return True if this error means the provider itself is unavailable (auth, billing, network)
    and we should try the next provider instead of retrying or failing."""
    message = str(exc).lower()
    return any(
        key in message
        for key in (
            "api error",
            "credit balance",
            "usage limit",
            "unauthorized",
            "forbidden",
            "rate limit",
            "timeout",
            "not configured",
            "not found",
            "connection",
            "503",
            "502",
            "500",
        )
    )
