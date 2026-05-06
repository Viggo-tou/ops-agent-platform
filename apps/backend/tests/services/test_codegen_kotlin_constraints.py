"""Stage B1: codegen prompt augmentation for Kotlin context."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codegen import (  # noqa: E402
    CODEGEN_KOTLIN_GUIDANCE,
    CODEGEN_SYSTEM_PROMPT,
    CodeGenerator,
)


def test_kotlin_guidance_appended_when_kt_in_context():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"app/src/main/java/Foo.kt": "package x"},
    )
    assert CODEGEN_KOTLIN_GUIDANCE in out
    assert out.startswith(CODEGEN_SYSTEM_PROMPT)


def test_kotlin_guidance_appended_when_kts_in_context():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"build.gradle.kts": "plugins {}"},
    )
    assert CODEGEN_KOTLIN_GUIDANCE in out


def test_kotlin_guidance_NOT_appended_when_no_kt_files():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"foo.py": "x = 1", "bar.js": "var y = 2"},
    )
    assert CODEGEN_KOTLIN_GUIDANCE not in out
    assert out == CODEGEN_SYSTEM_PROMPT


def test_kotlin_guidance_NOT_appended_when_context_empty():
    out = CodeGenerator._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT, {})
    assert CODEGEN_KOTLIN_GUIDANCE not in out


def test_kotlin_guidance_NOT_appended_when_context_none():
    out = CodeGenerator._augment_prompt_for_kotlin(CODEGEN_SYSTEM_PROMPT, None)
    assert CODEGEN_KOTLIN_GUIDANCE not in out


def test_kotlin_guidance_appended_when_mixed_context_includes_kt():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {
            "foo.py": "x = 1",
            "src/main/java/Bar.kt": "package x",
            "baz.xml": "<root/>",
        },
    )
    assert CODEGEN_KOTLIN_GUIDANCE in out


def test_kotlin_guidance_contains_expected_constraints():
    """The guidance text should mention the 3 recurring Kotlin bugs."""
    g = CODEGEN_KOTLIN_GUIDANCE.lower()
    assert "import" in g
    assert "@composable" in g or "annotation" in g
    assert "{" in CODEGEN_KOTLIN_GUIDANCE
    assert "hunk" in g


# --- L4a: import-preservation rule emitted for any Kotlin context ----------

def test_l4a_import_preservation_rule_added_for_kt():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"src/main/Foo.kt": "package x\nimport a.B\nclass Foo"},
    )
    assert "IMPORT-PRESERVATION RULE" in out
    assert "L4a" in out


def test_l4a_NOT_added_when_no_kotlin_files():
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT, {"foo.py": "x = 1"},
    )
    assert "IMPORT-PRESERVATION RULE" not in out
    assert "L4a" not in out


# --- L4b: Compose-aware scope rule -----------------------------------------

def test_l4b_compose_rules_added_when_at_composable_present():
    """When ANY context file uses @Composable, the prompt gains the
    Compose-scope clarification."""
    src = (
        "package x\n"
        "import androidx.compose.runtime.Composable\n"
        "@Composable\n"
        "fun MyScreen() { Text(\"hi\") }\n"
    )
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"src/main/MyScreen.kt": src},
    )
    assert "COMPOSE SCOPE RULES" in out
    assert "L4b" in out
    # Specific guidance lifted from real v26 failure
    assert "viewModel()" in out


def test_l4b_NOT_added_when_kotlin_lacks_composable():
    """A regular Kotlin file (no Compose) does NOT get the scope block."""
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"src/main/Util.kt": "package x\nclass Util { fun foo() {} }"},
    )
    assert "COMPOSE SCOPE RULES" not in out
    # L4a still present (applies to any Kotlin)
    assert "IMPORT-PRESERVATION RULE" in out


def test_l4b_added_when_only_one_context_file_uses_compose():
    """Multi-file context: as long as ONE file mentions @Composable,
    the rules apply (since the diff might touch any of them)."""
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {
            "src/main/Util.kt": "class Util",  # no Compose
            "src/main/Screen.kt": "@Composable fun S() {}",  # has Compose
        },
    )
    assert "COMPOSE SCOPE RULES" in out


# --- L4d: cross-file naming consistency ------------------------------------

def test_l4d_added_for_multi_kotlin_context():
    """When >= 2 .kt files in context, append the cross-file naming rule
    (the v27 oscillation fix)."""
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {
            "src/main/Job.kt": "data class Job(val jobLocation: String)",
            "src/main/JobPostingFragment.kt": "class F { val x = job.jobLocation }",
        },
    )
    assert "CROSS-FILE NAMING CONSISTENCY" in out
    assert "L4d" in out
    # Specific symbols from the v27 failure should appear
    assert "jobLocation" in out


def test_l4d_NOT_added_for_single_kotlin_file():
    """Single .kt file context shouldn't trigger the multi-file rule —
    avoids prompt bloat for small single-file edits."""
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {"src/main/Foo.kt": "package x\nclass Foo"},
    )
    assert "CROSS-FILE NAMING CONSISTENCY" not in out
    # L4a still applies (always for any Kotlin)
    assert "IMPORT-PRESERVATION RULE" in out


def test_l4d_lists_paths_in_prompt():
    """The prompt should reference the actual file paths so the LLM knows
    which files are in scope for cross-checking."""
    out = CodeGenerator._augment_prompt_for_kotlin(
        CODEGEN_SYSTEM_PROMPT,
        {
            "app/src/main/A.kt": "class A",
            "app/src/main/B.kt": "class B",
        },
    )
    assert "app/src/main/A.kt" in out
    assert "app/src/main/B.kt" in out
