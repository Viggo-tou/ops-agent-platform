"""Tests for Aider-format codegen dispatch (Tier 1.5).

Pins the format selection rules and the Aider parse path through
``CodeGenerator._parse_response``. Provider HTTP calls are out of
scope here — those are exercised in service-integration tests with
mocks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.services.codegen import (  # noqa: E402
    CODEGEN_SYSTEM_PROMPT,
    CODEGEN_STRUCTURAL_CODEGEN_SYSTEM_PROMPT,
    CODEGEN_SYSTEM_PROMPT_AIDER,
    CodeGenerator,
    CodegenError,
)
from app.agents.schemas import CodegenResult  # noqa: E402


def _settings(output_format: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(
        codegen_provider=None,
        codegen_output_format=output_format,
        codegen_structural_kotlin_enabled=True,
        primary_agent_provider="mock",
        primary_agent_model="mock",
        deepseek_api_key="deepseek-test",
        deepseek_model="deepseek-test",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
    )


# --- format resolution -------------------------------------------------------


def test_resolve_format_auto_deepseek_picks_aider():
    cg = CodeGenerator(_settings("auto"))
    assert cg._resolve_codegen_output_format("deepseek") == "aider_blocks"


def test_resolve_format_auto_openai_picks_aider():
    cg = CodeGenerator(_settings("auto"))
    assert cg._resolve_codegen_output_format("openai") == "aider_blocks"


def test_resolve_format_auto_claude_code_keeps_unified_diff():
    cg = CodeGenerator(_settings("auto"))
    assert cg._resolve_codegen_output_format("claude_code") == "unified_diff"


def test_resolve_format_auto_anthropic_keeps_unified_diff():
    cg = CodeGenerator(_settings("auto"))
    assert cg._resolve_codegen_output_format("anthropic") == "unified_diff"


def test_resolve_format_pin_unified_diff_overrides_auto_default():
    cg = CodeGenerator(_settings("unified_diff"))
    assert cg._resolve_codegen_output_format("deepseek") == "unified_diff"


def test_resolve_format_pin_aider_blocks_overrides_for_any_provider():
    cg = CodeGenerator(_settings("aider_blocks"))
    assert cg._resolve_codegen_output_format("claude_code") == "aider_blocks"


# --- system prompt selection ------------------------------------------------


def test_select_base_prompt_swaps_to_aider_when_format_active():
    cg = CodeGenerator(_settings("aider_blocks"))
    cg._active_codegen_output_format = "aider_blocks"
    assert cg._select_base_prompt(CODEGEN_SYSTEM_PROMPT) is CODEGEN_SYSTEM_PROMPT_AIDER


def test_select_base_prompt_passes_through_when_unified_diff():
    cg = CodeGenerator(_settings("unified_diff"))
    cg._active_codegen_output_format = "unified_diff"
    assert cg._select_base_prompt(CODEGEN_SYSTEM_PROMPT) is CODEGEN_SYSTEM_PROMPT


def test_select_base_prompt_does_not_touch_unrelated_base():
    cg = CodeGenerator(_settings("aider_blocks"))
    cg._active_codegen_output_format = "aider_blocks"
    other = "some other system prompt"
    assert cg._select_base_prompt(other) == other  # JSON mode etc unaffected


# --- parse dispatch ---------------------------------------------------------


def test_parse_response_aider_simple_replace():
    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "def f():\n    return 1\n"}

    aider_text = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "def f():\n"
        "    return 1\n"
        "=======\n"
        "def f():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    result = cg._parse_response(
        aider_text,
        provider_name="deepseek",
        model_name="deepseek-coder",
        input_tokens=10,
        output_tokens=20,
    )
    assert result.diff.startswith("diff --git a/m.py b/m.py")
    assert "+    return 2" in result.diff
    assert result.files_changed == ["m.py"]
    assert "Aider blocks" in result.summary


def test_generate_patch_uses_structural_kotlin_json_before_aider():
    source = """\
package demo

