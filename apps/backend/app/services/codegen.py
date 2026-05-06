from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from app.agents.schemas import CodegenResult
from app.core.config import Settings, get_settings
from app.core.timeouts import external_http_timeout
from app.services.llm_cache import cached_http_post
from app.services.llm_telemetry import LlmCall, log_llm_cache_hit, record_llm_call
from app.services.reviewer import DiffReviewer


MEMORY_PROMPT_INSTRUCTION = (
    "Memory section is informational background. If memory contradicts must_touch_files "
    "or change_summary or evidence bundle citations, OBEY THE CURRENT SPEC. Memory "
    "is past observation, not future authority."
)


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


CODEGEN_KOTLIN_GUIDANCE = """

KOTLIN / COMPOSE SYNTAX CONSTRAINTS (when the diff touches .kt or .kts files):

1. `import` statements MUST be at the top of the file, immediately after `package`. NEVER place an `import` inside a class body, function body, or annotation block (e.g. NOT inside `@Composable`).

2. For a `class`, `object`, `data class`, or function: the opening `{` MUST be on the SAME line as the signature. Do NOT put `{` on a new line:
   YES:  `data class Job(val x: Int) {`
   NO:   `data class Job(val x: Int)`
         `{`

3. Composable navigation `composable("route") { ... }` blocks MUST be balanced. When inserting a new `LaunchedEffect { ... }` inside a composable block, ensure the inner `{` and `}` are paired and the OUTER closing `}` of the `composable("route") {` is preserved.

4. Every `composable("...")` block MUST close with `}` before the next `composable("...")` opens. Do NOT delete or shift the closing `}` of an existing block when adding logic inside it.

5. For data class secondary constructors / companion objects: `companion object { ... }` MUST be inside the class body braces, not after the closing `)` of the primary constructor:
   YES:  `data class X(val a: Int) { companion object { fun build() = X(1) } }`
   NO:   `data class X(val a: Int)` followed by ` { companion object { ... } }` on a new line.

6. When generating a unified diff that modifies Kotlin: the hunk's context lines (lines NOT prefixed with `+` or `-`) MUST EXACTLY MATCH the source file. Do NOT paraphrase, reformat, or trim whitespace from context lines. Hunk drift on Kotlin frequently causes structural breakage that compile_gate catches but repair cannot fix.

7. Prefer `LaunchedEffect(Unit) { ... }` for one-shot side effects in Compose; use `remember { ... }` for lifecycle-scoped state.

8. When adding a state read like `val context = LocalContext.current`, put it OUTSIDE any nested lambdas (at the composable's direct scope), not inside `LaunchedEffect` (LocalContext is composition-scope only).

If your diff violates any of these, the post-codegen self-validation OR compile_gate will reject and the task fails. Output a clean diff that compiles."""


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


RAW_DIFF_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Output ONLY the raw unified diff. "
    "Do NOT wrap with === markers, code fences, comments, or "
    "any prose. Start your response with the line "
    "'diff --git a/...' and end at the last hunk line."
)


class CodegenError(Exception):
    pass


