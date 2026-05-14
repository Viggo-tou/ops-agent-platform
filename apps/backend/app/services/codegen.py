from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("app.services.codegen")

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

MINIMAL-EDIT INVARIANT (L5):
For any file path in must_touch_files (which always refers to files that already exist), your diff MUST be a line-anchored minimal edit using `--- a/<path>` / `+++ b/<path>` hunks. You are FORBIDDEN to emit `new file mode 100644`, `--- /dev/null`, or any `index 0000000..xxxxxxx` header for these paths - those are reserved for genuinely new files (i.e., paths in expected_new_files that do NOT yet exist). Preserve every line of the original file that is not directly affected by the task: imports, methods, classes, helpers, lifecycle callbacks, navigation routes - keep them character-for-character.

If you find yourself wanting to rewrite the whole file, instead produce hunks that only add/remove the lines you need to change. A 5-line behavior change should be a 5-line diff, not a 100-line file replacement.

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

DO NOT output anything before "diff --git". DO NOT wrap in markdown code fences. DO NOT add explanations.

GROUNDING DISCIPLINE (read carefully — errors here cause harness rejection):

A. Symbol grounding: every identifier you reference (field, method, import) MUST already exist in the source files provided to you, OR be added in the same diff. Do NOT invent field names. If the task says "use the saved address field" and the ViewModel only has `locationAddress`, your diff MUST use `locationAddress` (not `jobLocation`, `address`, or any guess).

B. Line-number grounding: hunk headers `@@ -A,B +C,D @@` MUST match the actual file. Count lines from the source content I provided. A 3-line context above + 3 below + the changes is the minimum. If you are uncertain about the exact line, prefer ADDING new code at the END of an existing method (lines easier to count) over modifying mid-method.

C. Output discipline: ONE diff per response. NO header text like "Here's the diff:" or "===". NO trailing summary. Stop after the last hunk.

GOOD EXAMPLE — minimal edit, 5 lines, preserves everything else:
diff --git a/app/Foo.kt b/app/Foo.kt
--- a/app/Foo.kt
+++ b/app/Foo.kt
@@ -22,6 +22,9 @@ class Foo {
     fun load() {
         val data = repo.fetch()
+        if (viewModel.locationAddress.isBlank()) {
+            viewModel.locationAddress = SessionManager.getHomeAddress(context)
+        }
         render(data)
     }

BAD EXAMPLE — DO NOT DO THIS — full-file replacement:
diff --git a/app/Foo.kt b/app/Foo.kt
new file mode 100644
--- /dev/null
+++ b/app/Foo.kt
@@ -0,0 +1,90 @@
+...90 lines including the entire original file plus your changes...

The BAD example fails L5 gate and wastes the user's time. ALWAYS prefer the GOOD pattern: tiny hunks targeting the specific lines you need to change."""


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


CODEGEN_REACT_PLAN_SYSTEM_PROMPT = """You are a code generation planning agent.

Given a task plan and source file contents, output ONLY the requested JSON symbol plan.
Do not produce a diff, markdown fence, explanation, or any prose."""


CODEGEN_STRUCTURAL_EDIT_SYSTEM_PROMPT = """You are a structural compile-repair agent.

Output ONLY one valid JSON object. No markdown fences, no prose.

Schema:
{
  "status": "repair_patch" | "no_patch",
  "file": "relative/path.kt",
  "edits": [
    {
      "operation": "add_import" | "replace_call_expression" | "replace_block" | "insert_into_function",
      "anchor_line": 123,
      "anchor_substring": "exact text near the diagnostic",
      "content": "replacement or inserted code"
    }
  ],
  "preserves_intents": ["short intent ids or symbol names"]
}

Rules:
1. Do not output a unified diff.
2. Do not output Aider SEARCH/REPLACE blocks.
3. Keep edits local to the broken file and diagnostic area.
4. Use add_import for imports; do not place import statements inside functions.
5. For Kotlin parser/scope failures, repair the nearest block or call expression. Do not rewrite the whole file.
6. Preserve every protected symbol listed by the harness.
"""


CODEGEN_STRUCTURAL_CODEGEN_SYSTEM_PROMPT = """You are a structural Kotlin code generation agent.

Output ONLY one valid JSON object. No markdown fences, no prose.

Schema:
{
  "status": "edit_plan" | "no_patch",
  "file": "relative/path.kt",
  "edits": [
    {
      "operation": "add_import" | "replace_call_expression" | "replace_block" | "insert_into_function" | "insert_after_anchor" | "insert_before_anchor",
      "anchor_line": 123,
      "anchor_substring": "exact text copied from the file",
      "content": "replacement or inserted Kotlin code"
    }
  ],
  "preserves_intents": ["short intent ids or symbol names"]
}

Rules:
1. Do not output a unified diff.
2. Do not output Aider SEARCH/REPLACE blocks.
3. The harness will locate anchors, apply edits, validate structure, and generate the final diff.
4. Use add_import for imports; do not place import statements inside functions.
5. Use exact anchors from the current file. Do not invent line context.
6. Prefer small semantic edits: add imports, add state declarations, insert a UI block, replace a broken call/block.
7. If the requested change cannot be made within the single allowed Kotlin file, output {"status":"no_patch","file":"...","edits":[]}.
"""


# --- Aider search/replace block format (Tier 1.5) ----------------------------
# Mid-tier models (DeepSeek, GPT-4o-mini) consistently miscount unified-diff
# hunk headers and paraphrase context lines. Aider's published benchmark data
# shows search/replace blocks beat unified diff by 15-25 percentage points on
# those same models. The harness converts blocks to a unified diff at the
# boundary so every downstream consumer (sandbox apply, reviewer, SWE-bench
# predictions) keeps working unchanged.
CODEGEN_SYSTEM_PROMPT_AIDER = """You are a code generation agent. Given a task plan and source file contents, produce a sequence of search/replace blocks describing the edit.

CRITICAL RULES:
1. Output ONLY the search/replace blocks below. No prose, no markdown fences, no explanation, no trailing summary.
2. The block format is exact:

filename.py
<<<<<<< SEARCH
exact verbatim source text (every character, every space, every newline)
=======
new text
>>>>>>> REPLACE

3. The SEARCH region MUST be a literal substring of the file as I provided it. Do not paraphrase, reformat, or trim whitespace. The harness will refuse the patch if the SEARCH region does not occur exactly once in the file.

4. To make multiple edits in the same file, emit multiple blocks back-to-back under the same filename header. Edits are applied in the order given.

5. To create a NEW file, emit on the line immediately above the filename header:

### NEW FILE: path/to/file.py
path/to/file.py
<<<<<<< SEARCH
=======
full content of the new file
>>>>>>> REPLACE

6. To DELETE a region, leave the REPLACE side empty:

filename.py
<<<<<<< SEARCH
text to remove
=======
>>>>>>> REPLACE

7. SCOPE: only emit blocks for files in the plan's must_touch_files / expected_new_files. Do not invent edits to other files.

8. MINIMAL EDIT: choose the smallest SEARCH region that uniquely identifies the spot — usually a few lines. Do not paste the whole function unless the function itself is the unit being changed.

9. ANCHOR DISCIPLINE: pick a SEARCH region that occurs ONCE in the file. If a candidate region appears multiple times, extend it (add a line above or below) until it is unique.

10. SYMBOL GROUNDING: every identifier you reference MUST already exist in the file content I provided, OR be added in the same block. Do not invent field names, methods, or imports.

EXAMPLE OUTPUT (your response must look exactly like this; nothing before, nothing after):

app/example.py
<<<<<<< SEARCH
def greet(name):
    return "Hello " + name
=======
def greet(name):
    return f"Hello, {name}!"
>>>>>>> REPLACE

DO NOT output anything before the first filename header. DO NOT wrap in markdown code fences. DO NOT add explanations."""


AIDER_FORMAT_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your output must be Aider search/replace blocks, NOT a "
    "unified diff and NOT JSON. Each edit looks like:\n"
    "filename.py\n"
    "<<<<<<< SEARCH\n"
    "exact verbatim text from the file\n"
    "=======\n"
    "new text\n"
    ">>>>>>> REPLACE\n\n"
    "The SEARCH region must be a verbatim substring of the file content I "
    "provided. If your previous attempt failed with anchor_not_found, you "
    "paraphrased or trimmed whitespace — copy the SEARCH region byte-for-byte "
    "from the file. If it failed with anchor_ambiguous, extend the SEARCH "
    "region by one line until it is unique.\n\n"
    # 2026-05-11 reverse-evidence prompt (codex consult):
    # Curb premature ## EVIDENCE_GAP_REQUEST. Each truncated file's view
    # summary lists which functions are kept WHOLE — those are the only
    # legitimate SEARCH anchors anyway. If the planner picked a stub'd
    # function, the model should re-anchor onto a sibling kept-whole
    # function, not give up.
    "REMINDER about the file context: each truncated file begins with a "
    "`=== view summary ===` header that lists which functions have REAL "
    "bodies (use these as SEARCH anchors) and which are stubbed (body is "
    "`pass`). Pick a SEARCH anchor only from the 'Real bodies' list. Do NOT "
    "emit ## EVIDENCE_GAP_REQUEST when one of the listed real-body functions "
    "is a viable patch site — patch that one. Only request more context if "
    "the fix demonstrably requires a function whose body is stubbed."
)


RAW_DIFF_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Output ONLY the raw unified diff. "
    "Do NOT wrap with === markers, code fences, comments, or "
    "any prose. Start your response with the line "
    "'diff --git a/...' and end at the last hunk line."
)

MINIMAL_EDIT_RETRY_SUFFIX = (
    "\n\nIMPORTANT: For files that already exist (i.e., paths in "
    "must_touch_files), output ONLY a minimal line-anchored unified "
    "diff. Do NOT use 'new file mode 100644' or '--- /dev/null' for "
    "an existing file - those are reserved for genuinely new files. "
    "Preserve every line of the original file that is not directly "
    "affected by the task."
)


class CodegenError(Exception):
    pass


class CodegenEvidenceGapRequest(CodegenError):
    """Raised when the model emits a structured ``## EVIDENCE_GAP_REQUEST``.

    Carries the parsed requests so the harness can fetch the named
    spans from disk and re-run codegen ONCE with the additional
    context appended. This is the Tier 4-H bounded tool-use loop.
    """

    def __init__(self, requests: list, raw_marker: str = "") -> None:  # type: ignore[no-untyped-def]
        super().__init__(
            f"codegen_terminal: EVIDENCE_GAP_REQUEST ({len(requests)} request(s))"
        )
        self.requests = requests
        self.raw_marker = raw_marker


