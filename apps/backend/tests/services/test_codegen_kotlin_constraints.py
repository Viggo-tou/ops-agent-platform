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