class CodeGenerator:
    def __init__(self, settings: Settings | None = None, *, db: Session | None = None):
        self.settings = settings or get_settings()
        self.db = db

    @staticmethod
    def _extract_plan_target_paths(plan_json: dict[str, Any]) -> tuple[list[str], list[str], set[str]]:
        def clean(values: Any) -> list[str]:
            if not isinstance(values, list):
                return []
            cleaned: list[str] = []
            seen: set[str] = set()
            for value in values:
                if not isinstance(value, str):
                    continue
                path = value.strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                cleaned.append(path)
            return cleaned

        must_touch = clean(plan_json.get("must_touch_files"))
        expected_new = clean(plan_json.get("expected_new_files"))
        return must_touch, expected_new, set(must_touch) | set(expected_new)

    @staticmethod
    def _paths_match(left: str, right: str) -> bool:
        """Path-segment suffix-tolerant equality (mirrors evidence_chain helper)."""
        if not left or not right:
            return False
        if left == right:
            return True
        if right.endswith("/" + left):
            return True
        if left.endswith("/" + right):
            return True
        return False

    @staticmethod
    def _augment_prompt_for_kotlin(
        base_prompt: str,
        context_files: dict[str, str] | None,
    ) -> str:
        """Append Kotlin-specific syntax guidance when context contains
        .kt/.kts files. Mitigates recurring Kotlin codegen syntax bugs
        (import-in-annotation, brace-on-new-line, hunk-drift-removes-brace)
        at prompt level (Stage B1).

        L4b: when ANY context file uses Compose (`@Composable`), append
        a stricter scope-rules clarification. Empirical (P69-17 v26):
        DeepSeek calls `viewModel(...)`, `LaunchedEffect{...}`, `remember{}`
        outside @Composable function bodies, triggering "@Composable
        invocations can only happen from the context of a @Composable
        function" compile errors that the repair loop can't reliably fix.

        L4a (companion): the import-preservation guidance is also
        emphasized so DeepSeek stops dropping the original file's
        import block when re-emitting bodies (the v26 round-1 failure
        mode that produced 12 'Unresolved reference' errors).
        """
        if not context_files:
            return base_prompt
        kt_files = [
            (path, content)
            for path, content in context_files.items()
            if str(path).lower().endswith((".kt", ".kts"))
        ]
        if not kt_files:
            return base_prompt

        out = base_prompt + CODEGEN_KOTLIN_GUIDANCE
        # L4a — explicit import-preservation guard (general for all .kt files)
        out += (
            "\n\nIMPORT-PRESERVATION RULE (L4a — repeated DeepSeek failure mode):\n"
            "When you emit a unified diff that modifies an existing .kt file, "
            "you MUST preserve the file's original `import` block. Do NOT "
            "delete `import` lines unless the symbol is no longer used. "
            "If your patch references symbols like `rememberNavController`, "
            "`viewModel`, `LaunchedEffect`, `JobPostingViewModel`, etc., "
            "the corresponding `import androidx.navigation.compose.rememberNavController`, "
            "`import androidx.lifecycle.viewmodel.compose.viewModel`, etc. MUST "
            "exist in the post-patch file. Dropping them produces "
            "'Unresolved reference' compile errors that the repair loop "
            "cannot reliably fix."
        )

        # L4d — multi-file cross-naming consistency rule. Triggers
        # when context contains >= 2 source files. Empirical (v27 P69-17
        # with DeepSeek): codegen renamed `jobLocation` -> `location`
        # in Job.kt but JobPostingFragment.kt kept the old reference,
        # producing 'Unresolved reference' compile errors that
        # oscillated round-to-round (jobLocation -> location -> address
        # -> jobLocation) without ever converging.
        if len(kt_files) >= 2:
            other_paths = ", ".join(p for p, _ in kt_files[:6])
            out += (
                "\n\nCROSS-FILE NAMING CONSISTENCY (L4d — repeated "
                "DeepSeek failure mode in v27):\n"
                f"You are editing MULTIPLE files in the same module: "
                f"{other_paths}.\n"
                "  * If you RENAME a property, function, or class in one "
                "file (e.g. `jobLocation` -> `location` in Job.kt), you "
                "MUST update EVERY reference to it in the other file(s) "
                "in the same patch. Do NOT change a name in one file and "
                "leave callers in other files referencing the old name.\n"
                "  * Before emitting your diff, mentally cross-check: "
                "every property/method/class your patch references in "
                "file A — does it exist (with that exact name) in the "
                "definition file B that your patch is also touching?\n"
                "  * Inconsistent naming across files produces "
                "'Unresolved reference' errors that the compile_repair "
                "loop cannot fix because each round renames again "
                "(jobLocation -> location -> address -> jobLocation), "
                "never converging.\n"
                "  * If you are unsure of the canonical name, KEEP THE "
                "ORIGINAL NAME from the source file — do not rename "
                "fields gratuitously."
            )

        # L4b — Compose context detection: scan content for @Composable
        any_compose = any(
            "@Composable" in (content or "") for _, content in kt_files
        )
        if any_compose:
            out += (
                "\n\nCOMPOSE SCOPE RULES (L4b — repeated misuse seen in v26):\n"
                "The file you are editing uses Jetpack Compose (@Composable).\n"
                "  * `viewModel()`, `LaunchedEffect { ... }`, `remember { ... }`,\n"
                "    `rememberCoroutineScope()`, `LocalContext.current`, and any\n"
                "    other Compose API call MUST be invoked ONLY from inside a\n"
                "    function annotated with `@Composable` (or inside a lambda\n"
                "    that itself runs in a Composable context such as the body\n"
                "    of `LaunchedEffect`).\n"
                "  * Do NOT call them from `onCreateView`, `onViewCreated`,\n"
                "    `apply { }` blocks, or from a regular `fun foo() { ... }`\n"
                "    that lacks the `@Composable` annotation.\n"
                "  * If you need to wire a side-effect in a non-Composable\n"
                "    method, use the existing `setContent { ... }` block or\n"
                "    create a `@Composable` helper and invoke it from there.\n"
                "  * Compose API references inserted outside @Composable scope "
                "produce '@Composable invocations can only happen from the "
                "context of a @Composable function' and the repair loop "
                "cannot reliably fix them."
            )
        return out

    @staticmethod
    def _validate_changed_files_within_allowed(
        files_changed: list[str],
        *,
        allowed_paths: set[str],
        must_touch_files: list[str],
        expected_new_files: list[str],
    ) -> None:
        actual_files = {
            path.strip()
            for path in files_changed
            if isinstance(path, str) and path.strip()
        }
        extra = sorted(
            path for path in actual_files
            if not any(CodeGenerator._paths_match(path, allowed) for allowed in allowed_paths)
        )
        if extra:
            raise CodegenError(
                "file_outside_allowed_set: codegen modified files not in plan: "
                f"{extra}. Allowed must_touch_files={sorted(must_touch_files)}, "
                f"expected_new_files={sorted(expected_new_files)}"
            )

    def generate_patch(
        self,
        *,
        task_id: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str = "",
        source_repo_path: str | None = None,
        actor_name: str | None = None,
    ) -> CodegenResult:
        """Generate a unified diff from a plan and file context."""
        # Stage B1: stash current context_files so provider call methods
        # can augment the system prompt with Kotlin-specific syntax
        # constraints when .kt/.kts files are involved (without threading
        # context_files through every provider signature).
        self._current_context_files = dict(context_files or {})
        providers = self._resolve_provider_chain()
        must_touch_files, expected_new_files, allowed_paths = self._extract_plan_target_paths(plan_json)
        enforce = bool(allowed_paths)

        import logging as _log
        _logger = _log.getLogger("codegen.provider_chain")
        _logger.info("Provider chain: %s", providers)

        attempts: list[dict[str, Any]] = []
        for provider_idx, provider in enumerate(providers):
            _logger.info("Trying provider %d/%d: %s", provider_idx + 1, len(providers), provider)
            try:
                result = self._try_provider(
                    provider=provider,
                    task_id=task_id,
                    plan_json=plan_json,
                    context_files=context_files,
                    task_description=task_description,
                    source_repo_path=source_repo_path,
                    actor_name=actor_name,
                    fallback_step=provider_idx,
                )
                if enforce:
                    self._validate_changed_files_within_allowed(
                        result.files_changed,
                        allowed_paths=allowed_paths,
                        must_touch_files=must_touch_files,
                        expected_new_files=expected_new_files,
                    )

                # Stage A: codegen self-validation — validate diff applies
                # + parses before returning to caller. Catches hunk drift
                # at source instead of letting it through to sandbox apply
                # + compile_gate + repair (wastes 3-5 min per failure).
                #
                # IMPORTANT: skip during compile_repair calls. Repair patches
                # are written against the BROKEN sandbox file (not pristine
                # source), so apply-check vs settings.knowledge_source_path
                # would falsely reject every legit repair attempt and loop
                # max_retries before raising. Repair's own validation is
                # the next compile_gate round.
                _is_repair_call = isinstance(task_description, str) and task_description.startswith("Fix syntax errors in")
                if not _is_repair_call and getattr(self.settings, "codegen_self_validation_enabled", True):
                    from app.services.codegen_self_validate import self_validate
                    raw_source = str(getattr(self.settings, "knowledge_source_path", "") or "").strip()
                    source_path = Path(raw_source) if raw_source else None
                    # Skip when source_path is unset / non-existent / not a
                    # real source repo. Test fixtures often leave this unset
                    # and validation against cwd would surface false failures.
                    if source_path is not None and source_path.is_absolute() and source_path.is_dir() and (source_path / ".git").exists():
                        max_retries = int(getattr(self.settings, "codegen_self_validation_max_retries", 1))
                        for sv_attempt in range(max_retries + 1):
                            validation = self_validate(result.diff, source_path)
                            if validation.valid:
                                break
                            if sv_attempt >= max_retries:
                                raise CodegenError(
                                    f"codegen self-validation failed after "
                                    f"{sv_attempt + 1} attempt(s): "
                                    f"{validation.reason}: "
                                    f"{validation.error_detail[:500]}"
                                )
                            # Retry: re-call same provider with validation feedback
                            sv_prompt = self._build_prompt(
                                plan_json,
                                context_files,
                                task_description,
                                json_mode=provider in {"minimax", "ollama"},
                            )
                            retry_prompt = (
                                f"{sv_prompt}\n\n"
                                f"---\n"
                                f"VALIDATION FEEDBACK (your previous attempt failed):\n"
                                f"{validation.reason}\n"
                                f"{validation.error_detail[:1500]}\n\n"
                                f"Regenerate the diff. Make sure the hunk context "
                                f"matches the actual file content (no drift). If "
                                f"parse failed, fix the syntactic error."
                            )
                            _logger.info(
                                "Self-validation retry %d/%d for provider %s",
                                sv_attempt + 1, max_retries, provider,
                            )
                            result = self._try_provider(
                                provider=provider,
                                task_id=task_id,
                                plan_json=plan_json,
                                context_files=context_files,
                                task_description=task_description,
                                source_repo_path=source_repo_path,
                                actor_name=actor_name,
                                fallback_step=provider_idx,
                                override_prompt=retry_prompt,
                            )
                            if enforce:
                                self._validate_changed_files_within_allowed(
                                    result.files_changed,
                                    allowed_paths=allowed_paths,
                                    must_touch_files=must_touch_files,
                                    expected_new_files=expected_new_files,
                                )

                _logger.info("Provider %s succeeded: %d files changed", provider, len(result.files_changed))
                attempts.append({"provider": provider, "status": "succeeded"})
                try:
                    result.attempt_history = attempts
                except Exception:
                    result = result.model_copy(update={"attempt_history": attempts})
                return result
            except CodegenError as exc:
                _logger.warning("Provider %s failed: %s", provider, str(exc)[:300])
                attempts.append({
                    "provider": provider,
                    "status": "failed",
                    "error": str(exc)[:300],
                })
                if _is_provider_level_error(exc) and provider_idx < len(providers) - 1:
                    _logger.info("Classified as provider-level error — trying next provider")
                    continue
                raise

        raise CodegenError("No codegen provider available.")

    def _try_provider(
        self,
        *,
        provider: str,
        task_id: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        source_repo_path: str | None = None,
        actor_name: str | None = None,
        fallback_step: int = 0,
        override_prompt: str | None = None,
    ) -> CodegenResult:
        """Attempt codegen with a single provider, with up to 3 retries for parse errors.

        When override_prompt is set, use it directly (skip _build_prompt).
        This is used by generate_patch for self-validation retries.
        """
        if provider == "ollama":
            context_files = self._trim_context_for_ollama(context_files)

        if override_prompt is not None:
            prompt = override_prompt
        else:
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
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
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
                started = time.perf_counter()
                if provider == "claude_code":
                    result = self._call_claude_code(
                        call_prompt,
                        context_files=context_files,
                        source_repo_path=source_repo_path,
                        task_id=task_id,
                    )
                elif provider == "codex":
                    result = self._call_codex(call_prompt, context_files=context_files)
                elif provider == "anthropic":
                    result = self._call_anthropic(call_prompt)
                elif provider == "deepseek":
                    result = self._call_deepseek(call_prompt)
                elif provider == "ollama":
                    result = self._call_ollama(call_prompt, context_files=context_files)
                elif provider == "minimax":
                    result = self._call_minimax(call_prompt, context_files=context_files)
                elif provider == "openai":
                    result = self._call_openai(call_prompt)
                else:
                    raise CodegenError(f"Unknown provider: {provider}")
                self._record_codegen_call(
                    task_id=task_id,
                    actor_name=actor_name,
                    result=result,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=True,
                    retry_count=attempt,
                    fallback_step=fallback_step,
                    prompt_fingerprint=prompt_fingerprint,
                )
                return result
            except CodegenError as exc:
                self._record_codegen_failure(
                    task_id=task_id,
                    actor_name=actor_name,
                    provider=provider,
                    latency_ms=int((time.perf_counter() - started) * 1000) if "started" in locals() else 0,
                    retry_count=attempt,
                    fallback_step=fallback_step,
                    prompt_fingerprint=prompt_fingerprint,
                    error_type=type(exc).__name__,
                )
                if _is_retryable_codegen_error(exc):
                    last_error = str(exc)
                    continue
                raise

        raise CodegenError(f"Failed to generate valid diff after {max_attempts} attempts. Last error: {last_error}")

    def _record_codegen_call(
        self,
        *,
        task_id: str,
        actor_name: str | None,
        result: CodegenResult,
        latency_ms: int,
        success: bool,
        retry_count: int,
        fallback_step: int,
        prompt_fingerprint: str,
    ) -> None:
        if self.db is None:
            return
        record_llm_call(
            self.db,
            LlmCall(
                purpose="codegen",
                provider=result.provider_name,
                model=result.model_name or "",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
                success=success,
                retry_count=retry_count,
                fallback_step=fallback_step,
                prompt_fingerprint=prompt_fingerprint,
                task_id=task_id,
                actor_name=actor_name,
            ),
        )

    def _record_codegen_failure(
        self,
        *,
        task_id: str,
        actor_name: str | None,
        provider: str,
        latency_ms: int,
        retry_count: int,
        fallback_step: int,
        prompt_fingerprint: str,
        error_type: str,
    ) -> None:
        if self.db is None:
            return
        record_llm_call(
            self.db,
            LlmCall(
                purpose="codegen",
                provider=provider,
                model=self._resolve_model_name(provider) if provider not in {"claude_code", "codex"} else provider,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                success=False,
                retry_count=retry_count,
                fallback_step=fallback_step,
                error_type=error_type,
                prompt_fingerprint=prompt_fingerprint,
                task_id=task_id,
                actor_name=actor_name,
            ),
        )

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
        # Claude Code CLI first — full worktree codegen with repo visibility.
        # Codex as fallback (no worktree support, frequent timeouts on large
        # context).
        if shutil.which(self.settings.claude_code_command):
            chain.append("claude_code")
        # 2026-05-04: DeepSeek-V4-Pro promoted to 2nd-priority fallback after
        # claude_code, ahead of codex CLI. Subagent experiment Stage 25.6/25.7
        # showed DeepSeek delivers production-ready commits on small/medium
        # tasks; codex CLI keeps 3rd slot for full-context worktree work.
        if getattr(self.settings, "deepseek_api_key", None):
            chain.append("deepseek")
        if shutil.which(self.settings.codex_command):
            chain.append("codex")
        if getattr(self.settings, "anthropic_api_key", None):
            chain.append("anthropic")
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
            resp = httpx.get(
                f"{self.settings.ollama_base_url.replace('/v1', '')}/api/tags",
                timeout=external_http_timeout(2),
            )
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
            "system": self._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT, getattr(self, "_current_context_files", None)),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=external_http_timeout(120),
            )
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

    def _call_claude_code(
        self,
        prompt: str,
        *,
        context_files: dict[str, str],
        source_repo_path: str | None = None,
        task_id: str | None = None,
    ) -> CodegenResult:
        """Call Claude Code CLI for code generation.

        **Worktree mode** (when ``source_repo_path`` points to a valid git
        repo): creates a ``git worktree`` from the source repo so Claude Code
        has full repository visibility — it can explore related modules, run
        tests, and verify its own changes before returning.  The diff is
        extracted via ``git diff HEAD``.

        **Temp-dir mode** (fallback): copies only ``context_files`` into a
        disposable ``tempfile.mkdtemp()``, initialises an empty git repo, and
        diffs the filesystem after Claude finishes.
        """
        claude_cmd = shutil.which(self.settings.claude_code_command)
        if not claude_cmd:
            raise CodegenError(f"Claude Code CLI not found: {self.settings.claude_code_command}")

        repo_root = self._resolve_git_repo_root(source_repo_path)

        if repo_root is not None:
            return self._call_claude_code_worktree(
                prompt,
                context_files=context_files,
                source_repo_path=repo_root,
                claude_cmd=claude_cmd,
                task_id=task_id,
            )
        return self._call_claude_code_tempdir(
            prompt,
            context_files=context_files,
            claude_cmd=claude_cmd,
        )

    # ---- worktree-based codegen ------------------------------------------- #

    @staticmethod
    def _resolve_git_repo_root(source_repo_path: str | None) -> Path | None:
        """Return the git repository root for *source_repo_path*, or None."""
        if not source_repo_path:
            return None
        try:
            candidate = Path(source_repo_path)
        except (OSError, TypeError):
            return None
        if not candidate.is_dir():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        root = (result.stdout or "").strip()
        return Path(root) if root else None

    def _call_claude_code_worktree(
        self,
        prompt: str,
        *,
        context_files: dict[str, str],
        source_repo_path: Path,
        claude_cmd: str,
        task_id: str | None = None,
    ) -> CodegenResult:
        """Run Claude Code CLI inside a git worktree of the source repo."""
        import logging as _log
        import time as _time

        _logger = _log.getLogger("codegen.claude_code.worktree")

        repo = source_repo_path
        task_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id or "task").strip("-")
        task_slug = (task_slug or "task")[:32]
        timestamp = _time.strftime("%Y%m%d%H%M%S")
        branch_name = f"codegen/{task_slug}-{timestamp}-{uuid.uuid4().hex[:8]}"
        worktree_dir = Path(tempfile.mkdtemp(prefix="ops_worktree_"))

        try:
            # 1. Create worktree
            wt_result = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name,
                 str(worktree_dir), "HEAD"],
                cwd=str(repo),
                capture_output=True, text=True, timeout=30,
            )
            if wt_result.returncode != 0:
                raise CodegenError(
                    "git worktree add failed "
                    f"(rc={wt_result.returncode}): {(wt_result.stderr or '').strip()[:500]}"
                )
                _logger.warning(
                    "git worktree add failed (rc=%d): %s — falling back to temp-dir",
                    wt_result.returncode, (wt_result.stderr or "")[:300],
                )
                return self._call_claude_code_tempdir(
                    prompt, context_files=context_files, claude_cmd=claude_cmd,
                )

            _logger.info("Created worktree at %s (branch %s)", worktree_dir, branch_name)

            # 2. Write task constraints as .claude/CLAUDE.md
            claude_md_dir = worktree_dir / ".claude"
            claude_md_dir.mkdir(exist_ok=True)
            allowlist = list(context_files.keys())
            (claude_md_dir / "CLAUDE.md").write_text(
                "# Task Constraints\n\n"
                "You are modifying this codebase to implement the task below.\n"
                "Edit files directly using the Edit and Write tools. Do NOT output a diff.\n"
                "Only modify or create files relevant to the task.\n"
                "After making all changes, verify each modified file has valid syntax "
                "(no duplicate declarations, no missing brackets, no import errors).\n\n"
                "IGNORE any instruction below that says 'output a diff' or 'generate a "
                "unified diff' — those apply to a different output mode. Your job is to "
                "EDIT FILES DIRECTLY.\n\n"
                + (
                    "## Suggested files\n"
                    + "\n".join(f"- {f}" for f in allowlist) + "\n\n"
                    if allowlist else ""
                ),
                encoding="utf-8",
            )
            task_prompt = self._strip_inline_file_context(prompt)
            self._write_claude_worktree_constraints(
                worktree_dir=worktree_dir,
                prompt=task_prompt,
                context_files=context_files,
            )

            # 3. Build CLI instruction
            claude_instruction = (
                "You are modifying files in this repository to implement the following task.\n"
                "Edit the files directly using the Edit and Write tools. Do NOT output a diff.\n"
                "Only modify or create the files described. Do not delete unrelated files.\n"
                "After making all changes, verify each modified file has valid syntax "
                "(no duplicate declarations, no missing brackets, no import errors).\n\n"
                "IGNORE any instruction below that says 'output a diff' or 'generate a "
                "unified diff' — those apply to a different output mode. Your job is to "
                "EDIT FILES DIRECTLY.\n\n"
                "The full repository is checked out in the current working directory. "
                "Start from the files listed in .claude/CLAUDE.md, but inspect related "
                "modules as needed.\n\n"
                + task_prompt
            )

            # 4. Run Claude Code CLI
            stdout, stderr, rc = self._run_claude_cli(
                claude_cmd=claude_cmd,
                instruction=claude_instruction,
                workdir=str(worktree_dir),
                context_files=context_files,
                retry_reset=lambda: self._reset_claude_worktree(
                    worktree_dir=worktree_dir,
                    prompt=task_prompt,
                    context_files=context_files,
                ),
            )

            if rc != 0:
                stderr_text = (stderr or "").strip()[:500]
                raise CodegenError(
                    f"Claude Code CLI codegen failed (rc={rc}): {stderr_text}"
                )

            # 5. Extract diff via git diff HEAD. `git add -N` makes
            # untracked files appear in the worktree diff without staging
            # actual content.
            subprocess.run(
                ["git", "add", "-N", "--", "."],
                cwd=str(worktree_dir),
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(worktree_dir),
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            if diff_proc.returncode != 0:
                raise CodegenError(
                    "git diff HEAD failed "
                    f"(rc={diff_proc.returncode}): {(diff_proc.stderr or '').strip()[:500]}"
                )
            diff_text = self._filter_diff_excluding_paths(
                (diff_proc.stdout or "").strip(),
                excluded_prefixes=(".claude/",),
            ).strip()

            if not diff_text:
                # Fallback: try filesystem comparison like temp-dir mode
                _logger.info("No git diff output; falling back to filesystem comparison")
                modified_files = self._scan_worktree_changes(
                    worktree_dir, context_files,
                )
                if not modified_files and stdout and stdout.strip():
                    modified_files = self._parse_claude_code_output(stdout, context_files)
                if not modified_files:
                    out_preview = (stdout or "")[:300].replace("\n", "\\n")
                    err_preview = (stderr or "")[:200].replace("\n", "\\n")
                    raise CodegenError(
                        f"Claude Code CLI did not modify any files. "
                        f"stdout[:{min(len(stdout or ''), 300)}]: {out_preview} | "
                        f"stderr[:{min(len(stderr or ''), 200)}]: {err_preview}"
                    )
                diff_text, files_changed = self._generate_diff_from_files(
                    context_files, modified_files,
                )
                file_summaries = [
                    {"path": f["path"], "summary": f["summary"]}
                    for f in modified_files
                ]
            else:
                # Parse files_changed from diff headers
                files_changed = self._parse_diff_files(diff_text)
                file_summaries = [
                    {"path": f, "summary": "Modified by Claude Code CLI (worktree)"}
                    for f in files_changed
                ]

            summary = (
                f"Generated patch modifying {len(files_changed)} file(s) "
                f"(worktree mode): {', '.join(files_changed[:5])}"
            )
            return CodegenResult(
                diff=diff_text,
                summary=summary,
                files_changed=files_changed,
                file_summaries=file_summaries,
                provider_name="claude_code",
                model_name="claude-code-cli-worktree",
            )
        except CodegenError:
            raise
        except subprocess.TimeoutExpired:
            raise CodegenError(
                f"Claude Code CLI timed out after {self.settings.claude_code_timeout_seconds}s"
            )
        finally:
            self._cleanup_worktree(
                repo_path=str(repo),
                worktree_dir=str(worktree_dir),
                branch_name=branch_name,
            )

    @staticmethod
    def _strip_inline_file_context(prompt: str) -> str:
        """Remove large embedded file bodies when Claude has a full worktree."""

        def _replace_file_block(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            return f"\n- {path} (read from the worktree)"

        stripped = re.sub(
            r"\n--- BEGIN FILE ([^\n]+) ---\n.*?\n--- END FILE \1 ---",
            _replace_file_block,
            prompt,
            flags=re.DOTALL,
        )
        return stripped.replace(
            "=== FILE CONTEXT (existing files) ===",
            "=== RELEVANT FILES (read from worktree) ===",
        )

    @staticmethod
    def _write_claude_worktree_constraints(
        *,
        worktree_dir: Path,
        prompt: str,
        context_files: dict[str, str],
    ) -> None:
        claude_dir = worktree_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        allowed = "\n".join(f"- {path}" for path in sorted(context_files))
        if not allowed:
            allowed = "(No specific file allowlist was provided.)"
        (claude_dir / "CLAUDE.md").write_text(
            "# Task Constraints\n\n"
            "You are modifying this codebase to implement the following task.\n"
            "Edit files directly. Only modify files relevant to the task.\n"
            "After making changes, verify syntax (no duplicate declarations, "
            "no missing brackets, no import errors).\n\n"
            "## Allowed files\n"
            f"{allowed}\n\n"
            "## Task\n"
            f"{prompt}\n",
            encoding="utf-8",
        )

    def _reset_claude_worktree(
        self,
        *,
        worktree_dir: Path,
        prompt: str,
        context_files: dict[str, str],
    ) -> None:
        for cmd in (
            ["git", "reset", "--", "."],
            ["git", "checkout", "--", "."],
            ["git", "clean", "-fd"],
        ):
            result = subprocess.run(
                cmd,
                cwd=str(worktree_dir),
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                raise CodegenError(
                    f"{' '.join(cmd)} failed during Claude Code retry reset: "
                    f"{(result.stderr or '').strip()[:300]}"
                )
        self._write_claude_worktree_constraints(
            worktree_dir=worktree_dir,
            prompt=prompt,
            context_files=context_files,
        )

    @staticmethod
    def _filter_diff_excluding_paths(
        diff_text: str,
        *,
        excluded_prefixes: tuple[str, ...],
    ) -> str:
        sections = re.split(r"(?m)^(?=diff --git )", diff_text)
        kept: list[str] = []
        for section in sections:
            if not section.strip():
                continue
            match = re.match(r"diff --git a/(.+?) b/(.+)", section)
            if not match:
                kept.append(section)
                continue
            old_path = match.group(1).strip()
            new_path = match.group(2).strip()
            if old_path.startswith(excluded_prefixes) or new_path.startswith(excluded_prefixes):
                continue
            kept.append(section)
        return "\n".join(kept)

    @staticmethod
    def _parse_diff_files(diff_text: str) -> list[str]:
        """Extract changed file paths from unified diff headers."""
        files: list[str] = []
        seen: set[str] = set()
        for line in diff_text.splitlines():
            if line.startswith("diff --git a/"):
                parts = line.split(" b/", 1)
                if len(parts) == 2:
                    path = parts[1].strip()
                    if path not in seen:
                        files.append(path)
                        seen.add(path)
        return files

    @staticmethod
    def _scan_worktree_changes(
        worktree_dir: Path,
        context_files: dict[str, str],
    ) -> list[dict[str, str]]:
        """Filesystem scan for context-file changes if git diff is empty."""
        modified: list[dict[str, str]] = []
        for rel, original in context_files.items():
            file_path = worktree_dir / rel
            try:
                new_content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                new_content = ""
            if new_content != original:
                modified.append({
                    "path": rel,
                    "content": new_content,
                    "summary": "Modified by Claude Code CLI",
                })
        return modified

    @staticmethod
    def _cleanup_worktree(
        *, repo_path: str, worktree_dir: str, branch_name: str, temp_root: str | None = None,
    ) -> None:
        """Remove a git worktree and its branch, tolerating Windows file locks."""
        import logging as _log
        _logger = _log.getLogger("codegen.claude_code.worktree")
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_dir],
                cwd=repo_path, capture_output=True, timeout=15,
            )
        except Exception as exc:
            _logger.debug("git worktree remove failed: %s", exc)
        # Force-remove directories on Windows where git object locks linger.
        shutil.rmtree(worktree_dir, ignore_errors=True)
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo_path, capture_output=True, timeout=10,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=repo_path, capture_output=True, timeout=10,
            )
        except Exception as exc:
            _logger.debug("git branch -D %s failed: %s", branch_name, exc)

    # ---- temp-dir fallback codegen ---------------------------------------- #

    def _call_claude_code_tempdir(
        self,
        prompt: str,
        *,
        context_files: dict[str, str],
        claude_cmd: str,
    ) -> CodegenResult:
        """Fallback: run Claude Code CLI in a disposable temp directory.

        Used when no source repo is available for worktree mode.
        """
        import logging as _log
        _logger = _log.getLogger("codegen.claude_code.tempdir")

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

            claude_instruction = (
                "You are modifying files in this directory to implement the following task.\n"
                "Edit the files directly using the Edit and Write tools. Do NOT output a diff.\n"
                "Only modify or create the files described. Do not delete unrelated files.\n"
                "After making all changes, verify each modified file has valid syntax "
                "(no duplicate declarations, no missing brackets, no import errors).\n\n"
                "IGNORE any instruction below that says 'output a diff' or 'generate a "
                "unified diff' — those apply to a different output mode. Your job is to "
                "EDIT FILES DIRECTLY.\n\n"
                + prompt
            )

            def reset_tempdir() -> None:
                for rel_path, content in context_files.items():
                    fp = Path(workdir) / rel_path
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(content, encoding="utf-8")

            stdout, stderr, rc = self._run_claude_cli(
                claude_cmd=claude_cmd,
                instruction=claude_instruction,
                workdir=workdir,
                context_files=context_files,
                retry_reset=reset_tempdir,
            )

            if rc != 0:
                stderr_text = (stderr or "").strip()[:500]
                raise CodegenError(
                    f"Claude Code CLI codegen failed (rc={rc}): {stderr_text}"
                )

            # Diff original vs modified files
            modified_files: list[dict[str, str]] = []
            work_path = Path(workdir)
            for file_path in work_path.rglob("*"):
                if file_path.is_dir():
                    continue
                rel = str(file_path.relative_to(work_path)).replace("\\", "/")
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

            if not modified_files and stdout and stdout.strip():
                _logger.info("No file modifications detected in sandbox; attempting to parse CLI output")
                modified_files = self._parse_claude_code_output(stdout, context_files)

            if not modified_files:
                out_preview = (stdout or "")[:300].replace("\n", "\\n")
                err_preview = (stderr or "")[:200].replace("\n", "\\n")
                raise CodegenError(
                    f"Claude Code CLI did not modify any files. "
                    f"stdout[:{min(len(stdout or ''), 300)}]: {out_preview} | "
                    f"stderr[:{min(len(stderr or ''), 200)}]: {err_preview}"
                )

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

    # ---- shared CLI runner ------------------------------------------------ #

    def _run_claude_cli(
        self,
        *,
        claude_cmd: str,
        instruction: str,
        workdir: str,
        context_files: dict[str, str],
        retry_reset: Callable[[], None] | None = None,
    ) -> tuple[str, str, int]:
        """Run Claude Code CLI subprocess with retry logic.

        Returns ``(stdout, stderr, returncode)``.  Handles timeout,
        Windows process tree cleanup, and retry-on-failure.
        """
        import logging as _log
        import time as _time

        _logger = _log.getLogger("codegen.claude_code")

        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)
        if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
            for candidate in [
                "D:\\Git\\bin\\bash.exe",
                "C:\\Program Files\\Git\\bin\\bash.exe",
                "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
            ]:
                if os.path.isfile(candidate):
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                    break

        prompt_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        prompt_file.write(instruction)
        prompt_file.close()
        prompt_file_path = prompt_file.name

        claude_args = self.settings.claude_code_args.split()
        if "-p" not in claude_args and "--print" not in claude_args:
            claude_args.append("--print")
        if "--dangerously-skip-permissions" not in claude_args:
            claude_args.append("--dangerously-skip-permissions")
        if "--output-format" not in " ".join(claude_args):
            claude_args.extend(["--output-format", "json"])
        cmd = [claude_cmd, *claude_args, "-"]
        timeout_sec = int(self.settings.claude_code_timeout_seconds)
        max_retries = int(getattr(self.settings, "cli_max_retries", 1))

        _logger.info("Claude Code CLI cmd: %s (cwd=%s)", cmd, workdir)

        stdout: str = ""
        stderr: str = ""
        last_rc: int = -1

        try:
            for attempt in range(1 + max_retries):
                if attempt > 0:
                    _logger.info("Claude Code CLI retry %d/%d", attempt, max_retries)
                    if retry_reset is not None:
                        try:
                            retry_reset()
                        except Exception as exc:
                            stderr = str(exc)
                            _logger.warning("Claude Code CLI retry reset failed: %s", exc)
                            last_rc = -1
                            continue
                    _time.sleep(3)

                try:
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
                            _logger.warning("Claude Code CLI timed out (attempt %d)", attempt + 1)
                            stderr = f"timed out after {timeout_sec}s"
                            last_rc = -1
                            continue
                except OSError as exc:
                    stderr = str(exc)
                    _logger.warning("Claude Code CLI OS error (attempt %d): %s", attempt + 1, exc)
                    last_rc = -1
                    continue

                last_rc = proc.returncode
                _logger.info(
                    "Claude Code CLI finished (rc=%d, stdout=%d chars, stderr=%d chars)",
                    last_rc, len(stdout or ""), len(stderr or ""),
                )
                if last_rc == 0:
                    break
                _logger.warning("Claude Code CLI failed rc=%d (attempt %d)", last_rc, attempt + 1)
        finally:
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass

        return stdout, stderr, last_rc

    def _parse_claude_code_output(
        self,
        stdout: str,
        context_files: dict[str, str],
    ) -> list[dict[str, str]]:
        """Best-effort extraction of file contents from Claude Code CLI output.

        When ``-p --output-format json`` is used, the CLI emits a JSON object
        with a ``result`` field containing the assistant's text response.  If
        the response itself contains a JSON code block with a ``files`` array
        (matching our JSON-mode codegen schema), we parse it.  Otherwise we
        attempt to extract raw JSON from the output.

        Returns an empty list if nothing useful can be extracted (caller
        should raise CodegenError).
        """
        text = stdout.strip()

        # --output-format json wraps the response in {"result": "...", ...}
        try:
            wrapper = json.loads(text)
            if isinstance(wrapper, dict) and "result" in wrapper:
                text = wrapper["result"]
        except (json.JSONDecodeError, TypeError):
            pass

        # Try direct JSON parse (the response may be pure JSON)
        try:
            return self._parse_json_codegen_response(text)
        except CodegenError:
            pass

        # Try to find a JSON code block inside the text
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            try:
                return self._parse_json_codegen_response(json_match.group(1))
            except CodegenError:
                pass

        # Try to find raw {"files": [...]} anywhere in the text
        files_match = re.search(r'(\{"files"\s*:\s*\[.*?\]\s*\})', text, re.DOTALL)
        if files_match:
            try:
                return self._parse_json_codegen_response(files_match.group(1))
            except CodegenError:
                pass

        return []

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

        must_touch_files, expected_new_files, allowed_paths = self._extract_plan_target_paths(plan_json)
        if allowed_paths:
            parts.extend(["", "=== ALLOWED FILES (you may only modify or create these) ==="])
            for path in must_touch_files:
                parts.append(f"- {path}")
            for path in expected_new_files:
                parts.append(f"- {path} (new)")
            parts.extend(
                [
                    "",
                    "You MUST NOT modify any other files. If the request seems to require modifying other files, return an error indicating which file you would need.",
                ]
            )

        memory_context = str(plan_json.get("memory_context") or "").strip()
        if memory_context:
            max_lines = max(1, int(getattr(self.settings, "memory_max_lines_in_prompt", 30) or 30))
            memory_lines = memory_context.splitlines()[:max_lines]
            parts.extend(
                [
                    "",
                    "===== Prior gate failure patterns =====",
                    MEMORY_PROMPT_INSTRUCTION,
                    *memory_lines,
                    "===== End memory =====",
                ]
            )

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
                {"role": "system", "content": self._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT_JSON_MODE, getattr(self, "_current_context_files", None)),},
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
                timeout=external_http_timeout(max(self.settings.minimax_planner_timeout_seconds, 180)),
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
                {"role": "system", "content": self._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT_JSON_MODE, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            response = httpx.post(
                url,
                json=body,
                timeout=external_http_timeout(self.settings.ollama_timeout_seconds),
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
        """Call DeepSeek API (OpenAI-compatible) for code generation.

        NOTE: settings.deepseek_base_url may point at the Anthropic-compat
        path (e.g. https://api.deepseek.com/anthropic) when configured for
        the deepseek_agent.py wrapper. /chat/completions only exists on
        the OpenAI-compat path, so hardcode that here independent of the
        configured deepseek_base_url. Same pattern as cc_agent_loop.
        """
        if not self.settings.deepseek_api_key:
            raise CodegenError("OPS_AGENT_DEEPSEEK_API_KEY is not configured.")

        model_name = self.settings.deepseek_model
        try:
            return self._call_deepseek_once(prompt, model_name)
        except CodegenError as exc:
            if "valid unified diff" not in str(exc) and "changed file headers" not in str(exc):
                raise
            return self._call_deepseek_once(prompt + RAW_DIFF_RETRY_SUFFIX, model_name)

    def _call_deepseek_once(self, prompt: str, model_name: str) -> CodegenResult:
        """Call DeepSeek once and parse the raw response."""
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = cached_http_post(
                url=url,
                json=body,
                headers=headers,
                timeout=external_http_timeout(self.settings.deepseek_timeout_seconds),
                provider_hint="codegen.deepseek",
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            log_llm_cache_hit(
                provider="deepseek",
                model=model_name,
                purpose="codegen",
                usage=usage,
            )
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
        try:
            return self._call_openai_once(prompt, model_name)
        except CodegenError as exc:
            if "valid unified diff" not in str(exc) and "changed file headers" not in str(exc):
                raise
            return self._call_openai_once(prompt + RAW_DIFF_RETRY_SUFFIX, model_name)

    def _call_openai_once(self, prompt: str, model_name: str) -> CodegenResult:
        """Call OpenAI once and parse the raw response."""
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        try:
            response = cached_http_post(
                url=url,
                json=body,
                headers=headers,
                timeout=external_http_timeout(getattr(self.settings, "primary_agent_timeout_seconds", 90)),
                provider_hint="codegen.openai",
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            log_llm_cache_hit(
                provider="openai",
                model=model_name,
                purpose="codegen",
                usage=usage,
            )
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