class CodeGenerator:
    def __init__(self, settings: Settings | None = None, *, db: Session | None = None):
        self.settings = settings or get_settings()
        self.db = db
        # Per-call active format ("unified_diff" | "aider_blocks"). Set by
        # _try_provider before each provider invocation so downstream
        # _build_*_prompt + _parse_response can dispatch without threading.
        self._active_codegen_output_format: str = "unified_diff"

    def _react_loop_enabled(self) -> bool:
        return bool(getattr(self.settings, "codegen_react_loop_enabled", False))

    def _resolve_codegen_output_format(self, provider: str) -> str:
        """Resolve the output format ("unified_diff" or "aider_blocks") for a
        codegen call. Settings can pin a value; "auto" defaults to
        aider_blocks for mid-tier API providers (deepseek, openai) and
        unified_diff elsewhere. Providers using JSON mode (minimax, ollama)
        and CLI providers (claude_code, codex) stay on their existing path.
        """
        configured = getattr(self.settings, "codegen_output_format", "auto")
        if configured in {"unified_diff", "aider_blocks"}:
            return configured
        if provider in {"deepseek", "openai"}:
            return "aider_blocks"
        return "unified_diff"

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
        # Phase 2.4 (2026-05-11): likely_touch_files are allowed-to-edit
        # candidates the planner surfaced but isn't sure about. Including
        # them in allowed_paths means the codegen LLM is free to upgrade
        # them to actual edits when in-context evidence justifies it.
        # must_inspect_files are NOT in this set — they remain read-only
        # context and codegen will be rejected if it modifies them.
        likely_touch = clean(plan_json.get("likely_touch_files"))
        return (
            must_touch,
            expected_new,
            set(must_touch) | set(expected_new) | set(likely_touch),
        )

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

    def _library_hints_block(self) -> str:
        """Return rendered REPO LIBRARY HINTS for the current task's repo.

        Cached per CodeGenerator instance keyed on source_repo_path so a
        single task doesn't re-scan the repo for each provider attempt.
        Returns "" when the path can't be resolved or no hints fire.
        """
        repo_path = getattr(self, "_current_source_repo_path", None)
        if not repo_path:
            return ""
        cache = getattr(self, "_library_hints_cache", None)
        if cache is None:
            cache = {}
            self._library_hints_cache = cache
        if repo_path in cache:
            return cache[repo_path]
        try:
            from app.services.repo_library_fingerprint import (
                fingerprint_repository,
                render_library_hints_block,
            )
            hints = fingerprint_repository(Path(repo_path))
            block = render_library_hints_block(hints)
        except Exception:  # noqa: BLE001
            block = ""
        cache[repo_path] = block
        return block

    def _build_system_prompt(
        self,
        base_prompt: str,
        context_files: dict[str, str] | None,
    ) -> str:
        """Compose the full codegen system prompt.

        Layers (in order, most-context-bearing last):
          1. Codegen playbooks (Tier 1.1) — language- and file-glob-
             keyed structural rules (Python edit rules, diff
             discipline, etc.). Playbooks land first so they're closest
             to the system role anchor and least likely to be
             attention-faded by long context.
          2. Library hints block (Leg 1: pin OSMDroid vs Google Maps etc.)
          3. The base prompt (CODEGEN_SYSTEM_PROMPT or variant). When the
             active output format is ``aider_blocks``, the unified-diff
             base prompt is swapped for ``CODEGEN_SYSTEM_PROMPT_AIDER``.
             JSON-mode prompts pass through unchanged.
          4. Kotlin / Compose / multi-file augmentations from
             ``_augment_prompt_for_kotlin``
        """
        effective_base = self._select_base_prompt(base_prompt)
        playbook_block = self._codegen_playbooks_block(context_files)
        hints = self._library_hints_block()
        kotlin_augmented = self._augment_prompt_for_kotlin(effective_base, context_files)
        layers: list[str] = []
        if playbook_block:
            layers.append(playbook_block)
        if hints:
            layers.append(hints)
        layers.append(kotlin_augmented)
        return "\n\n".join(layers)

    def _select_base_prompt(self, base_prompt: str) -> str:
        """Swap the unified-diff base prompt for the Aider variant when
        the active format calls for it. Other base prompts (JSON mode,
        ReAct plan) pass through unchanged.
        """
        if self._active_codegen_output_format != "aider_blocks":
            return base_prompt
        if base_prompt is CODEGEN_SYSTEM_PROMPT:
            return CODEGEN_SYSTEM_PROMPT_AIDER
        return base_prompt

    def _codegen_playbooks_block(
        self, context_files: dict[str, str] | None
    ) -> str:
        """Pull codegen playbooks relevant to the active task.

        Best-effort — never let a playbook lookup error abort codegen.
        Language is inferred from the first file's extension when
        present; otherwise falls back to "any" so only high-priority
        global playbooks are included.
        """
        try:
            from app.services.codegen_playbooks import (
                rebuild_index,
                select_playbooks,
                render_for_prompt,
            )

            # The index is process-wide; rebuild once on first call so
            # subsequent codegen invocations reuse it. We accept the
            # rebuild cost (a few hundred μs) to avoid coupling to a
            # specific startup hook in this commit.
            rebuild_index()
            language = "any"
            file_paths: list[str] = []
            if context_files:
                file_paths = list(context_files.keys())
                first_ext = Path(file_paths[0]).suffix.lower() if file_paths else ""
                language = {
                    ".py": "python",
                    ".kt": "kotlin",
                    ".java": "java",
                    ".ts": "typescript",
                    ".tsx": "typescript",
                    ".js": "javascript",
                    ".jsx": "javascript",
                    ".go": "go",
                    ".rs": "rust",
                }.get(first_ext, "any")
            selected = select_playbooks(language=language, file_paths=file_paths)
            if not selected:
                return ""
            rendered = render_for_prompt(selected)
            return f"### Codegen playbooks\n\n{rendered}"
        except Exception:  # noqa: BLE001
            return ""

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

    @staticmethod
    def _validate_diff_paths_within_allowed(
        diff: str,
        files_changed: list[str],
        *,
        allowed_paths: set[str],
        must_touch_files: list[str],
        expected_new_files: list[str],
    ) -> None:
        """Validate the executable diff, not only reported metadata."""
        CodeGenerator._validate_changed_files_within_allowed(
            files_changed,
            allowed_paths=allowed_paths,
            must_touch_files=must_touch_files,
            expected_new_files=expected_new_files,
        )
        if not str(diff or "").strip():
            return
        try:
            from app.services.spec_conformance import _classify_files_in_diff

            file_shapes = _classify_files_in_diff(diff)
        except Exception:  # noqa: BLE001
            file_shapes = {}
        if not file_shapes:
            return

        diff_paths = {
            path.strip()
            for path in file_shapes
            if isinstance(path, str) and path.strip()
        }
        extra = sorted(
            path
            for path in diff_paths
            if not any(
                CodeGenerator._paths_match(path, allowed)
                for allowed in allowed_paths
            )
        )
        if extra:
            raise CodegenError(
                "file_outside_allowed_set: diff contains files not in plan: "
                f"{extra}. Allowed must_touch_files={sorted(must_touch_files)}, "
                f"expected_new_files={sorted(expected_new_files)}"
            )

        created_files = [
            path for path, shape in file_shapes.items()
            if shape == "create"
        ]
        unplanned_created = sorted(
            path
            for path in created_files
            if not any(
                CodeGenerator._paths_match(path, expected)
                for expected in expected_new_files
            )
        )
        if unplanned_created:
            raise CodegenError(
                "unplanned_new_file: diff creates file(s) not declared in "
                f"expected_new_files: {unplanned_created}. New files must be "
                "source-bound by the planner before codegen may create them."
            )

        must_touch_created = sorted(
            path
            for path in created_files
            if any(
                CodeGenerator._paths_match(path, must_touch)
                for must_touch in must_touch_files
            )
        )
        if must_touch_created:
            raise CodegenError(
                "must_touch_recreated_as_new_file: existing must_touch file(s) "
                f"were emitted as new files: {must_touch_created}."
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
        self._current_source_repo_path = source_repo_path
        # Tier 4-H: track whether the bounded tool-use loop already fired
        # for this generate_patch call. Reset here (instance is reused
        # across calls).
        self._tool_loop_count = 0

        must_touch_files, expected_new_files, allowed_paths = self._extract_plan_target_paths(plan_json)
        enforce = bool(allowed_paths)

        recipe_result = self._try_android_map_location_recipe_codegen(
            plan_json=plan_json,
            context_files=context_files,
            task_description=task_description,
            source_repo_path=source_repo_path,
            must_touch_files=must_touch_files,
            expected_new_files=expected_new_files,
        )
        if recipe_result is not None:
            if enforce:
                self._validate_diff_paths_within_allowed(
                    recipe_result.diff,
                    recipe_result.files_changed,
                    allowed_paths=allowed_paths,
                    must_touch_files=must_touch_files,
                    expected_new_files=expected_new_files,
                )
            return recipe_result

        # Tier 4 main course: when agent mode = loop, run the multi-turn
        # agent loop instead of the static 1-shot pipeline. Decision
        # 2026-05-10: static stays default until loop validates ≥ static
        # quality on the 4-task regression baseline.
        if getattr(self.settings, "codegen_agent_mode", "static") == "loop":
            result = self._run_agent_loop(
                task_id=task_id,
                plan_json=plan_json,
                context_files=context_files,
                task_description=task_description,
                source_repo_path=source_repo_path,
                actor_name=actor_name,
            )
            if enforce:
                self._validate_diff_paths_within_allowed(
                    result.diff,
                    result.files_changed,
                    allowed_paths=allowed_paths,
                    must_touch_files=must_touch_files,
                    expected_new_files=expected_new_files,
                )
            return result

        providers = self._resolve_provider_chain()

        import logging as _log
        _logger = _log.getLogger("codegen.provider_chain")
        _logger.info("Provider chain: %s", providers)

        attempts: list[dict[str, Any]] = []
        for provider_idx, provider in enumerate(providers):
            _logger.info("Trying provider %d/%d: %s", provider_idx + 1, len(providers), provider)
            _provider_t0 = time.monotonic()
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
                _logger.info(
                    "Provider %s primary call returned in %.1fs",
                    provider, time.monotonic() - _provider_t0,
                )
                if enforce:
                    self._validate_diff_paths_within_allowed(
                        result.diff,
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
                #
                # M1 fix (2026-05-11): the marker "Fix syntax errors in"
                # is stored in plan_json.objective (orchestrator service
                # line 7712), not task_description. The previous guard
                # checked the wrong field, so self_validate ran during
                # every repair attempt and falsely rejected legit
                # sandbox-based patches against the original repo. v9
                # spent 28 min stuck in this loop before the 30-min
                # watchdog killed the task. Check both fields against
                # a list of repair markers.
                _repair_markers = (
                    "Fix syntax errors in",
                    "Fix compile errors in",
                    "Repair patch for",
                    "Compile repair",
                )
                _plan_objective = ""
                if isinstance(plan_json, dict):
                    _plan_objective = str(plan_json.get("objective") or "")
                _task_desc_str = task_description if isinstance(task_description, str) else ""
                _is_repair_call = any(
                    _plan_objective.startswith(m) or _task_desc_str.startswith(m)
                    for m in _repair_markers
                )
                if not _is_repair_call and getattr(self.settings, "codegen_self_validation_enabled", True):
                    from app.services.codegen_self_validate import self_validate
                    raw_source = str(
                        source_repo_path
                        or getattr(self.settings, "knowledge_source_path", "")
                        or ""
                    ).strip()
                    source_path = Path(raw_source) if raw_source else None
                    # Skip when source_path is unset / non-existent / not a
                    # real source repo. Test fixtures often leave this unset
                    # and validation against cwd would surface false failures.
                    if source_path is not None and source_path.is_absolute() and source_path.is_dir() and (source_path / ".git").exists():
                        max_retries = int(getattr(self.settings, "codegen_self_validation_max_retries", 1))
                        for sv_attempt in range(max_retries + 1):
                            validation = self_validate(
                                result.diff,
                                source_path,
                                must_touch_files=must_touch_files,
                            )
                            if validation.valid:
                                break
                            l5_failure = _is_minimal_edit_retryable_error_message(
                                f"{validation.reason} {validation.error_detail}"
                            )
                            if sv_attempt >= max_retries:
                                # Dump rejected diff for debugging — without
                                # this, you can't see what DeepSeek actually
                                # emitted; only the error code reaches the
                                # operator. Saved to the task workspace so
                                # it shows up in event timeline + workspace
                                # browser.
                                try:
                                    from datetime import datetime, timezone
                                    workspace_root = Path(
                                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                                    ) / "data" / "agent_workspace" / str(task_id) / "attempts" / "rejected"
                                    workspace_root.mkdir(parents=True, exist_ok=True)
                                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                    rej_path = workspace_root / f"rejected_{ts}_{validation.reason[:30].replace(' ', '_').replace(':', '')}.patch"
                                    rej_path.write_text(
                                        f"# REJECTED by self_validate after {sv_attempt + 1} attempts\n"
                                        f"# reason: {validation.reason}\n"
                                        f"# detail: {validation.error_detail[:1000]}\n"
                                        f"# provider: {result.provider_name}, model: {result.model_name}\n"
                                        f"# ---- raw diff begins ----\n"
                                        + (result.diff or "<empty>"),
                                        encoding="utf-8",
                                    )
                                except Exception as _dump_exc:  # noqa: BLE001
                                    _logger.warning(
                                        "rejected_diff_dump_failed",
                                        extra={"err": str(_dump_exc)[:200]},
                                    )
                                if l5_failure:
                                    raise CodegenError(
                                        f"{validation.reason} :: "
                                        f"{validation.error_detail[:500]}"
                                    )
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
                            # v16 P0-3: classify the apply error and inject a
                            # TARGETED hint instead of generic "hunk drift". The
                            # most common silent-killer is `MISSING_NEW_FILE` —
                            # model emitted `--- a/<new-file>` for a path in
                            # expected_new_files, git rejects with "No such file
                            # or directory", and the generic retry just says
                            # "context mismatch" so the model has no idea what
                            # actually needs to change.
                            targeted_hint = ""
                            kind = getattr(validation, "error_kind", "UNKNOWN")
                            if kind == "MISSING_NEW_FILE":
                                new_files_list = "\n".join(
                                    f"  - {p}" for p in (expected_new_files or [])
                                ) or "  (none declared)"
                                targeted_hint = (
                                    "\n---\n"
                                    "TARGETED FIX: at least one path in this diff "
                                    "does not exist in the source tree. git apply "
                                    "rejected with 'No such file or directory'. "
                                    "This file is a NEW FILE — you MUST emit the "
                                    "new-file diff shape:\n"
                                    "    diff --git a/<path> b/<path>\n"
                                    "    new file mode 100644\n"
                                    "    --- /dev/null\n"
                                    "    +++ b/<path>\n"
                                    "    @@ -0,0 +1,N @@\n"
                                    "    +<content>\n"
                                    "Do NOT use '--- a/<path>' — that header is "
                                    "only for files that already exist on disk.\n"
                                    f"Plan's expected_new_files:\n{new_files_list}\n"
                                )
                            retry_prompt = (
                                f"{sv_prompt}\n\n"
                                f"---\n"
                                f"VALIDATION FEEDBACK (your previous attempt failed):\n"
                                f"{validation.reason}\n"
                                f"{validation.error_detail[:1500]}\n"
                                f"{targeted_hint}\n"
                                f"Regenerate the diff. Make sure the hunk context "
                                f"matches the actual file content (no drift). If "
                                f"parse failed, fix the syntactic error."
                            )
                            if l5_failure:
                                retry_prompt += MINIMAL_EDIT_RETRY_SUFFIX
                            _logger.info(
                                "Self-validation retry %d/%d for provider %s (error_kind=%s)",
                                sv_attempt + 1, max_retries, provider, kind,
                            )
                            _sv_retry_t0 = time.monotonic()
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
                            _logger.info(
                                "Retry call returned in %.1fs (provider=%s, error_kind=%s, retry=%d/%d)",
                                time.monotonic() - _sv_retry_t0,
                                provider, kind, sv_attempt + 1, max_retries,
                            )
                            if enforce:
                                self._validate_diff_paths_within_allowed(
                                    result.diff,
                                    result.files_changed,
                                    allowed_paths=allowed_paths,
                                    must_touch_files=must_touch_files,
                                    expected_new_files=expected_new_files,
                                )

                _provider_total_s = round(time.monotonic() - _provider_t0, 1)
                _logger.info(
                    "Provider %s succeeded: %d files changed in %.1fs",
                    provider, len(result.files_changed), _provider_total_s,
                )
                attempts.append({
                    "provider": provider,
                    "status": "succeeded",
                    "duration_s": _provider_total_s,
                })
                try:
                    result.attempt_history = attempts
                except Exception:
                    result = result.model_copy(update={"attempt_history": attempts})
                return result
            except CodegenError as exc:
                _provider_total_s = round(time.monotonic() - _provider_t0, 1)
                _logger.warning(
                    "Provider %s failed after %.1fs: %s",
                    provider, _provider_total_s, str(exc)[:300],
                )
                # Extract error_kind from the CodegenError if it embedded one
                # via self-validate (format: "<reason> :: <detail>" or
                # "codegen self-validation failed after N attempt(s): <reason>: <detail>").
                # Best-effort tag for observability + downstream fail-fast decisions.
                _err_msg = str(exc)
                _error_kind = "UNKNOWN"
                if "No such file or directory" in _err_msg:
                    _error_kind = "MISSING_NEW_FILE"
                elif "corrupt patch" in _err_msg.lower():
                    _error_kind = "CORRUPT_PATCH"
                elif "patch does not apply" in _err_msg.lower() or "hunk drift" in _err_msg.lower():
                    _error_kind = "HUNK_DRIFT"
                attempts.append({
                    "provider": provider,
                    "status": "failed",
                    "error": str(exc)[:300],
                    "error_kind": _error_kind,
                    "duration_s": _provider_total_s,
                })
                if _is_provider_level_error(exc) and provider_idx < len(providers) - 1:
                    _logger.info("Classified as provider-level error — trying next provider")
                    continue
                raise

        raise CodegenError("No codegen provider available.")

    def _try_android_map_location_recipe_codegen(
        self,
        *,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        source_repo_path: str | None,
        must_touch_files: list[str],
        expected_new_files: list[str],
    ) -> CodegenResult | None:
        if expected_new_files:
            return None
        if not context_files:
            return None
        if self._is_repair_codegen_call(plan_json, task_description):
            return None

        from app.services.android_map_location_recipe import (
            try_generate_android_map_location_recipe,
        )

        must_touch_norm = {
            path.replace("\\", "/")
            for path in (must_touch_files or [])
            if str(path).strip()
        }
        targets: list[tuple[str, str]] = []
        for path, content in context_files.items():
            normalized = path.replace("\\", "/")
            if must_touch_norm and normalized not in must_touch_norm:
                continue
            targets.append((path, content))
        if not targets:
            return None

        started = time.perf_counter()
        recipe_results = []
        for target_path, prompt_content in targets:
            original_content = self._load_structural_codegen_source(
                relative_path=target_path,
                fallback_content=prompt_content,
                source_repo_path=source_repo_path,
            )
            recipe_result = try_generate_android_map_location_recipe(
                file_path=target_path,
                original_content=original_content,
                plan_json=plan_json,
                task_description=task_description,
            )
            if recipe_result is None:
                return None
            recipe_results.append(recipe_result)

        diffs = [result.diff.rstrip() for result in recipe_results if result.diff.strip()]
        if not diffs:
            return None

        files_changed = [
            file_path
            for result in recipe_results
            for file_path in result.files_changed
        ]
        operations = [
            operation
            for result in recipe_results
            for operation in result.applied_operations
        ]
        latency_s = round(time.perf_counter() - started, 3)
        logger.info(
            "android_map_location_recipe_applied files=%s latency_s=%.3f operations=%s",
            files_changed,
            latency_s,
            operations[:12],
        )

        return CodegenResult(
            diff="\n".join(diffs).strip() + "\n",
            summary=(
                "Generated Android map/location patch via deterministic "
                "harness recipe"
            ),
            files_changed=files_changed,
            file_summaries=[
                {
                    "path": file_path,
                    "summary": "Applied Android map/location harness recipe",
                }
                for file_path in files_changed
            ],
            attempt_history=[
                {
                    "provider": "harness:android_map_location_recipe",
                    "status": "succeeded",
                    "duration_s": latency_s,
                }
            ],
            provider_name="harness:android_map_location_recipe",
            model_name="deterministic-v1",
            input_tokens=0,
            output_tokens=0,
            contract_coverage=self._merge_codegen_contract_coverage(
                [
                    result.contract_coverage
                    for result in recipe_results
                    if isinstance(result.contract_coverage, dict)
                ]
            ),
        )

    @staticmethod
    def _merge_codegen_contract_coverage(
        coverages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not coverages:
            return None
        merged: dict[str, Any] = {
            "implemented_contracts": [],
            "verified_no_change_contracts": [],
            "unimplemented_contracts": [],
        }
        for coverage in coverages:
            for key in (
                "implemented_contracts",
                "verified_no_change_contracts",
                "unimplemented_contracts",
            ):
                rows = coverage.get(key)
                if isinstance(rows, list):
                    merged[key].extend(rows)
        return merged

    def _should_try_structural_kotlin_codegen(
        self,
        *,
        provider: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        source_repo_path: str | None,
        override_prompt: str | None,
    ) -> bool:
        if not bool(getattr(self.settings, "codegen_structural_kotlin_enabled", True)):
            return False
        if override_prompt is not None:
            return False
        if provider not in {"deepseek", "openai"}:
            return False
        if not context_files or len(context_files) != 1:
            return False
        if self._is_repair_codegen_call(plan_json, task_description):
            return False

        path, content = next(iter(context_files.items()))
        if not content.strip():
            return False
        if Path(path).suffix.lower() not in {".kt", ".kts"}:
            return False

        _must_touch, expected_new_files, _allowed_paths = self._extract_plan_target_paths(
            plan_json
        )
        if expected_new_files:
            return False

        # If a real repo root is provided and the file is absent there, this
        # is effectively a create-file task. V1 keeps create-file JSON on the
        # existing full-file/Aider path.
        if source_repo_path:
            try:
                disk_file = Path(source_repo_path) / path
                if not disk_file.is_file():
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _is_repair_codegen_call(
        plan_json: dict[str, Any],
        task_description: str,
    ) -> bool:
        markers = (
            "Fix syntax errors in",
            "Fix compile errors in",
            "Repair patch for",
            "Compile repair",
        )
        objective = ""
        if isinstance(plan_json, dict):
            objective = str(plan_json.get("objective") or "")
        description = task_description if isinstance(task_description, str) else ""
        return any(
            objective.startswith(marker) or description.startswith(marker)
            for marker in markers
        )

    def _try_structural_kotlin_codegen(
        self,
        *,
        provider: str,
        prompt: str,
        context_files: dict[str, str],
        source_repo_path: str | None,
    ) -> CodegenResult:
        from app.services.structural_edit import (
            apply_structural_edit_plan,
            parse_structural_edit_response,
        )

        target_path, prompt_content = next(iter(context_files.items()))
        if provider == "deepseek":
            model_name = self.settings.deepseek_model
            raw, input_tokens, output_tokens = self._call_deepseek_text(
                prompt,
                model_name,
                system_prompt=CODEGEN_STRUCTURAL_CODEGEN_SYSTEM_PROMPT,
                purpose="codegen.structural_kotlin",
            )
        elif provider == "openai":
            model_name = self._resolve_model_name("openai")
            raw, input_tokens, output_tokens = self._call_openai_text(
                prompt,
                model_name,
                system_prompt=CODEGEN_STRUCTURAL_CODEGEN_SYSTEM_PROMPT,
                purpose="codegen.structural_kotlin",
            )
        else:
            raise CodegenError(f"structural Kotlin codegen unsupported provider: {provider}")

        try:
            edit_plan = parse_structural_edit_response(raw)
        except Exception as exc:  # noqa: BLE001
            raise CodegenError(f"structural Kotlin JSON parse failed: {exc}") from exc

        status = str(edit_plan.get("status") or "").strip().lower()
        if status in {"no_patch", "no_change", "noop"}:
            raise CodegenError("structural Kotlin response returned no_patch")
        edits = edit_plan.get("edits") or []
        if not isinstance(edits, list) or not edits:
            raise CodegenError("structural Kotlin response had no edits")

        plan_file = str(edit_plan.get("file") or target_path).strip()
        if not self._paths_match(plan_file.replace("\\", "/"), target_path.replace("\\", "/")):
            raise CodegenError(
                f"structural Kotlin plan targets {plan_file!r}, expected {target_path!r}"
            )
        normalized_plan = dict(edit_plan)
        normalized_plan["file"] = target_path

        original_content = self._load_structural_codegen_source(
            relative_path=target_path,
            fallback_content=prompt_content,
            source_repo_path=source_repo_path,
        )
        applied = apply_structural_edit_plan(
            file_path=target_path,
            original_content=original_content,
            plan=normalized_plan,
        )
        if not applied.ok:
            reasons = "; ".join(
                f"{err.operation}:{err.reason}" if err.operation else err.reason
                for err in applied.errors
            )
            raise CodegenError(f"structural Kotlin apply failed: {reasons}")

        coverage_payload = None
        raw_coverage = edit_plan.get("contract_coverage")
        if isinstance(raw_coverage, dict):
            coverage_payload = raw_coverage

        operations = ", ".join(applied.applied_operations[:8]) or "structured edits"
        return CodegenResult(
            diff=applied.diff,
            summary=(
                "Generated structural Kotlin patch via harness-applied JSON "
                f"({operations})"
            ),
            files_changed=[target_path],
            file_summaries=[
                {
                    "path": target_path,
                    "summary": (
                        "Applied structural Kotlin edit plan and generated diff "
                        "inside the harness"
                    ),
                }
            ],
            provider_name=f"{provider}:structural_kotlin",
            model_name=model_name,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            contract_coverage=coverage_payload,
        )

    def _load_structural_codegen_source(
        self,
        *,
        relative_path: str,
        fallback_content: str,
        source_repo_path: str | None,
    ) -> str:
        if source_repo_path:
            try:
                disk_file = Path(source_repo_path) / relative_path
                if disk_file.is_file():
                    return disk_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return fallback_content

    def _build_structural_codegen_prompt(
        self,
        *,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
    ) -> str:
        target_path, target_content = next(iter(context_files.items()))
        objective = str(plan_json.get("objective") or "").strip()
        change_summary = str(plan_json.get("change_summary") or "").strip()
        change_explanation = str(plan_json.get("change_explanation") or "").strip()

        parts = [
            "<task>",
            "Generate a structural JSON edit plan for the single Kotlin file.",
            "The model decides WHAT code should change. The harness decides WHERE edits apply and generates the diff.",
            "</task>",
            "",
            "<output_contract>",
            "Return only JSON with status, file, edits, and preserves_intents.",
            "Do not return unified diff, Aider blocks, markdown, or prose.",
            "Allowed operations: add_import, replace_call_expression, replace_block, insert_into_function, insert_after_anchor, insert_before_anchor.",
            "Every anchor_substring must be copied exactly from the file context and must be unique or line-pinned.",
            "</output_contract>",
            "",
            "<allowed_file>",
            target_path,
            "</allowed_file>",
        ]

        if objective or change_summary or change_explanation:
            parts.extend(["", "<objective>"])
            if objective:
                parts.append(objective)
            if change_summary:
                parts.append(f"Summary: {change_summary}")
            if change_explanation:
                parts.append(f"Details: {change_explanation}")
            parts.append("</objective>")

        if task_description.strip():
            parts.extend(["", "<task_description>", task_description.strip(), "</task_description>"])

        constraints = plan_json.get("constraints") or []
        if isinstance(constraints, list) and constraints:
            parts.extend(["", "<constraints>"])
            for item in constraints[:10]:
                if isinstance(item, str) and item.strip():
                    parts.append(f"- {item.strip()}")
            parts.append("</constraints>")

        steps = plan_json.get("steps") or []
        if isinstance(steps, list) and steps:
            parts.extend(["", "<plan_steps>"])
            for step in steps[:12]:
                if not isinstance(step, dict):
                    continue
                title = str(step.get("title") or "").strip()
                expected = str(step.get("expected_output") or "").strip()
                if title or expected:
                    parts.append(f"- {title}: {expected}".strip())
            parts.append("</plan_steps>")

        memory_context = str(plan_json.get("memory_context") or "").strip()
        if memory_context:
            max_lines = max(1, int(getattr(self.settings, "memory_max_lines_in_prompt", 30) or 30))
            parts.extend(
                [
                    "",
                    "<prior_failure_memory>",
                    MEMORY_PROMPT_INSTRUCTION,
                    *memory_context.splitlines()[:max_lines],
                    "</prior_failure_memory>",
                ]
            )

        acceptance_tests = plan_json.get("acceptance_tests") or []
        if isinstance(acceptance_tests, list):
            scoped_tests: list[dict[str, Any]] = []
            for test in acceptance_tests:
                if not isinstance(test, dict):
                    continue
                test_file = str(test.get("file") or test.get("target") or "").strip()
                if not test_file or self._paths_match(test_file, target_path):
                    scoped_tests.append(test)
            if scoped_tests:
                parts.extend(["", "<hard_acceptance_requirements>"])
                for idx, test in enumerate(scoped_tests[:10], start=1):
                    kind = str(test.get("kind") or "").strip()
                    pattern = str(test.get("pattern") or "").strip()
                    rationale = str(test.get("rationale") or "").strip()
                    line = f"{idx}. "
                    if kind:
                        line += f"[{kind}] "
                    if pattern:
                        line += f"required pattern: {pattern}"
                    if rationale:
                        line += f" | why: {rationale[:220]}"
                    parts.append(line.rstrip())
                parts.append("</hard_acceptance_requirements>")

        required_contracts = plan_json.get("required_contracts") or []
        if isinstance(required_contracts, list) and required_contracts:
            parts.extend(["", "<required_contract_signals>"])
            for contract in required_contracts[:8]:
                if not isinstance(contract, dict):
                    continue
                cid = str(contract.get("contract_id") or contract.get("id") or "").strip()
                signal = str(contract.get("signal") or "").strip().replace("\n", " ")
                if cid or signal:
                    parts.append(f"- {cid}: {signal[:260]}".strip())
                for pattern in self._iter_contract_patterns(contract)[:4]:
                    parts.append(f"  pattern: {pattern}")
            parts.append("</required_contract_signals>")

        parts.extend(
            [
                "",
                "<file_context>",
                f"--- BEGIN FILE {target_path} ---",
                target_content,
                f"--- END FILE {target_path} ---",
                "</file_context>",
            ]
        )
        return "\n".join(parts)

    def generate_structural_edit(
        self,
        *,
        task_id: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        source_repo_path: str | None = None,
        actor_name: str | None = None,
    ) -> dict[str, Any]:
        """Generate diagnostic-scoped structural edit JSON.

        Normal codegen returns a diff.  This path is reserved for compile
        repair, where the harness needs the model to propose constrained edit
        operations and the harness applies them to the broken sandbox file.
        """
        from app.services.structural_edit import parse_structural_edit_response

        self._current_context_files = dict(context_files or {})
        self._current_source_repo_path = source_repo_path
        prompt = self._build_structural_edit_prompt(
            plan_json=plan_json,
            context_files=context_files,
            task_description=task_description,
        )
        providers = [
            provider
            for provider in self._resolve_provider_chain()
            if provider in {"deepseek", "openai", "anthropic", "mock"}
        ] or ["mock"]
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
        errors: list[str] = []

        for fallback_step, provider in enumerate(providers):
            started = time.perf_counter()
            try:
                if provider == "mock":
                    first_file = next(iter(context_files.keys()), "")
                    raw = json.dumps({"status": "no_patch", "file": first_file, "edits": []})
                    model_name = "mock"
                    input_tokens = output_tokens = 0
                elif provider == "deepseek":
                    model_name = self.settings.deepseek_model
                    raw, input_tokens, output_tokens = self._call_deepseek_text(
                        prompt,
                        model_name,
                        system_prompt=CODEGEN_STRUCTURAL_EDIT_SYSTEM_PROMPT,
                        purpose="codegen.structural_repair",
                    )
                elif provider == "openai":
                    model_name = self._resolve_model_name("openai")
                    raw, input_tokens, output_tokens = self._call_openai_text(
                        prompt,
                        model_name,
                        system_prompt=CODEGEN_STRUCTURAL_EDIT_SYSTEM_PROMPT,
                        purpose="codegen.structural_repair",
                    )
                elif provider == "anthropic":
                    model_name = self.settings.anthropic_model
                    raw, input_tokens, output_tokens = self._call_anthropic_text(
                        prompt,
                        model_name,
                        system_prompt=CODEGEN_STRUCTURAL_EDIT_SYSTEM_PROMPT,
                    )
                else:
                    continue
                parsed = parse_structural_edit_response(raw)
                if self.db is not None:
                    record_llm_call(
                        self.db,
                        LlmCall(
                            purpose="codegen.structural_repair",
                            provider=provider,
                            model=model_name,
                            input_tokens=int(input_tokens or 0),
                            output_tokens=int(output_tokens or 0),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            success=True,
                            retry_count=0,
                            fallback_step=fallback_step,
                            prompt_fingerprint=prompt_fingerprint,
                            task_id=task_id,
                            actor_name=actor_name,
                        ),
                    )
                return {
                    "status": "completed",
                    "provider_name": provider,
                    "model_name": model_name,
                    "edit_plan": parsed,
                    "raw_response": raw[:4000],
                }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider}: {exc}")
                if self.db is not None:
                    self._record_codegen_failure(
                        task_id=task_id,
                        actor_name=actor_name,
                        provider=provider,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        retry_count=0,
                        fallback_step=fallback_step,
                        prompt_fingerprint=prompt_fingerprint,
                        error_type=type(exc).__name__,
                    )
                continue

        raise CodegenError("structural edit generation failed: " + "; ".join(errors))

    def _build_structural_edit_prompt(
        self,
        *,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
    ) -> str:
        parts = [
            "Generate a diagnostic-scoped structural edit JSON plan.",
            "Do not generate a diff. The harness validates and applies the JSON.",
            "",
            "=== PLAN JSON ===",
            json.dumps(plan_json, ensure_ascii=False, default=str)[:4000],
        ]
        if task_description.strip():
            parts.extend(["", "=== REPAIR TASK ===", task_description.strip()])
        parts.extend(["", "=== CURRENT FILE CONTEXT ==="])
        for path, content in (context_files or {}).items():
            parts.extend([
                f"--- BEGIN FILE {path} ---",
                content,
                f"--- END FILE {path} ---",
            ])
        return "\n".join(parts)

    @staticmethod
    def _iter_contract_patterns(contract: dict[str, Any]) -> list[str]:
        patterns: list[str] = []
        for pattern in contract.get("verification_patterns") or []:
            if isinstance(pattern, str) and pattern.strip():
                patterns.append(pattern.strip())

        def _walk_rules(rules: object) -> None:
            if not isinstance(rules, list):
                return
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                pattern = rule.get("pattern")
                if isinstance(pattern, str) and pattern.strip():
                    patterns.append(pattern.strip())
                _walk_rules(rule.get("rules"))

        _walk_rules(contract.get("verifications"))
        out: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            if pattern not in seen:
                seen.add(pattern)
                out.append(pattern)
        return out

    @staticmethod
    def _find_pattern_quote(pattern: str, content: str) -> str:
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        for line in content.splitlines():
            if regex.search(line):
                return line.strip()[:180]
        return ""

    @classmethod
    def _existing_contract_evidence_block(
        cls,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
    ) -> str:
        """Render a prompt hint when required contract signals already exist."""
        required_contracts = plan_json.get("required_contracts") or []
        if not isinstance(required_contracts, list) or not required_contracts:
            return ""
        if not context_files:
            return ""

        hits: list[str] = []
        seen_contracts: set[tuple[str, str]] = set()
        for contract in required_contracts:
            if not isinstance(contract, dict):
                continue
            cid = str(contract.get("contract_id") or contract.get("id") or "").strip()
            if not cid:
                continue
            for pattern in cls._iter_contract_patterns(contract):
                for path, content in context_files.items():
                    if not isinstance(content, str) or not content.strip():
                        continue
                    quote = cls._find_pattern_quote(pattern, content)
                    if not quote:
                        continue
                    key = (cid, path)
                    if key in seen_contracts:
                        continue
                    seen_contracts.add(key)
                    hits.append(
                        f"  - {cid} in {path}: pattern `{pattern}` matched `{quote}`"
                    )
                    break
                if len(hits) >= 8:
                    break
            if len(hits) >= 8:
                break

        if not hits:
            return ""
        return "\n".join(
            [
                "=== EXISTING CONTRACT EVIDENCE (minimal-patch mode) ===",
                "These required contract signals already appear in this "
                "batch's file context. Treat them as existing implementation "
                "anchors: preserve them and make minimal corrective edits "
                "around missing wiring. Do not rebuild or rewrite a file just "
                "to re-add signals that are already present.",
                *hits,
            ]
        )

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

        # Resolve and pin the active output format for this provider call.
        # Downstream prompt builders, system-prompt selection, and parsing
        # all read this attribute. JSON-mode providers ignore it.
        self._active_codegen_output_format = self._resolve_codegen_output_format(provider)

        if self._should_try_structural_kotlin_codegen(
            provider=provider,
            plan_json=plan_json,
            context_files=context_files,
            task_description=task_description,
            source_repo_path=source_repo_path,
            override_prompt=override_prompt,
        ):
            structural_prompt = self._build_structural_codegen_prompt(
                plan_json=plan_json,
                context_files=context_files,
                task_description=task_description,
            )
            structural_fingerprint = hashlib.sha256(
                structural_prompt.encode("utf-8")
            ).hexdigest()[:10]
            structural_started = time.perf_counter()
            try:
                result = self._try_structural_kotlin_codegen(
                    provider=provider,
                    prompt=structural_prompt,
                    context_files=context_files,
                    source_repo_path=source_repo_path,
                )
                self._record_codegen_call(
                    task_id=task_id,
                    actor_name=actor_name,
                    result=result,
                    latency_ms=int((time.perf_counter() - structural_started) * 1000),
                    success=True,
                    retry_count=0,
                    fallback_step=fallback_step,
                    prompt_fingerprint=structural_fingerprint,
                )
                return result
            except CodegenError as exc:
                logger.warning(
                    "structural_kotlin_codegen_fallback provider=%s error=%s",
                    provider,
                    str(exc)[:240],
                )
                self._record_codegen_failure(
                    task_id=task_id,
                    actor_name=actor_name,
                    provider=f"{provider}:structural_kotlin",
                    latency_ms=int((time.perf_counter() - structural_started) * 1000),
                    retry_count=0,
                    fallback_step=fallback_step,
                    prompt_fingerprint=structural_fingerprint,
                    error_type=type(exc).__name__,
                )

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

        # Reverted to 3 after v11 task 1 (astropy-14995) regressed:
        # the original v10 success on this task came from the 3rd
        # attempt converging where attempts 1+2 had emitted plain
        # EVIDENCE_GAP. Keep 3; the perf gain wasn't worth a real
        # bug fix being lost. The Tier 4-H implicit extraction in
        # a384544 should make this less critical going forward.
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
                elif self._active_codegen_output_format == "aider_blocks":
                    call_prompt += (
                        f"\n\nPREVIOUS ATTEMPT FAILED: {last_error}"
                        f"{AIDER_FORMAT_RETRY_SUFFIX}"
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
            except CodegenEvidenceGapRequest as gap_exc:
                self._record_codegen_failure(
                    task_id=task_id,
                    actor_name=actor_name,
                    provider=provider,
                    latency_ms=int((time.perf_counter() - started) * 1000) if "started" in locals() else 0,
                    retry_count=attempt,
                    fallback_step=fallback_step,
                    prompt_fingerprint=prompt_fingerprint,
                    error_type=type(gap_exc).__name__,
                )
                # Tier 4-H bounded recovery: cap at 2 fires per provider
                # call. 2 (was 1) lets the stub-anchor detector raise a
                # follow-up EVIDENCE_GAP_REQUEST and have it actually
                # trigger another swap attempt, rather than fail with
                # "unfulfilled" when the model anchors on a stub during
                # the recovery prompt.
                _loop_count = int(getattr(self, "_tool_loop_count", 0))
                if _loop_count < 2:
                    self._tool_loop_count = _loop_count + 1
                    import logging as _log

                    _tl_logger = _log.getLogger("codegen.tool_loop")
                    _tl_logger.warning(
                        "tool_loop.fire requests=%s candidate_files=%s repo_root=%s",
                        [
                            {"file": r.file, "symbol": r.symbol, "why": (r.why or "")[:100]}
                            for r in gap_exc.requests
                        ],
                        list(context_files.keys()),
                        source_repo_path,
                    )
                    # Codex option E (2026-05-11): try budgeted swap
                    # first — re-truncate the requested file with the
                    # symbol added to keep_symbols, staying within the
                    # per-file byte cap. This makes Tier 4-H actually
                    # fulfillable for the common "I need this stubbed
                    # function's body" case.
                    swapped = self._swap_evidence_for_request(
                        gap_exc.requests,
                        candidate_files=context_files,
                        source_repo_path=source_repo_path,
                    )
                    _tl_logger.warning(
                        "tool_loop.swap files=%s",
                        swapped,
                    )
                    if swapped:
                        # Rebuild the prompt with the new context and try
                        # codegen again. The same `prompt` string still
                        # references file paths; context_files was
                        # mutated in place so the next provider call
                        # picks up the swapped views.
                        try:
                            return self._call_provider_once(
                                provider=provider,
                                prompt=prompt,
                                context_files=context_files,
                                source_repo_path=source_repo_path,
                                task_id=task_id,
                            )
                        except CodegenError:
                            # Swap recovery failed too — fall through to
                            # the older append-spans path below as a
                            # second-chance recovery.
                            pass
                    spans_text = self._fulfil_evidence_gap_request(
                        gap_exc.requests,
                        candidate_files=context_files,
                        source_repo_path=source_repo_path,
                    )
                    _tl_logger.warning(
                        "tool_loop.result span_count=%d span_bytes=%d",
                        spans_text.count("--- "),
                        len(spans_text),
                    )
                    if spans_text:
                        recovered_prompt = (
                            prompt
                            + "\n\n---\nThe harness fulfilled your "
                            "EVIDENCE_GAP_REQUEST below. Use these spans "
                            "as your Aider SEARCH anchors and produce the "
                            "diff now.\n"
                            + spans_text
                        )
                        try:
                            return self._call_provider_once(
                                provider=provider,
                                prompt=recovered_prompt,
                                context_files=context_files,
                                source_repo_path=source_repo_path,
                                task_id=task_id,
                            )
                        except CodegenError:
                            # Recovery attempt also failed; fall through.
                            pass
                # No request fulfilled (or recovery failed) — surface as
                # plain terminal so caller treats it as a hard stop.
                raise CodegenError(
                    f"codegen_terminal: EVIDENCE_GAP_REQUEST unfulfilled "
                    f"({len(gap_exc.requests)} request(s))"
                ) from gap_exc
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

    def _fulfil_evidence_gap_request(
        self,
        requests: list,
        *,
        candidate_files: dict[str, str],
        source_repo_path: str | None,
    ) -> str:
        """Wrap the codegen_tool_loop fetcher with a safe try/except.

        Returns the rendered prompt section (possibly empty) so the
        caller can append it without further checking.
        """
        try:
            from app.services.codegen_tool_loop import (
                fulfil_requests,
                render_spans_for_prompt,
            )

            repo_root = Path(source_repo_path) if source_repo_path else None
            spans = fulfil_requests(
                requests, candidate_files=candidate_files, repo_root=repo_root
            )
            return render_spans_for_prompt(spans)
        except Exception:  # noqa: BLE001
            return ""

    def _swap_evidence_for_request(
        self,
        requests: list,
        *,
        candidate_files: dict[str, str],
        source_repo_path: str | None,
    ) -> list[str]:
        """Re-truncate requested file with augmented keep set, replacing
        the version in candidate_files. Codex's option E (2026-05-11):
        when a symbol is stubbed, don't append spans (which exceed
        budget); rebuild the file view with the symbol added to
        keep_symbols, evicting other slices to stay within the same
        per-file byte cap. Returns the list of file paths that were
        successfully re-truncated.
        """
        if not source_repo_path:
            return []
        try:
            from app.services.evidence_pack import truncate_for_context
        except Exception:  # noqa: BLE001
            return []
        repo_root = Path(source_repo_path)
        per_file_budget = int(
            getattr(self.settings, "codegen_per_file_byte_budget", 18_000)
        )
        swapped: list[str] = []
        # Group requests by file so we can union all requested symbols
        # for each file in a single re-truncation pass.
        by_file: dict[str, list[str]] = {}
        for req in requests:
            file = getattr(req, "file", None)
            symbol = getattr(req, "symbol", None)
            if not file or not symbol:
                continue
            by_file.setdefault(str(file), []).append(str(symbol))
        for file_path, new_keeps in by_file.items():
            if file_path not in candidate_files:
                continue
            disk = repo_root / file_path
            try:
                original_text = disk.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            existing = candidate_files[file_path]
            # Best-effort: extract any function names already kept whole
            # from the existing view-summary header so we don't drop them
            # when adding the new symbol.
            prior_keeps: list[str] = []
            for line in existing.splitlines()[:30]:
                marker = "Real bodies (use as SEARCH anchors):"
                if marker in line:
                    tail = line.split(marker, 1)[1]
                    for chunk in tail.replace("(", ",").replace(")", ",").split(","):
                        token = chunk.strip(" #.,")
                        if token and token.replace("_", "").isalnum():
                            prior_keeps.append(token)
                    break
            merged_keeps = list(dict.fromkeys(prior_keeps + new_keeps))
            new_view = truncate_for_context(
                original_text,
                max_bytes=per_file_budget,
                path=file_path,
                keep_symbols=merged_keeps,
            )
            existing_stubbed = [
                sym for sym in new_keeps
                if self._symbol_is_stubbed(existing, sym)
            ]
            new_stubbed = [
                sym for sym in new_keeps
                if self._symbol_is_stubbed(new_view, sym)
            ]
            logger.warning(
                "swap_evidence file=%s new_keeps=%s prior_keeps=%s "
                "existing_bytes=%d new_bytes=%d existing_stubbed=%s "
                "new_stubbed=%s view_changed=%s",
                file_path, new_keeps, prior_keeps,
                len(existing), len(new_view) if new_view else 0,
                existing_stubbed, new_stubbed,
                new_view != existing,
            )
            # Codex's correctness criterion (2026-05-11): success means
            # each requested symbol's body is ACTUALLY whole in the new
            # view, not just that the view changed. Heuristic: if a
            # `def NAME(...):\n    pass\n` stub for the requested symbol
            # still appears, swap failed for this symbol — try a
            # focused per-symbol view as last resort.
            if not new_view:
                continue
            unresolved = [
                sym for sym in new_keeps
                if self._symbol_is_stubbed(new_view, sym)
            ]
            if not unresolved:
                # Every requested symbol is whole in new_view. Take it
                # even if it's identical to the existing view — that
                # means the symbol was already whole and the swap is a
                # no-op (the model just need to be told to look there).
                if new_view != existing:
                    candidate_files[file_path] = new_view
                    swapped.append(file_path)
                else:
                    # No-op swap: log but don't add to swapped (caller
                    # should fall through to span fetcher).
                    pass
                continue
            # Symbol(s) still stubbed in budgeted re-truncation —
            # produce a focused view with just those symbols' bodies
            # plus minimal surrounding context.
            focused = self._build_focused_symbol_view(
                original_text=original_text,
                file_path=file_path,
                requested_symbols=unresolved,
            )
            if focused and focused != existing:
                candidate_files[file_path] = focused
                swapped.append(file_path)
        return swapped

    @staticmethod
    def _symbol_is_stubbed(view: str, symbol: str) -> bool:
        """Heuristic: detect if a function/class is rendered as a `pass`
        stub in the truncated view. Returns True if `def {symbol}(...):`
        is followed (modulo blank lines / docstring) by an indented
        `pass` and nothing else of substance.
        """
        # Match the symbol's def line + the following few lines. If
        # the body collapses to just `pass`, it's a stub.
        pattern = re.compile(
            rf"^[ \t]*(?:async\s+)?def\s+{re.escape(symbol)}\s*\([^)]*\)[^\n:]*:\s*\n"
            rf"((?:[ \t]+(?:\"\"\".*?\"\"\"|'''.*?''')\s*\n)?)"
            rf"[ \t]+pass\s*\n",
            re.MULTILINE | re.DOTALL,
        )
        return bool(pattern.search(view))

    def _build_focused_symbol_view(
        self,
        *,
        original_text: str,
        file_path: str,
        requested_symbols: list[str],
    ) -> str:
        """Build a per-symbol focused view: pull the requested function
        bodies whole from disk plus a tiny header noting that other
        parts of the file are omitted. Used as last resort when budget
        truncation can't keep all requested symbols whole.
        """
        try:
            import ast as _ast
            tree = _ast.parse(original_text)
        except SyntaxError:
            return ""
        lines = original_text.splitlines(keepends=True)
        kept: list[str] = []
        kept.append("# === focused symbol view ===\n")
        kept.append(
            f"# {file_path}: extracting only requested symbols whole; "
            f"rest of file omitted.\n"
        )
        kept.append(
            f"# Requested: {', '.join(requested_symbols)}\n"
        )
        kept.append("# === end ===\n\n")
        # Walk top-level + class-level defs; emit any matching the request.
        wanted = set(requested_symbols)

        def emit_def(node: Any) -> None:
            if not getattr(node, "lineno", None):
                return
            start = node.lineno - 1
            end = (node.end_lineno or node.lineno) - 1
            if 0 <= start < len(lines) and 0 <= end < len(lines):
                kept.append(f"# --- {file_path}:{node.lineno}-{node.end_lineno} ---\n")
                kept.extend(lines[start:end + 1])
                kept.append("\n")

        for node in tree.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name in wanted:
                    emit_def(node)
            elif isinstance(node, _ast.ClassDef):
                emitted_class_header = False
                for child in node.body:
                    if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        if child.name in wanted:
                            if not emitted_class_header:
                                kept.append(
                                    f"# (in class {node.name})\n"
                                )
                                emitted_class_header = True
                            emit_def(child)
        if len(kept) <= 4:  # only header lines, no bodies — nothing matched
            return ""
        return "".join(kept)

    def _call_provider_once(
        self,
        *,
        provider: str,
        prompt: str,
        context_files: dict[str, str],
        source_repo_path: str | None,
        task_id: str,
    ) -> CodegenResult:
        """Single-shot provider invocation used by the Tier 4-H recovery
        path. Mirrors the dispatch in ``_try_provider`` but doesn't
        re-enter the retry loop or terminal-marker handler.
        """
        if provider == "claude_code":
            return self._call_claude_code(
                prompt,
                context_files=context_files,
                source_repo_path=source_repo_path,
                task_id=task_id,
            )
        if provider == "codex":
            return self._call_codex(prompt, context_files=context_files)
        if provider == "anthropic":
            return self._call_anthropic(prompt)
        if provider == "deepseek":
            return self._call_deepseek(prompt)
        if provider == "ollama":
            return self._call_ollama(prompt, context_files=context_files)
        if provider == "minimax":
            return self._call_minimax(prompt, context_files=context_files)
        if provider == "openai":
            return self._call_openai(prompt)
        raise CodegenError(f"Unknown provider: {provider}")

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
        # UI-set per-stage override (runtime_overrides.json) wins over .env.
        # When unset, falls through bytewise to the historical .env-driven path.
        from app.services.runtime_override import effective_provider
        codegen_override = effective_provider(
            "codegen", getattr(self.settings, "codegen_provider", None)
        )
        if codegen_override and codegen_override != "auto":
            return [codegen_override]

        provider = effective_provider(
            "primary_agent", self.settings.primary_agent_provider
        )
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
        content, input_tokens, output_tokens = self._call_anthropic_text(
            prompt,
            self.settings.anthropic_model,
            system_prompt=CODEGEN_SYSTEM_PROMPT,
        )
        return self._parse_response(
            content,
            provider_name="anthropic",
            model_name=self.settings.anthropic_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _call_anthropic_text(
        self,
        prompt: str,
        model_name: str,
        *,
        system_prompt: str,
    ) -> tuple[str, int, int]:
        """Call Anthropic Messages API once and return raw text."""
        if not self.settings.anthropic_api_key:
            raise CodegenError("OPS_AGENT_ANTHROPIC_API_KEY is not configured.")

        url = f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model_name,
            "max_tokens": 8192,
            "system": self._build_system_prompt(system_prompt, getattr(self, "_current_context_files", None)),
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
            return (
                content,
                int(usage.get("input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0),
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
            source_repo_path=source_repo_path,
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
        source_repo_path: str | None = None,
    ) -> CodegenResult:
        """Fallback: run Claude Code CLI in a disposable temp directory.

        Used when no source repo is available for worktree mode.

        2026-05-11 (G2 v26 finding): when `source_repo_path` is given
        (e.g. SWE-bench's cached repo without .git metadata), copy the
        ORIGINAL un-truncated file content from disk into the tempdir
        instead of the truncated `context_files`. This makes the
        post-edit `git diff` produce a patch in original-file
        coordinates, which is what SWE-bench's `git apply` and our
        leg-2 symbol verifier expect.
        """
        import logging as _log
        _logger = _log.getLogger("codegen.claude_code.tempdir")

        workdir = tempfile.mkdtemp(prefix="ops_claude_code_")
        repo_root = Path(source_repo_path) if source_repo_path else None

        def _resolve_initial_content(rel_path: str, fallback: str) -> str:
            """Prefer un-truncated disk content; fall back to context_files
            (truncated view) only if disk read fails."""
            if repo_root is None:
                return fallback
            disk = repo_root / rel_path
            try:
                return disk.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return fallback

        try:
            # Write context files into the working directory
            for rel_path, content in context_files.items():
                full = Path(workdir) / rel_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(
                    _resolve_initial_content(rel_path, content),
                    encoding="utf-8",
                )

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
                    fp.write_text(
                        _resolve_initial_content(rel_path, content),
                        encoding="utf-8",
                    )

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
        elif self._active_codegen_output_format == "aider_blocks":
            parts = [
                "Generate Aider search/replace blocks that implement the task below.",
                "",
                "IMPORTANT RULES:",
                "- ONLY emit blocks for files you actually need to change.",
                "- The SEARCH region must be a verbatim substring of the file content I provided below — do NOT paraphrase or trim whitespace.",
                "- Return only the blocks. No prose, no markdown fences.",
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

        # Phase 2.3 (2026-05-11): inject must_inspect_files / likely_touch_files
        # as READ-ONLY context advisories. The planner lists files whose content
        # the patch depends on (e.g. build.gradle for a SDK version, Manifest for
        # a permission tag) but that are not themselves modified. Surfacing them
        # in the prompt lets the codegen LLM ground references (e.g. "the play-
        # services-maps dependency is at the version declared in
        # libs.versions.toml") without licensing edits to those files.
        must_inspect_files = plan_json.get("must_inspect_files") or []
        likely_touch_files = plan_json.get("likely_touch_files") or []
        if isinstance(must_inspect_files, list) and must_inspect_files:
            parts.extend(["", "=== READ-ONLY CONTEXT FILES (must inspect, do NOT modify) ==="])
            for path in must_inspect_files[:12]:
                if isinstance(path, str) and path.strip():
                    parts.append(f"- {path.strip()}")
            parts.append(
                "Inspect these for grounding (config / manifest / dependency / "
                "route registration). Do NOT include them in your diff hunks."
            )
        if isinstance(likely_touch_files, list) and likely_touch_files:
            parts.extend(["", "=== LIKELY-TOUCH CANDIDATES (modify only if directly required) ==="])
            for path in likely_touch_files[:12]:
                if isinstance(path, str) and path.strip():
                    parts.append(f"- {path.strip()}")
            parts.append(
                "Evidence here is inconclusive. Modify only if you find a "
                "concrete reason in context. Prefer must-touch over guesses."
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

        # Phase 1.2 (2026-05-11): acceptance-as-codegen-contract.
        # Planner-emitted acceptance_tests are normally a post-codegen
        # gate, but DeepSeek often satisfies the surface intent without
        # meeting the structural requirements (e.g. P69-19 v11: wrote a
        # signup-form field patch that compiled but never imported the
        # Maps SDK). Surface acceptance patterns up front as a hard
        # contract so the model includes the required signals or
        # declines explicitly via NO_CHANGE_NEEDED / EVIDENCE_GAP.
        acceptance_tests = plan_json.get("acceptance_tests") or []
        # v16.0 (2026-05-12) — batch-scope filtering. The codegen call is
        # often scoped to a single file (parallel_max>1 per-file batches).
        # An acceptance_test that targets a DIFFERENT file is impossible
        # for this batch to satisfy on its own; surfacing it as a HARD
        # REQUIREMENT makes the model emit `## PLAN_CONFLICT` and produce
        # no diff, which then fails batch_coverage.check on the
        # legitimately-must-touch file (24ecfb5c failure mode).
        #
        # Filter rule: keep a test if its `file` field is empty (global —
        # the test applies to the merged diff, will be re-evaluated post-
        # merge) OR if the file is in this batch's context_files.
        _batch_paths = set(context_files.keys()) if context_files else set()
        _filtered_acceptance: list[dict] = []
        _deferred_acceptance: list[dict] = []
        for _t in acceptance_tests:
            if not isinstance(_t, dict):
                continue
            _tgt = str(_t.get("file") or _t.get("target") or "").strip()
            if not _tgt or _tgt in _batch_paths:
                _filtered_acceptance.append(_t)
            else:
                _deferred_acceptance.append(_t)
        if _filtered_acceptance:
            parts.extend(["", "=== HARD ACCEPTANCE REQUIREMENTS ==="])
            parts.append(
                "Your patch will be REJECTED by an automated post-gate unless "
                "the final diff satisfies ALL of the following. Do not produce "
                "a shallow patch that passes compilation but misses these "
                "signals — if the available context is insufficient to "
                "implement them, emit NO_CHANGE_NEEDED or EVIDENCE_GAP_REQUEST "
                "instead of inventing a partial answer. "
                "If you emit NO_CHANGE_NEEDED, you MUST include a JSON block "
                "with `evidence` listing EXACT quoted lines from the file you "
                "reviewed. A claim without verifiable quotes is treated as "
                "PHANTOM_NO_CHANGE and rejected. Format:\n"
                "## NO_CHANGE_NEEDED\n"
                "{\n"
                "  \"reason\": \"...\",\n"
                "  \"evidence\": [\n"
                "    {\"file_path\": \"...\", \"claim\": \"...\", "
                "\"quote\": \"copy real lines from file\"}\n"
                "  ]\n"
                "}"
            )
            for idx, test in enumerate(_filtered_acceptance[:8], start=1):
                kind = str(test.get("kind") or "").strip()
                pattern = str(test.get("pattern") or "").strip()
                target = str(test.get("file") or test.get("target") or "").strip()
                rationale = str(test.get("rationale") or "").strip()
                line = f"  {idx}. [{kind}]" if kind else f"  {idx}."
                if pattern:
                    line += f" required pattern: `{pattern}`"
                if target:
                    line += f" in {target}"
                parts.append(line)
                if rationale:
                    parts.append(f"     why: {rationale[:240]}")
            parts.append(
                "Treat these as non-negotiable. Adding the required imports / "
                "callbacks / class references is preferred over leaving them "
                "out."
            )
        if _deferred_acceptance and _batch_paths:
            # Show the model what's deferred to OTHER batches so it knows
            # NOT to emit PLAN_CONFLICT for these — they're someone else's
            # job. This is the explicit signal the 24ecfb5c run was missing.
            parts.extend([
                "",
                "=== ACCEPTANCE TESTS HANDLED BY OTHER BATCHES (informational) ===",
                "These tests target files NOT in your batch. They will be "
                "satisfied by a parallel codegen call for the relevant file. "
                "Do NOT emit PLAN_CONFLICT just because you can't satisfy "
                "them — your job is the files listed above only.",
            ])
            for test in _deferred_acceptance[:6]:
                target = str(test.get("file") or test.get("target") or "").strip()
                pattern = str(test.get("pattern") or "").strip()
                parts.append(f"  - {target}: pattern `{pattern}` (handled elsewhere)")

        # v16.2 — Contract Coverage requirement. When the plan carries
        # required_contracts (sourced from a matched domain playbook in
        # the orchestrator, not invented by the planner), the model MUST
        # close every contract by emitting a CONTRACT_COVERAGE JSON
        # block at the end of its response. NO_CHANGE_NEEDED_VERIFIED is
        # NOT a free pass — every required_contract must appear in
        # exactly one of implemented_contracts / verified_no_change_contracts
        # / unimplemented_contracts, AND the harness will grep the diff
        # (for implemented) or the actual file content (for no_change)
        # against the contract's verification patterns. Prose claims
        # without verifiable evidence are treated as lies.
        required_contracts = plan_json.get("required_contracts") or []
        if required_contracts:
            parts.extend(["", "=== REQUIRED CONTRACTS (v16.2 coverage protocol) ==="])
            parts.append(
                "Your plan commits to implementing the following named "
                "contracts. You MUST close every one of them by including "
                "a CONTRACT_COVERAGE JSON block at the end of your "
                "response. The harness will verify each claim against "
                "the actual artifact (diff or pre-existing file content) "
                "using the contract's verification patterns. Prose "
                "claims without grep-verifiable evidence are rejected "
                "as coverage lies.\n\n"
                "Required contracts for this task:"
            )
            for c in required_contracts[:8]:
                if not isinstance(c, dict):
                    continue
                cid = c.get("contract_id") or c.get("id") or "?"
                signal = (c.get("signal") or "").strip().replace("\n", " ")
                patterns = c.get("verification_patterns") or []
                pat_preview = patterns[0] if patterns else ""
                parts.append(f"  - id: {cid}")
                if signal:
                    parts.append(f"    signal: {signal[:280]}")
                if pat_preview:
                    parts.append(f"    verifier_pattern (one of): `{pat_preview}`")
            parts.append("")
            parts.append("At the end of your response, emit (literal text):")
            parts.append("")
            parts.append("## CONTRACT_COVERAGE")
            parts.append("```json")
            parts.append("{")
            parts.append('  "implemented_contracts": [')
            parts.append(
                '    {"id": "<contract_id>", "file": "<path>", '
                '"evidence_quote": "<exact line you ADDED in your diff>", '
                '"evidence_mode": "direct_diff | diff_modified_payload_existing_sink", '
                '"diff_evidence": "<what your diff CHANGED in one sentence>", '
                '"context_evidence": "<unchanged surrounding code that completes the data flow, if any>"}'
            )
            parts.append("  ],")
            parts.append('  "verified_no_change_contracts": [')
            parts.append(
                '    {"id": "<contract_id>", "file": "<path>", '
                '"evidence_quote": "<exact line that ALREADY EXISTS in the file>"}'
            )
            parts.append("  ],")
            parts.append('  "unimplemented_contracts": [')
            parts.append(
                '    {"id": "<contract_id>", "reason": "<honest reason '
                'this batch did not address it>"}'
            )
            parts.append("  ]")
            parts.append("}")
            parts.append("```")
            parts.append("")
            parts.append(
                "Rules: every required_contract MUST appear in exactly "
                "one of the three lists. NO_CHANGE_NEEDED_VERIFIED is "
                "ONLY accepted when every relevant required_contract is "
                "in verified_no_change_contracts WITH a quoted line from "
                "the file. If a contract you can't see evidence for in "
                "this batch's files: put it in unimplemented_contracts "
                "with a real reason — do NOT silently omit it."
            )
            parts.append("")
            parts.append(
                "evidence_mode guide (v16.2.1): when your diff DIRECTLY "
                "adds the contract's expected pattern (e.g. you add a new "
                "setValue() call carrying the contract's payload), use "
                "evidence_mode = direct_diff. When you implement the "
                "contract by CHANGING THE INPUT of an existing, unchanged "
                "sink call (e.g. you replace hardcoded 0.0 with state "
                "variables inside the userData map that the unchanged "
                "userRef.setValue(userData) consumes), use evidence_mode "
                "= diff_modified_payload_existing_sink and explain BOTH "
                "what the diff changed AND which unchanged surrounding "
                "line completes the data flow. This second mode IS still "
                "implemented (not verified_no_change), because the "
                "previous behavior persisted hardcoded values, not the "
                "user's selection."
            )

        existing_contract_evidence = self._existing_contract_evidence_block(
            plan_json, context_files
        )
        if existing_contract_evidence:
            parts.extend(["", existing_contract_evidence])

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
                {"role": "system", "content": self._build_system_prompt(CODEGEN_SYSTEM_PROMPT_JSON_MODE, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
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
                {"role": "system", "content": self._build_system_prompt(CODEGEN_SYSTEM_PROMPT_JSON_MODE, getattr(self, "_current_context_files", None)),},
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
        effective_prompt = prompt
        if self._react_loop_enabled():
            try:
                from app.services.codegen_react_loop import react_codegen_call

                _repo_path = getattr(self, "_current_source_repo_path", None)
                effective_prompt = react_codegen_call(
                    task_description=prompt,
                    plan_json={},
                    context_files=getattr(self, "_current_context_files", None) or {},
                    once_call=lambda p: self._call_deepseek_once_text(p, model_name),
                    repo_root=Path(_repo_path) if _repo_path else None,
                )
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger("codegen").warning(
                    "react_loop.error_falling_back",
                    extra={
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:200],
                    },
                )
                effective_prompt = prompt

        try:
            return self._call_deepseek_once(effective_prompt, model_name)
        except CodegenError as exc:
            msg = str(exc)
            # Aider-mode parse failures get the Aider-specific retry hint.
            # The unified-diff "minimal edit" / "raw diff" retries don't
            # apply when blocks are the wire format.
            if self._active_codegen_output_format == "aider_blocks":
                if "Aider blocks could not be parsed" in msg or "Aider apply failed" in msg or "produced no diff" in msg:
                    return self._call_deepseek_once(
                        effective_prompt + AIDER_FORMAT_RETRY_SUFFIX, model_name
                    )
                raise
            if _is_minimal_edit_retryable_error_message(msg):
                return self._call_deepseek_once(
                    effective_prompt + MINIMAL_EDIT_RETRY_SUFFIX, model_name
                )
            if "valid unified diff" not in msg and "changed file headers" not in msg:
                raise
            return self._call_deepseek_once(effective_prompt + RAW_DIFF_RETRY_SUFFIX, model_name)

    def _call_deepseek_once_text(self, prompt: str, model_name: str) -> str:
        """Call DeepSeek once and return raw content without parsing."""
        content, _, _ = self._call_deepseek_text(
            prompt,
            model_name,
            system_prompt=CODEGEN_REACT_PLAN_SYSTEM_PROMPT,
            purpose="codegen.react_plan",
        )
        return content

    def _call_deepseek_text(
        self,
        prompt: str,
        model_name: str,
        *,
        system_prompt: str,
        purpose: str,
    ) -> tuple[str, int, int]:
        """Call DeepSeek once and return raw content plus token counts."""
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._build_system_prompt(system_prompt, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 8192,
        }
        try:
            response = cached_http_post(
                url=url,
                json=body,
                headers=headers,
                timeout=external_http_timeout(self.settings.deepseek_timeout_seconds),
                provider_hint=f"deepseek.{purpose}",
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            log_llm_cache_hit(
                provider="deepseek",
                model=model_name,
                purpose=purpose,
                usage=usage,
            )
            return (
                content,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
            )
        except httpx.HTTPError as exc:
            raise CodegenError(f"DeepSeek API error: {exc}") from exc

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
                {"role": "system", "content": self._build_system_prompt(CODEGEN_SYSTEM_PROMPT, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            # Phase A.2 (2026-05-11): temperature ignored under thinking
            # mode but kept for non-thinking variants. max_tokens bumped
            # 8K → 32K (was hitting empty-response cliff). reasoning_effort
            # explicitly `high` for cross-file codegen (was implicit `high`).
            "temperature": 0.0,
            "max_tokens": int(
                getattr(self.settings, "deepseek_max_tokens_codegen", 32768)
            ),
            "reasoning_effort": str(
                getattr(self.settings, "deepseek_reasoning_effort_codegen", "high")
            ),
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
            msg = str(exc)
            if self._active_codegen_output_format == "aider_blocks":
                if "Aider blocks could not be parsed" in msg or "Aider apply failed" in msg or "produced no diff" in msg:
                    return self._call_openai_once(
                        prompt + AIDER_FORMAT_RETRY_SUFFIX, model_name
                    )
                raise
            if _is_minimal_edit_retryable_error_message(msg):
                return self._call_openai_once(
                    prompt + MINIMAL_EDIT_RETRY_SUFFIX, model_name
                )
            if "valid unified diff" not in msg and "changed file headers" not in msg:
                raise
            return self._call_openai_once(prompt + RAW_DIFF_RETRY_SUFFIX, model_name)

    def _call_openai_once(self, prompt: str, model_name: str) -> CodegenResult:
        """Call OpenAI once and parse the raw response."""
        content, input_tokens, output_tokens = self._call_openai_text(
            prompt,
            model_name,
            system_prompt=CODEGEN_SYSTEM_PROMPT,
            purpose="codegen",
        )
        return self._parse_response(
            content,
            provider_name="openai",
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _call_openai_text(
        self,
        prompt: str,
        model_name: str,
        *,
        system_prompt: str,
        purpose: str,
    ) -> tuple[str, int, int]:
        """Call OpenAI-compatible API once and return raw text."""
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._build_system_prompt(system_prompt, getattr(self, "_current_context_files", None)),},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
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
                purpose=purpose,
                usage=usage,
            )
            return (
                content,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
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
        """Parse an LLM response. Dispatches on the active codegen output
        format: aider_blocks → search/replace blocks → unified diff at the
        boundary; unified_diff → raw diff (the historical path).
        """
        if self._active_codegen_output_format == "aider_blocks":
            return self._parse_response_aider(
                content,
                provider_name=provider_name,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        return self._parse_response_unified_diff(
            content,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _parse_response_unified_diff(
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
        # Honour playbook-defined terminal markers — model is signalling
        # "can't proceed" and retries won't help. Surface a non-retryable
        # CodegenError so the harness fails fast and the orchestrator can
        # decide what to do next (e.g. expand evidence pack, re-plan).
        marker = _detect_terminal_marker(diff)
        if marker is not None:
            # Tier 4-H: if the model emitted a structured request alongside
            # EVIDENCE_GAP, raise a special exception so _try_provider can
            # fetch the missing spans and retry once.
            if marker.startswith("EVIDENCE_GAP"):
                from app.services.codegen_tool_loop import (
                    parse_evidence_gap_requests,
                )

                requests = parse_evidence_gap_requests(diff)
                if requests:
                    raise CodegenEvidenceGapRequest(requests, raw_marker=marker)
            if marker.startswith("NO_CHANGE_NEEDED"):
                classification = _classify_no_change(
                    diff,
                    getattr(self, "_current_context_files", None),
                )
                if classification is not None:
                    raise CodegenError(
                        _format_no_change_terminal_message(
                            classification,
                            default_marker_detail=marker.removeprefix(
                                "NO_CHANGE_NEEDED"
                            ).lstrip(": "),
                        )
                    )
            raise CodegenError(f"codegen_terminal: {marker}")
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

        # v16.2: extract the model's contract coverage block (when
        # present) from the FULL raw response — the diff text has
        # already been stripped of code fences and prose. The coverage
        # block lives outside the diff fence so we look in `content`.
        coverage_payload = _extract_contract_coverage(content)

        return CodegenResult(
            diff=diff,
            summary=summary,
            files_changed=files_changed,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            contract_coverage=coverage_payload,
        )

    def _parse_response_aider(
        self,
        content: str,
        *,
        provider_name: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> CodegenResult:
        """Parse Aider search/replace blocks and convert to unified diff.

        Codex consult 2026-05-11 (truncation-coord-bug): Aider blocks
        must apply to ORIGINAL un-truncated source, not to the prompt
        view. Otherwise the produced diff carries truncated-coordinate
        line numbers (e.g. anchored on a `pass` stub) and SWE-bench's
        `git apply` rejects it. Flow: read original from
        ``source_repo_path`` for each touched file, apply blocks
        there, generate diff against original. If a block's SEARCH
        text exists ONLY in the prompt view (i.e. a synthetic stub),
        we surface a synthetic EVIDENCE_GAP_REQUEST so Tier 4-H/E can
        expand the symbol whole and retry.
        """
        from app.services.aider_format import (
            AiderParseError,
            aider_blocks_to_unified_diff,
            apply_aider_blocks_in_memory,
            parse_aider_blocks,
        )

        text = content.strip()
        # Same terminal-marker check as the unified-diff path. EVIDENCE_GAP
        # / NO_CHANGE_NEEDED / PLAN_CONFLICT are the playbook-sanctioned
        # ways for the model to say "can't make a patch from what I have"
        # — retrying just burns tokens. Surface as non-retryable.
        marker = _detect_terminal_marker(text)
        if marker is not None:
            if marker.startswith("EVIDENCE_GAP"):
                from app.services.codegen_tool_loop import (
                    parse_evidence_gap_requests,
                )

                requests = parse_evidence_gap_requests(text)
                if requests:
                    raise CodegenEvidenceGapRequest(requests, raw_marker=marker)
            if marker.startswith("NO_CHANGE_NEEDED"):
                classification = _classify_no_change(
                    text,
                    getattr(self, "_current_context_files", None),
                )
                if classification is not None:
                    raise CodegenError(
                        _format_no_change_terminal_message(
                            classification,
                            default_marker_detail=marker.removeprefix(
                                "NO_CHANGE_NEEDED"
                            ).lstrip(": "),
                        )
                    )
            raise CodegenError(f"codegen_terminal: {marker}")
        if text.startswith("```"):
            text = re.sub(r"^```(?:\w+)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        try:
            blocks = parse_aider_blocks(text)
        except AiderParseError as exc:
            preview = text[:300].replace("\n", "\\n")
            raise CodegenError(
                f"Aider blocks could not be parsed: {exc} | content_preview: {preview}"
            ) from exc

        prompt_view = dict(getattr(self, "_current_context_files", None) or {})
        repo_path = getattr(self, "_current_source_repo_path", None)
        originals = self._load_originals_for_blocks(blocks, repo_path, prompt_view)
        # Detect stub-anchor blocks BEFORE apply: SEARCH that exists in
        # the prompt view but not in the original is a coordinate-space
        # mismatch — the model anchored on a synthetic `pass` stub. Turn
        # those into EVIDENCE_GAP_REQUEST so E swap can expand the
        # symbol and retry. This matches Codex's "fail closed and feed
        # failures into E" recommendation.
        stub_requests = self._detect_stub_anchor_requests(
            blocks, originals=originals, prompt_view=prompt_view
        )
        if stub_requests:
            from app.services.codegen_tool_loop import GapRequest

            requests_obj = [
                GapRequest(file=r["file"], symbol=r["symbol"], why=r["why"])
                for r in stub_requests
            ]
            raise CodegenEvidenceGapRequest(
                requests_obj, raw_marker="STUB_ANCHOR_DETECTED"
            )
        result = apply_aider_blocks_in_memory(blocks, originals)
        if result.errors:
            reasons = "; ".join(
                f"{e.file}#{e.block_index}:{e.reason}" for e in result.errors[:5]
            )
            raise CodegenError(f"Aider apply failed: {reasons}")

        diff = aider_blocks_to_unified_diff(result)
        if not diff.strip():
            raise CodegenError("Aider blocks parsed but produced no diff (empty edits).")

        files_changed = list(result.before_after.keys())
        summary = (
            f"Generated patch modifying {len(files_changed)} file(s) "
            f"via Aider blocks: {', '.join(files_changed[:5])}"
        )
        # v16.2: contract coverage block, when present in the model's
        # response, is parsed identically across output formats.
        coverage_payload = _extract_contract_coverage(content)
        return CodegenResult(
            diff=diff,
            summary=summary,
            files_changed=files_changed,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            contract_coverage=coverage_payload,
        )

    def _load_originals_for_blocks(
        self,
        blocks: list,
        source_repo_path: str | None,
        prompt_view: dict[str, str],
    ) -> dict[str, str]:
        """Load un-truncated source from disk for files referenced in
        Aider blocks. New files (is_new_file=True) keep an empty
        original. Falls back to prompt_view content if disk read fails
        or no source_repo_path is set — preserves backward compatibility
        for non-SWE-bench paths where original==prompt_view.
        """
        result: dict[str, str] = {}
        repo = Path(source_repo_path) if source_repo_path else None
        for blk in blocks:
            file = getattr(blk, "file", None)
            if not file or file in result:
                continue
            if getattr(blk, "is_new_file", False):
                result[file] = ""
                continue
            if repo is not None:
                disk = repo / file
                try:
                    result[file] = disk.read_text(encoding="utf-8")
                    continue
                except (OSError, UnicodeDecodeError):
                    pass
            # Fallback: use prompt view content. This is the
            # pre-2026-05-11 behavior — only safe when the file wasn't
            # truncated.
            result[file] = prompt_view.get(file, "")
        return result

    def _detect_stub_anchor_requests(
        self,
        blocks: list,
        *,
        originals: dict[str, str],
        prompt_view: dict[str, str],
    ) -> list[dict[str, str]]:
        """Detect blocks whose SEARCH text exists ONLY in the prompt
        view (truncated stub) but not in the original file. Returns a
        list of {file, symbol, why} so the caller can raise a synthetic
        EVIDENCE_GAP_REQUEST and trigger E swap. The symbol is best-
        effort extracted from the block's SEARCH text (`def NAME(...)`
        line), defaulting to '<unknown>' if not found.
        """
        out: list[dict[str, str]] = []
        for blk in blocks:
            file = getattr(blk, "file", None)
            search = getattr(blk, "search", "") or ""
            if not file or not search.strip():
                continue
            if getattr(blk, "is_new_file", False):
                continue
            original = originals.get(file, "")
            view = prompt_view.get(file, "")
            # If SEARCH matches both, no stub problem.
            if search in original:
                continue
            # If SEARCH matches the prompt view but NOT the original,
            # that's a stub-anchor mismatch.
            if not view or search not in view:
                # SEARCH doesn't match either — different failure
                # (anchor_not_found); let apply_aider_blocks handle it
                # so the model gets a real error.
                continue
            symbol = self._extract_symbol_from_search(search)
            why = (
                f"SEARCH matches the truncated view's `pass` stub for "
                f"{symbol}() but the original file's body is non-empty. "
                f"The harness needs the actual body to produce an "
                f"applyable diff. Re-truncate this file with {symbol} "
                f"in keep_symbols and emit a SEARCH/REPLACE block "
                f"against the real body."
            )
            out.append({"file": file, "symbol": symbol, "why": why})
        return out

    @staticmethod
    def _extract_symbol_from_search(search: str) -> str:
        """Best-effort extract a Python def/class name from a SEARCH
        block. Returns '<unknown>' if the SEARCH doesn't begin with a
        recognizable definition line.
        """
        for line in search.splitlines():
            stripped = line.strip()
            m = re.match(r"^(?:async\s+)?def\s+(\w+)\s*\(", stripped)
            if m:
                return m.group(1)
            m = re.match(r"^class\s+(\w+)\b", stripped)
            if m:
                return m.group(1)
        return "<unknown>"

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

    # ===========================================================
    # Tier 4 main course: multi-turn agent loop integration.
    # ===========================================================

    def _run_agent_loop(
        self,
        *,
        task_id: str,
        plan_json: dict[str, Any],
        context_files: dict[str, str],
        task_description: str,
        source_repo_path: str | None,
        actor_name: str | None,
    ) -> CodegenResult:
        """Run the multi-turn agent loop and return its diff as a
        CodegenResult.

        Maps the static-pipeline contract onto agent-loop semantics:
          - ``plan_json`` → user_prompt (plan summary + targets)
          - ``context_files`` → AgentLoopContext.candidate_files
          - ``source_repo_path`` → AgentLoopContext.repo_root
          - sandbox dir → from settings (when develop pipeline has one)
          - DeepSeek (default codegen provider) → llm_call wrapper

        On terminal_reason == "diff_emitted" the unified diff is wrapped
        as a CodegenResult. On any other terminal (cannot_proceed, budget,
        error, no diff), raise CodegenError so the caller's existing
        failure path takes over.
        """
        from app.services.agent_loop import (
            AgentLoopBudget,
            AgentLoopContext,
            run_agent_loop,
        )

        user_prompt = self._build_agent_user_prompt(plan_json, task_description)
        sandbox_dir_path: Path | None = None
        if source_repo_path:
            try:
                sandbox_dir_path = Path(source_repo_path)
            except (TypeError, ValueError):
                sandbox_dir_path = None

        ctx = AgentLoopContext(
            sandbox_dir=sandbox_dir_path,
            repo_root=sandbox_dir_path,
            candidate_files=dict(context_files or {}),
        )
        budget = AgentLoopBudget(
            max_turns=int(getattr(self.settings, "codegen_agent_max_turns", 12)),
            max_seconds=float(getattr(self.settings, "codegen_agent_max_seconds", 600.0)),
        )

        # Dispatch the codegen provider as the loop's LLM. DeepSeek is
        # the target per current product config; openai / anthropic
        # available as fallbacks for measurement.
        provider = self._resolve_provider_chain()[0]
        if provider == "mock":
            raise CodegenError(
                "agent loop does not support mock provider — set codegen_provider=deepseek"
            )

        def _llm_call(
            system_prompt: str, messages: list[dict[str, str]]
        ) -> tuple[str, str]:
            return self._agent_llm_call(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
            )

        result = run_agent_loop(
            task_id=task_id,
            user_prompt=user_prompt,
            llm_call=_llm_call,
            ctx=ctx,
            budget=budget,
        )

        from app.services.reviewer import DiffReviewer

        # Path 1: model self-emitted a valid apply_diff during the loop.
        if result.terminated_reason == "diff_emitted" and result.final_diff.strip():
            files_changed = DiffReviewer.parse_changed_files(result.final_diff)
            logger.warning(
                "agent_loop diff source=loop turns=%d files=%d bytes=%d",
                len(result.state.turns),
                len(files_changed),
                len(result.final_diff),
            )
            return CodegenResult(
                diff=result.final_diff,
                summary=(
                    f"Agent loop produced patch in {len(result.state.turns)} turn(s); "
                    f"modified {len(files_changed)} file(s)"
                ),
                files_changed=files_changed,
                provider_name=f"agent_loop:{provider}",
                model_name=getattr(self.settings, "deepseek_model", "deepseek-coder")
                if provider == "deepseek" else self._resolve_model_name(provider),
            )

        # Path 2: synthesis fallback. Loop did not emit apply_diff but
        # gathered evidence; force a tool-free synthesis call against
        # what was actually read.
        from app.services.agent_loop import build_context_bundle

        bundle = build_context_bundle(result.state)
        synth_diff = self._synthesize_from_context_bundle(
            bundle=bundle,
            plan_json=plan_json,
            task_description=task_description,
            context_files=context_files,
            provider=provider,
        )
        if synth_diff.strip():
            files_changed = DiffReviewer.parse_changed_files(synth_diff)
            logger.warning(
                "agent_loop diff source=synth_fallback turns=%d files=%d bytes=%d "
                "loop_terminated=%s",
                len(result.state.turns),
                len(files_changed),
                len(synth_diff),
                result.terminated_reason,
            )
            return CodegenResult(
                diff=synth_diff,
                summary=(
                    f"Agent loop ({result.terminated_reason}) → synthesis fallback "
                    f"produced patch on {len(files_changed)} file(s)"
                ),
                files_changed=files_changed,
                provider_name=f"agent_loop_synth:{provider}",
                model_name=getattr(self.settings, "deepseek_model", "deepseek-coder")
                if provider == "deepseek" else self._resolve_model_name(provider),
            )

        # Both paths failed — surface as CodegenError so the caller's
        # existing static-pipeline failure handling takes over.
        raise CodegenError(
            f"agent_loop_terminated:{result.terminated_reason} "
            f"(turns={len(result.state.turns)}, "
            f"final_diff_bytes={len(result.final_diff)}, "
            f"synth_fallback_bytes=0)"
        )

    def _synthesize_from_context_bundle(
        self,
        *,
        bundle: Any,
        plan_json: dict[str, Any],
        task_description: str,
        context_files: dict[str, str],
        provider: str,
    ) -> str:
        """Force a tool-free synthesis call from gathered context.

        Codex's prescription (2026-05-10): treat the agent loop as
        context retrieval; force apply_diff via a separate stage that
        has no tools, no JSON, no prose — just Aider blocks. One repair
        retry on Aider parse failure, then give up.
        """
        from app.services.aider_format import (
            AiderParseError,
            aider_blocks_to_unified_diff,
            apply_aider_blocks_in_memory,
            parse_aider_blocks,
        )

        system_prompt = (
            "You are the patch synthesis stage. You have no tools.\n\n"
            "The investigation phase is over. The context below is all "
            "available context.\n"
            "You MUST output Aider SEARCH/REPLACE blocks only.\n\n"
            "Rules:\n"
            "- No JSON.\n"
            "- No TOOL_CALL.\n"
            "- No prose, no preamble, no explanations.\n"
            "- Do not ask for more context.\n"
            "- Produce a minimal patch using only files/snippets shown below.\n"
            "- SEARCH text must be copied EXACTLY from the provided snippets "
            "(whitespace, indentation, comments preserved verbatim).\n"
            "- Prefer one file and the smallest behavior-preserving fix.\n"
            "- If several fixes are plausible, choose the most local one.\n"
            "- Do not edit tests unless the task is explicitly a test fix.\n"
            "- If no exact SEARCH block can be formed from the snippets, "
            "output exactly: CANNOT_PROCEED\n"
        )
        user_prompt = self._build_synth_user_prompt(
            bundle=bundle,
            plan_json=plan_json,
            task_description=task_description,
        )

        def _call(messages: list[dict[str, str]]) -> str:
            # synth fallback uses content only (no tool loop, so
            # reasoning_content doesn't need to be threaded forward).
            if provider == "deepseek":
                content, _ = self._agent_llm_call_deepseek(system_prompt, messages)
                return content
            if provider == "openai":
                content, _ = self._agent_llm_call_openai(system_prompt, messages)
                return content
            return ""

        logger.warning(
            "synth user_prompt: bytes=%d snippets=%d hits=%d listings=%d",
            len(user_prompt),
            sum(len(v) for v in bundle.file_snippets.values()),
            sum(len(v) for v in bundle.symbol_hits.values()),
            len(bundle.dir_listings),
        )
        response = _call([{"role": "user", "content": user_prompt}])
        logger.warning(
            "synth response_1: bytes=%d preview=%r",
            len(response), response[:200],
        )
        if response.strip().upper().startswith("CANNOT_PROCEED"):
            return ""
        diff = self._parse_synth_response_to_diff(
            response, context_files,
            parse_aider_blocks, apply_aider_blocks_in_memory,
            aider_blocks_to_unified_diff,
        )
        if diff:
            return diff

        # One repair call: feed the parser error back, ask for fixed blocks.
        repair_user = (
            user_prompt
            + "\n\n---\nYour previous response could not be parsed as Aider "
            "SEARCH/REPLACE blocks. Re-emit ONLY the blocks, with the exact "
            "fenced format:\n"
            "<<<<<<< SEARCH\n<exact text>\n=======\n<replacement>\n>>>>>>> REPLACE\n"
            "Do not add prose. Make sure the SEARCH text is copied verbatim "
            "from a snippet above."
        )
        response2 = _call([{"role": "user", "content": repair_user}])
        logger.warning(
            "synth response_2: bytes=%d preview=%r",
            len(response2), response2[:200],
        )
        if response2.strip().upper().startswith("CANNOT_PROCEED"):
            return ""
        return self._parse_synth_response_to_diff(
            response2, context_files,
            parse_aider_blocks, apply_aider_blocks_in_memory,
            aider_blocks_to_unified_diff,
        )

    def _parse_synth_response_to_diff(
        self,
        response: str,
        context_files: dict[str, str],
        parse_fn: Callable[[str], Any],
        apply_fn: Callable[[Any, dict[str, str]], Any],
        diff_fn: Callable[[Any], str],
    ) -> str:
        """Strip code fences, parse Aider blocks, apply, emit diff. Returns
        empty string on any failure; logs the specific failure mode so
        a post-mortem can find it."""
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:\w+)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            blocks = parse_fn(text)
        except Exception as exc:  # noqa: BLE001
            preview = text[:300].replace("\n", "\\n")
            logger.warning(
                "synth parse failed: %s | response_bytes=%d preview=%s",
                exc, len(response), preview,
            )
            return ""
        if not blocks:
            preview = text[:300].replace("\n", "\\n")
            logger.warning(
                "synth parse returned 0 blocks | response_bytes=%d preview=%s",
                len(response), preview,
            )
            return ""
        result = apply_fn(blocks, dict(context_files or {}))
        errors = getattr(result, "errors", None) or []
        if errors:
            reasons = "; ".join(
                f"{getattr(e, 'file', '?')}#{getattr(e, 'block_index', '?')}:"
                f"{getattr(e, 'reason', str(e))}"
                for e in errors[:5]
            )
            logger.warning(
                "synth apply failed: blocks=%d errors=%d reasons=%s",
                len(blocks), len(errors), reasons,
            )
            return ""
        diff = diff_fn(result)
        if not diff.strip():
            logger.warning(
                "synth applied but diff empty: blocks=%d files=%d",
                len(blocks), len(getattr(result, "before_after", {})),
            )
            return ""
        return diff

    def _build_synth_user_prompt(
        self,
        *,
        bundle: Any,
        plan_json: dict[str, Any],
        task_description: str,
    ) -> str:
        """Compact, structured digest for synthesis. NO conversation history,
        NO reasoning_content, NO tool-call protocol noise — just facts."""
        parts: list[str] = []

        objective = str(plan_json.get("objective") or "").strip()
        if objective:
            parts.append(f"OBJECTIVE: {objective}")
        change_summary = str(plan_json.get("change_summary") or "").strip()
        if change_summary and change_summary != objective:
            parts.append(f"SUMMARY: {change_summary}")

        if task_description.strip():
            parts.append("")
            parts.append("ISSUE:")
            parts.append(task_description.strip())

        must_touch = plan_json.get("must_touch_files") or []
        if must_touch:
            parts.append("")
            parts.append("CANDIDATE FILES (planner-suggested):")
            for path in must_touch:
                parts.append(f"  - {path}")

        # Symbol search hits — short, just (path, line, preview).
        if bundle.symbol_hits:
            parts.append("")
            parts.append("SYMBOL SEARCH HITS:")
            for name, hits in bundle.symbol_hits.items():
                parts.append(f"  {name}:")
                for hit in hits[:8]:
                    parts.append(f"    - {hit.path}:{hit.line}  {hit.preview[:120]}")

        # File snippets — the meat. Tag each so the model knows what's
        # available and where it came from.
        if bundle.file_snippets:
            parts.append("")
            parts.append("FILE SNIPPETS (verbatim — copy SEARCH text from these):")
            for path, snippets in bundle.file_snippets.items():
                for snippet in snippets:
                    range_label = ""
                    if snippet.line_start or snippet.line_end:
                        range_label = (
                            f" lines {snippet.line_start or '?'}-"
                            f"{snippet.line_end or '?'}"
                        )
                    parts.append("")
                    parts.append(f"--- BEGIN {path}{range_label} ---")
                    parts.append(snippet.text.rstrip())
                    parts.append(f"--- END {path}{range_label} ---")

        if bundle.dir_listings:
            parts.append("")
            parts.append("DIRECTORY LISTINGS (for orientation only):")
            for path, names in bundle.dir_listings.items():
                parts.append(f"  {path}: {', '.join(names[:30])}")

        parts.append("")
        parts.append(
            "Now output Aider SEARCH/REPLACE blocks for the fix, "
            "or CANNOT_PROCEED if no exact SEARCH can be formed from the snippets above."
        )
        return "\n".join(parts)

    def _build_agent_user_prompt(
        self,
        plan_json: dict[str, Any],
        task_description: str,
    ) -> str:
        """Build the agent loop's initial user message.

        Includes the planner's objective + must_touch + acceptance_tests
        so the model knows what to fix without us pre-loading every file.
        Tools let the model fetch what it actually needs.
        """
        parts: list[str] = []
        objective = str(plan_json.get("objective") or "").strip()
        if objective:
            parts.append(f"OBJECTIVE: {objective}")
        change_summary = str(plan_json.get("change_summary") or "").strip()
        if change_summary and change_summary != objective:
            parts.append(f"SUMMARY: {change_summary}")

        if task_description.strip():
            parts.append("TASK DESCRIPTION:")
            parts.append(task_description.strip())

        must_touch = plan_json.get("must_touch_files") or []
        if must_touch:
            parts.append("MUST-TOUCH FILES (the patch should target these):")
            for path in must_touch:
                parts.append(f"  - {path}")

        expected_new = plan_json.get("expected_new_files") or []
        if expected_new:
            parts.append("EXPECTED NEW FILES (the patch should create these):")
            for path in expected_new:
                parts.append(f"  - {path}")

        acceptance_tests = plan_json.get("acceptance_tests") or []
        if acceptance_tests:
            parts.append(
                "ACCEPTANCE TESTS (the diff will be checked against these structurally):"
            )
            for t in acceptance_tests[:6]:
                if isinstance(t, dict):
                    kind = str(t.get("kind") or "")
                    pattern = str(t.get("pattern") or "")
                    rationale = str(t.get("rationale") or "")[:160]
                    parts.append(f"  - {kind}: {pattern}  ({rationale})")

        parts.append("")
        parts.append(
            "BUDGET (hard): you have at most 12 turns total. Soft caps: "
            "6 reads, 4 symbol searches, 3 directory listings. "
            "Aim to apply_diff by turn 6-8 — finish reading by turn 5."
        )
        parts.append(
            "Workflow: read just enough to locate the fix (typically 2-3 "
            "narrow read_file calls + 1 search_symbol), then emit a single "
            "apply_diff with Aider-style SEARCH/REPLACE blocks. "
            "If after 6 reads you still don't see a fix, emit "
            "`## CANNOT_PROCEED` with a one-line reason. "
            "Do NOT loop on read_file — every extra read shrinks your "
            "budget for the actual fix."
        )
        return "\n".join(parts)

    def _agent_llm_call(
        self,
        *,
        provider: str,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> tuple[str, str]:
        """Provider-agnostic single-turn LLM call for agent loop.

        Currently wires DeepSeek (production codegen target). Anthropic
        and OpenAI added when needed for cross-model harness-contribution
        measurement.
        """
        if provider == "deepseek":
            return self._agent_llm_call_deepseek(system_prompt, messages)
        if provider == "openai":
            return self._agent_llm_call_openai(system_prompt, messages)
        raise CodegenError(
            f"agent loop currently supports deepseek + openai only, got: {provider}"
        )

    def _agent_llm_call_deepseek(
        self, system_prompt: str, messages: list[dict[str, str]]
    ) -> tuple[str, str]:
        if not self.settings.deepseek_api_key:
            raise CodegenError("OPS_AGENT_DEEPSEEK_API_KEY is not configured.")
        url = "https://api.deepseek.com/v1/chat/completions"
        body = {
            "model": self.settings.deepseek_model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": 0.0,
            "reasoning_effort": str(
                getattr(self.settings, "deepseek_reasoning_effort_codegen", "high")
            ),
            # Phase A.2 (2026-05-11): bump 16K → settings-driven 32K
            # default. V4-Pro reasoning_content easily exceeds 16K on
            # complex turns; older 16K caused empty-response cliffs.
            "max_tokens": int(
                getattr(self.settings, "deepseek_max_tokens_agent_loop", 32768)
            ),
        }
        try:
            response = cached_http_post(
                url=url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=external_http_timeout(self.settings.deepseek_timeout_seconds),
                provider_hint="codegen.agent_loop.deepseek",
            )
            response.raise_for_status()
            data = response.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {}) or {}
            content = str(message.get("content", ""))
            reasoning = str(message.get("reasoning_content", "") or "")
            usage = data.get("usage", {}) or {}
            logger.warning(
                "agent_loop deepseek call: prompt_msgs=%d content_bytes=%d "
                "reasoning_bytes=%d finish=%s prompt_tokens=%s completion_tokens=%s "
                "total_tokens=%s",
                len(messages),
                len(content),
                len(reasoning),
                choice.get("finish_reason"),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )
            return content, reasoning
        except httpx.HTTPError as exc:
            raise CodegenError(f"DeepSeek API error in agent loop: {exc}") from exc

    def _agent_llm_call_openai(
        self, system_prompt: str, messages: list[dict[str, str]]
    ) -> tuple[str, str]:
        if not self.settings.openai_api_key:
            raise CodegenError("OPS_AGENT_OPENAI_API_KEY is not configured.")
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self._resolve_model_name("openai"),
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": 0.0,
            "max_tokens": 4096,
        }
        try:
            response = cached_http_post(
                url=url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=external_http_timeout(
                    getattr(self.settings, "primary_agent_timeout_seconds", 90)
                ),
                provider_hint="codegen.agent_loop.openai",
            )
            response.raise_for_status()
            data = response.json()
            content = str(
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            # OpenAI doesn't return reasoning_content; second tuple
            # element stays empty.
            return content, ""
        except httpx.HTTPError as exc:
            raise CodegenError(f"OpenAI API error in agent loop: {exc}") from exc


def _first_line(content: str) -> str | None:
    if not content.strip():
        return None
    return content.splitlines()[0]


_TERMINAL_MARKER_RE = re.compile(
    r"##\s*(EVIDENCE_GAP|NO_CHANGE_NEEDED|PLAN_CONFLICT)\s*:?\s*(.*)",
    re.IGNORECASE,
)


def _extract_contract_coverage(content: str) -> dict[str, Any] | None:
    """v16.2: parse the model's ``## CONTRACT_COVERAGE`` JSON block out of
    its response. Returns the dict-form `CoverageDeclaration.to_dict()`,
    or None when the block is absent or unparseable.

    Implemented as a thin wrapper around
    ``contract_coverage.parse_coverage_block`` so the response parsers
    don't need to import the data classes directly. Caller treats None
    as "model didn't follow the protocol" when the plan carried
    required_contracts — the orchestrator's coverage gate then treats
    every required contract as missing (incomplete verdict, not lie).
    """
    if not content:
        return None
    try:
        from app.services.contract_coverage import parse_coverage_block
        decl = parse_coverage_block(content)
        if decl is None:
            return None
        return decl.to_dict()
    except Exception:  # noqa: BLE001
        return None


def _detect_terminal_marker(content: str) -> str | None:
    """Return a one-line description if the response starts with one of
    the playbook's terminal markers (EVIDENCE_GAP, NO_CHANGE_NEEDED,
    PLAN_CONFLICT). Returns None otherwise.

    The playbook (docs/agent-playbooks/codegen/diff-discipline.md) tells
    the model these are valid terminal outputs when it can't proceed.
    Treating them as parse errors and retrying just burns tokens — the
    model has already said it can't make a patch with what it has. The
    caller surfaces this as a non-retryable CodegenError so the
    orchestrator can take a different remediation step (expand context,
    re-plan, etc.) instead of looping in codegen.
    """
    if not content:
        return None
    head = content.lstrip().splitlines()[:3]
    for line in head:
        m = _TERMINAL_MARKER_RE.match(line.strip())
        if m:
            kind = m.group(1).upper()
            detail = m.group(2).strip()
            return f"{kind}: {detail[:240]}" if detail else kind
    return None


# Match the line that starts a NO_CHANGE_NEEDED section. Captures
# whatever follows on the line so we can also accept the legacy
# ``## NO_CHANGE_NEEDED: <reason>`` format when there is no JSON.
_NO_CHANGE_HEADER_RE = re.compile(
    r"^[ \t]*##[ \t]*NO_CHANGE_NEEDED[ \t]*:?[ \t]*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_no_change_payload(content: str) -> dict | None:
    """Extract the structured payload following a ``## NO_CHANGE_NEEDED``
    marker. Returns ``{"reason": str, "evidence": [...]}`` when a JSON
    block is present, ``{"reason": str, "evidence": []}`` for the legacy
    inline-reason form, and ``None`` when the marker itself is missing.

    Tolerant to:
    - JSON optionally wrapped in triple-backtick json fences
    - Reason inlined on the marker line (legacy form) with no JSON body
    - Extra prose after the JSON block (truncate at next ``##`` heading)

    NEVER raises — malformed JSON returns ``{"reason": "", "evidence": []}``
    so the caller treats it as PHANTOM_NO_CHANGE rather than blowing up.
    """
    if not content:
        return None
    text = content.strip()
    header = _NO_CHANGE_HEADER_RE.search(text)
    if not header:
        return None
    inline_reason = header.group(1).strip()
    after = text[header.end():].lstrip()
    # Stop at the next top-level ``##`` heading so we don't swallow
    # unrelated sections.
    next_header = re.search(r"^\s*##\s+\S", after, re.MULTILINE)
    if next_header:
        after = after[: next_header.start()].rstrip()

    # Strip a possible code fence wrapping the JSON body.
    fenced = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", after, re.DOTALL)
    body = fenced.group(1).strip() if fenced else after.strip()

    payload: dict = {"reason": inline_reason, "evidence": []}
    if body and body.startswith("{"):
        try:
            import json as _json

            parsed = _json.loads(body)
            if isinstance(parsed, dict):
                reason = parsed.get("reason")
                if isinstance(reason, str) and reason.strip():
                    payload["reason"] = reason.strip()
                raw_evidence = parsed.get("evidence")
                if isinstance(raw_evidence, list):
                    cleaned: list[dict] = []
                    for entry in raw_evidence:
                        if not isinstance(entry, dict):
                            continue
                        file_path = str(
                            entry.get("file_path") or entry.get("file") or ""
                        ).strip()
                        quote = str(entry.get("quote") or "").strip()
                        claim = str(entry.get("claim") or "").strip()
                        if file_path or quote or claim:
                            cleaned.append(
                                {"file_path": file_path, "quote": quote, "claim": claim}
                            )
                    payload["evidence"] = cleaned
        except Exception:  # noqa: BLE001
            # Malformed JSON: keep the inline reason if any, no evidence.
            pass
    return payload


def _classify_no_change(
    content: str, context_files: dict[str, str] | None
) -> dict | None:
    """Classify a NO_CHANGE_NEEDED response as ``verified`` or ``phantom``.

    Returns ``None`` when the response is not a NO_CHANGE_NEEDED marker
    (caller should fall through to other handling). Otherwise returns::

        {
            "kind": "verified" | "phantom",
            "reason": str,
            "evidence_count": int,
            "verification": [VerifiedEvidence, ...],
            "phantom_summary": str,  # human-readable, surfaced as the
                                     # retry-feedback last_error
        }

    Phantom triggers when ANY of:
    - No evidence list at all (legacy inline-reason form)
    - evidence is empty
    - any evidence item's quote fails verification against the file
      content the model was shown (``context_files``)

    Verified requires at least one evidence item AND every item's quote
    to verify against the file the model was given.
    """
    payload = _parse_no_change_payload(content)
    if payload is None:
        return None

    from app.services.quote_verifier import verify_evidence_quotes

    reason = payload.get("reason") or ""
    evidence = payload.get("evidence") or []

    if not evidence:
        return {
            "kind": "phantom",
            "reason": reason,
            "evidence_count": 0,
            "verification": [],
            "phantom_summary": (
                "PHANTOM_NO_CHANGE: claim has no evidence quotes. "
                "When emitting NO_CHANGE_NEEDED you MUST include a JSON "
                "block with `evidence` listing real quotes copied from "
                "the file you reviewed: "
                "## NO_CHANGE_NEEDED\\n"
                "{\\\"reason\\\": \\\"...\\\", \\\"evidence\\\": "
                "[{\\\"file_path\\\": \\\"...\\\", \\\"claim\\\": \\\"...\\\", "
                "\\\"quote\\\": \\\"exact text from file\\\"}]}"
            ),
        }

    files_map = dict(context_files or {})
    verification = verify_evidence_quotes(file_to_source=files_map, claims=evidence)
    failures = [v for v in verification if not v.matched]
    if failures:
        details = "; ".join(
            f"file={(v.file_path or '?')!s} quote_preview={(v.quote_preview or '')!r} reason={v.reason}"
            for v in failures[:4]
        )
        summary = (
            f"PHANTOM_NO_CHANGE: {len(failures)} of {len(verification)} "
            f"evidence quote(s) could not be verified against the file "
            f"content you were shown. Failures: {details}. "
            "Either (a) generate the required patch instead of claiming "
            "no change, or (b) re-read the file and provide EXACT quotes "
            "(copy whole lines, do not paraphrase)."
        )
        return {
            "kind": "phantom",
            "reason": reason,
            "evidence_count": len(verification),
            "verification": verification,
            "phantom_summary": summary,
        }

    return {
        "kind": "verified",
        "reason": reason,
        "evidence_count": len(verification),
        "verification": verification,
        "phantom_summary": "",
    }


def _format_no_change_terminal_message(
    classification: dict, *, default_marker_detail: str
) -> str:
    """Produce the ``codegen_terminal:`` error message string.

    Verified: ``codegen_terminal: NO_CHANGE_NEEDED_VERIFIED: <reason>``
    Phantom:  ``codegen_terminal: PHANTOM_NO_CHANGE: <summary>``

    Both are wrapped in CodegenError; the verified flavor is
    non-retryable (the model gave honest evidence — retrying won't help)
    while the phantom flavor is retryable so ``_is_retryable_codegen_error``
    picks it up and the retry feedback includes the failure summary.
    """
    if classification.get("kind") == "verified":
        reason = (classification.get("reason") or default_marker_detail)[:240]
        return f"codegen_terminal: NO_CHANGE_NEEDED_VERIFIED: {reason}"
    summary = (
        classification.get("phantom_summary")
        or "PHANTOM_NO_CHANGE: no verifiable evidence quotes"
    )
    return f"codegen_terminal: {summary[:1000]}"


def _is_minimal_edit_retryable_error_message(message: str) -> bool:
    lowered = message.lower()
    return "new file mode" in lowered or "l5" in lowered


def _is_retryable_codegen_error(exc: CodegenError) -> bool:
    message = str(exc).lower()
    if "codegen_terminal" in message:
        return False
    # v15 Ticket 2A: PHANTOM_NO_CHANGE is a quote-verification failure —
    # the model claimed the file already implements the feature but the
    # quotes don't exist. This is a fixable mistake (the model can either
    # write the patch or quote real lines), so retry with feedback.
    if "phantom_no_change" in message:
        return True
    # Playbook-sanctioned terminal markers are explicit "I can't proceed"
    # signals; retrying changes nothing. Hard-no retry. Includes
    # NO_CHANGE_NEEDED_VERIFIED (model gave honest evidence; the patch
    # would be wrong) and the legacy un-classified NO_CHANGE_NEEDED.
    return any(
        key in message
        for key in (
            "valid unified diff",
            "changed file headers",
            "json",
            "no files",
            "missing path",
            "empty output",
            "new file mode",
            "l5",
            "aider blocks could not be parsed",
            "aider apply failed",
            "produced no diff",
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