fun Screen() {
    val name = "Ada"
}
"""
    settings = _settings("auto")
    settings.primary_agent_provider = "deepseek"
    settings.codegen_provider = "deepseek"
    cg = CodeGenerator(settings)

    def fake_structural_call(prompt, model_name, *, system_prompt, purpose):
        assert model_name == "deepseek-test"
        assert system_prompt is CODEGEN_STRUCTURAL_CODEGEN_SYSTEM_PROMPT
        assert purpose == "codegen.structural_kotlin"
        assert "<allowed_file>" in prompt
        return (
            json.dumps(
                {
                    "status": "edit_plan",
                    "file": "Screen.kt",
                    "edits": [
                        {
                            "operation": "insert_after_anchor",
                            "anchor_line": 3,
                            "anchor_substring": "fun Screen() {",
                            "content": 'Text("Hello")',
                        }
                    ],
                }
            ),
            11,
            7,
        )

    def fail_raw_call(prompt):
        raise AssertionError("raw Aider/deepseek path should not run")

    cg._call_deepseek_text = fake_structural_call  # type: ignore[method-assign]
    cg._call_deepseek = fail_raw_call  # type: ignore[method-assign]

    result = cg.generate_patch(
        task_id="task-structural",
        plan_json={
            "objective": "Add greeting UI.",
            "must_touch_files": ["Screen.kt"],
        },
        context_files={"Screen.kt": source},
    )

    assert result.provider_name == "deepseek:structural_kotlin"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.files_changed == ["Screen.kt"]
    assert "+    Text(\"Hello\")" in result.diff


def test_generate_patch_falls_back_when_structural_kotlin_json_fails():
    source = "fun Screen() {\n    val name = \"Ada\"\n}\n"
    settings = _settings("auto")
    settings.primary_agent_provider = "deepseek"
    settings.codegen_provider = "deepseek"
    cg = CodeGenerator(settings)

    def bad_structural_call(prompt, model_name, *, system_prompt, purpose):
        return ("not json", 3, 2)

    def raw_fallback(prompt):
        return CodegenResult(
            diff=(
                "diff --git a/Screen.kt b/Screen.kt\n"
                "--- a/Screen.kt\n"
                "+++ b/Screen.kt\n"
                "@@ -1,3 +1,4 @@\n"
                " fun Screen() {\n"
                "+    Text(\"Hello\")\n"
                "     val name = \"Ada\"\n"
                " }\n"
            ),
            summary="fallback patch",
            files_changed=["Screen.kt"],
            provider_name="deepseek",
            model_name="deepseek-test",
        )

    cg._call_deepseek_text = bad_structural_call  # type: ignore[method-assign]
    cg._call_deepseek = raw_fallback  # type: ignore[method-assign]

    result = cg.generate_patch(
        task_id="task-fallback",
        plan_json={
            "objective": "Add greeting UI.",
            "must_touch_files": ["Screen.kt"],
        },
        context_files={"Screen.kt": source},
    )

    assert result.provider_name == "deepseek"
    assert "+    Text(\"Hello\")" in result.diff


def test_parse_response_aider_strips_code_fence():
    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "x\ny\nz\n"}

    fenced = (
        "```\n"
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "y\n"
        "=======\n"
        "Y\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    result = cg._parse_response(
        fenced,
        provider_name="deepseek",
        model_name="m",
        input_tokens=0,
        output_tokens=0,
    )
    assert "+Y" in result.diff


def test_parse_response_aider_anchor_not_found_raises_retryable():
    from app.services.codegen import _is_retryable_codegen_error

    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "x\n"}

    aider_text = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "no such region\n"
        "=======\n"
        "Y\n"
        ">>>>>>> REPLACE\n"
    )
    with pytest.raises(CodegenError) as excinfo:
        cg._parse_response(
            aider_text,
            provider_name="deepseek",
            model_name="m",
            input_tokens=0,
            output_tokens=0,
        )
    assert "Aider apply failed" in str(excinfo.value)
    assert _is_retryable_codegen_error(excinfo.value) is True


def test_parse_response_aider_garbage_input_raises_retryable():
    from app.services.codegen import _is_retryable_codegen_error

    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "x\n"}

    with pytest.raises(CodegenError) as excinfo:
        cg._parse_response(
            "this is not aider format at all",
            provider_name="deepseek",
            model_name="m",
            input_tokens=0,
            output_tokens=0,
        )
    assert "Aider blocks could not be parsed" in str(excinfo.value)
    assert _is_retryable_codegen_error(excinfo.value) is True


# --- terminal-marker handling (regression: 2026-05-10 task 1) ---------------


@pytest.mark.parametrize(
    "marker_text",
    [
        "## EVIDENCE_GAP: need full content of _arithmetic_mask method",
        "## NO_CHANGE_NEEDED: source already implements behaviour",
        "## PLAN_CONFLICT: must_touch and constraints contradict",
        "  ## evidence_gap: lowercase variant",  # case + leading whitespace
    ],
)
def test_parse_response_aider_terminal_marker_is_non_retryable(marker_text):
    from app.services.codegen import _is_retryable_codegen_error

    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "x = 1\n"}
    with pytest.raises(CodegenError) as excinfo:
        cg._parse_response(
            marker_text + "\n",
            provider_name="deepseek",
            model_name="m",
            input_tokens=0,
            output_tokens=0,
        )
    assert "codegen_terminal" in str(excinfo.value)
    # Critical: no 3-attempt retry storm on these markers.
    assert _is_retryable_codegen_error(excinfo.value) is False


def test_parse_response_unified_diff_terminal_marker_is_non_retryable():
    from app.services.codegen import _is_retryable_codegen_error

    cg = CodeGenerator(_settings("unified_diff"))
    cg._active_codegen_output_format = "unified_diff"
    with pytest.raises(CodegenError) as excinfo:
        cg._parse_response(
            "## EVIDENCE_GAP: cannot see the function I need to edit\n",
            provider_name="anthropic",
            model_name="claude",
            input_tokens=0,
            output_tokens=0,
        )
    assert "codegen_terminal" in str(excinfo.value)
    assert _is_retryable_codegen_error(excinfo.value) is False


def test_parse_response_aider_marker_text_carries_through():
    cg = CodeGenerator(_settings())
    cg._active_codegen_output_format = "aider_blocks"
    cg._current_context_files = {"m.py": "x = 1\n"}
    with pytest.raises(CodegenError) as excinfo:
        cg._parse_response(
            "## EVIDENCE_GAP: need full content of _arithmetic_mask method\n",
            provider_name="deepseek",
            model_name="m",
            input_tokens=0,
            output_tokens=0,
        )
    msg = str(excinfo.value)
    assert "EVIDENCE_GAP" in msg
    assert "_arithmetic_mask" in msg


def test_parse_response_unified_diff_path_unaffected():
    """Sanity: when format is unified_diff (default), the historical
    parse path is preserved exactly — no Aider dispatch.
    """
    cg = CodeGenerator(_settings("unified_diff"))
    cg._active_codegen_output_format = "unified_diff"
    diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    result = cg._parse_response(
        diff, provider_name="anthropic", model_name="claude", input_tokens=0, output_tokens=0
    )
    assert result.files_changed == ["x"]
