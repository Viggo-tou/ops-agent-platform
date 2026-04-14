from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import CodegenResult  # noqa: E402
from app.core.enums import ToolPermissionCategory  # noqa: E402
from app.services.codegen import (  # noqa: E402
    CODEGEN_SYSTEM_PROMPT,
    CODEGEN_SYSTEM_PROMPT_JSON_MODE,
    CodeGenerator,
    CodegenError,
)
from app.tools.registry import ToolRegistry  # noqa: E402


def _settings(provider: str = "mock") -> SimpleNamespace:
    return SimpleNamespace(
        primary_agent_provider=provider,
        primary_agent_model="gpt-4o-mini",
        primary_agent_timeout_seconds=30.0,
        minimax_api_key=None,
        minimax_base_url="https://api.minimaxi.com",
        minimax_planner_timeout_seconds=90.0,
        semantic_translator_model="MiniMax-Text-01",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        anthropic_api_key=None,
        anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-sonnet-4-20250514",
        tool_permission_overrides=None,
        tool_default_timeout_seconds=15.0,
        sandbox_command_timeout_seconds=60.0,
        slack_bot_token=None,
        slack_post_message_timeout_seconds=10.0,
        slack_post_message_retry_count=1,
        jira_base_url=None,
        jira_api_token=None,
        jira_bearer_token=None,
        jira_timeout_seconds=15.0,
        jira_retry_count=1,
        internal_api_base_url=None,
        internal_api_timeout_seconds=10.0,
        internal_api_retry_count=1,
        internal_db_url=None,
        internal_db_timeout_seconds=8.0,
        internal_db_retry_count=0,
    )


def _diff_for(path: str = "app/example.py") -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 old line
+new line
"""


class CodeGeneratorTests(unittest.TestCase):
    def test_mock_generate_produces_valid_diff(self) -> None:
        result = CodeGenerator(_settings()).generate_patch(
            task_id="task-1",
            plan_json={
                "objective": "Add a generated comment.",
                "affected_code_locations": [{"relative_path": "app/example.py"}],
            },
            context_files={"app/example.py": "print('hello')\n"},
        )

        self.assertTrue(result.diff.startswith("diff --git"))
        self.assertEqual(result.files_changed, ["app/example.py"])

    def test_mock_generate_no_context_files_raises(self) -> None:
        with self.assertRaises(CodegenError):
            CodeGenerator(_settings()).generate_patch(
                task_id="task-1",
                plan_json={"objective": "No context"},
                context_files={},
            )

    def test_parse_response_valid_diff(self) -> None:
        result = CodeGenerator(_settings())._parse_response(
            _diff_for(),
            provider_name="mock",
            model_name="mock",
            input_tokens=10,
            output_tokens=20,
        )

        self.assertEqual(result.files_changed, ["app/example.py"])
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.output_tokens, 20)

    def test_parse_response_with_code_fences(self) -> None:
        diff = _diff_for()
        result = CodeGenerator(_settings())._parse_response(
            f"```diff\n{diff}```",
            provider_name="mock",
            model_name="mock",
            input_tokens=0,
            output_tokens=0,
        )

        self.assertEqual(result.diff, diff.strip())
        self.assertEqual(result.files_changed, ["app/example.py"])

    def test_parse_response_extracts_diff_from_preamble(self) -> None:
        diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        result = CodeGenerator(_settings())._parse_response(
            f"Here is the diff:\n{diff}",
            provider_name="mock",
            model_name="mock",
            input_tokens=0,
            output_tokens=0,
        )

        self.assertEqual(result.diff, diff.strip())
        self.assertEqual(result.files_changed, ["x"])

    def test_parse_response_invalid_content(self) -> None:
        with self.assertRaises(CodegenError):
            CodeGenerator(_settings())._parse_response(
                "I changed the file.",
                provider_name="mock",
                model_name="mock",
                input_tokens=0,
                output_tokens=0,
            )

    def test_build_prompt_includes_plan_and_files(self) -> None:
        prompt = CodeGenerator(_settings())._build_prompt(
            plan_json={
                "objective": "Update the greeting.",
                "affected_code_locations": [{"relative_path": "app/example.py"}],
            },
            context_files={"app/example.py": "print('hello')\n"},
            task_description="Make the greeting friendlier.",
        )

        self.assertIn("Update the greeting.", prompt)
        self.assertIn("app/example.py", prompt)
        self.assertIn("print('hello')", prompt)

    def test_generate_diff_from_files(self) -> None:
        diff, files_changed = CodeGenerator(_settings())._generate_diff_from_files(
            {"app/example.py": "old line\n"},
            [{"path": "app/example.py", "content": "new line\n"}],
        )

        self.assertTrue(diff.startswith("diff --git a/app/example.py b/app/example.py\n"))
        self.assertIn("--- a/app/example.py\n", diff)
        self.assertIn("+++ b/app/example.py\n", diff)
        self.assertIn("@@ -1 +1 @@\n", diff)
        self.assertIn("-old line\n", diff)
        self.assertIn("+new line\n", diff)
        self.assertEqual(files_changed, ["app/example.py"])

    def test_generate_diff_no_changes(self) -> None:
        with self.assertRaisesRegex(CodegenError, "no files with changes"):
            CodeGenerator(_settings())._generate_diff_from_files(
                {"app/example.py": "same line\n"},
                [{"path": "app/example.py", "content": "same line\n"}],
            )

    def test_parse_json_codegen_response_valid(self) -> None:
        files = CodeGenerator(_settings())._parse_json_codegen_response(
            '{"files":[{"path":"app/example.py","content":"print(1)\\n","summary":"Update example"}]}'
        )

        self.assertEqual(files[0]["path"], "app/example.py")
        self.assertEqual(files[0]["content"], "print(1)\n")

    def test_parse_json_codegen_response_with_fences(self) -> None:
        files = CodeGenerator(_settings())._parse_json_codegen_response(
            '```json\n{"files":[{"path":"app/example.py","content":"print(1)\\n"}]}\n```'
        )

        self.assertEqual(files[0]["path"], "app/example.py")
        self.assertEqual(files[0]["content"], "print(1)\n")

    def test_parse_json_codegen_response_empty_files(self) -> None:
        with self.assertRaisesRegex(CodegenError, "no files"):
            CodeGenerator(_settings())._parse_json_codegen_response('{"files":[]}')

    def test_minimax_uses_json_mode_prompt(self) -> None:
        settings = _settings("minimax")
        settings.minimax_api_key = "minimax-test"
        settings.primary_agent_model = ""
        generator = CodeGenerator(settings)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"files":[{"path":"app/example.py",'
                            '"content":"print(\\"hello\\")\\nprint(\\"bye\\")\\n",'
                            '"summary":"Add goodbye print"}]}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

        with patch("app.services.codegen.httpx.post", return_value=response) as post:
            result = generator.generate_patch(
                task_id="task-1",
                plan_json={
                    "objective": "Add another print.",
                    "affected_code_locations": [{"relative_path": "app/example.py"}],
                },
                context_files={"app/example.py": 'print("hello")\n'},
            )

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["messages"][0]["content"], CODEGEN_SYSTEM_PROMPT_JSON_MODE)
        self.assertNotEqual(body["messages"][0]["content"], CODEGEN_SYSTEM_PROMPT)
        self.assertIn("Return only valid JSON", body["messages"][1]["content"])
        self.assertNotIn("Return only the diff", body["messages"][1]["content"])
        self.assertTrue(result.diff.startswith("diff --git a/app/example.py b/app/example.py\n"))
        self.assertIn('+print("bye")\n', result.diff)
        self.assertEqual(result.files_changed, ["app/example.py"])
        self.assertEqual(result.provider_name, "minimax")
        self.assertEqual(result.input_tokens, 3)
        self.assertEqual(result.output_tokens, 4)

    def test_codegen_result_schema(self) -> None:
        result = CodegenResult(
            diff=_diff_for(),
            summary="Generated patch modifying 1 file.",
            files_changed=["app/example.py"],
            provider_name="mock",
            model_name="mock",
            input_tokens=1,
            output_tokens=2,
        )

        self.assertEqual(result.files_changed, ["app/example.py"])
        self.assertEqual(result.provider_name, "mock")

    def test_tool_registered(self) -> None:
        definition = ToolRegistry(_settings()).get_definition("codegen.generate_patch")

        # Under the current auto-approve tool policy, codegen.generate_patch is WRITE, not APPROVAL_REQUIRED.
        self.assertEqual(definition.permission_category, ToolPermissionCategory.WRITE)
        self.assertIn("codegen", definition.tags)

    def test_resolve_provider_anthropic_auto(self) -> None:
        settings = _settings("auto")
        settings.anthropic_api_key = "sk-test"

        # Auto mode now returns a chain; the first entry is the preferred provider.
        self.assertEqual(CodeGenerator(settings)._resolve_provider_chain()[0], "anthropic")

    def test_resolve_provider_anthropic_explicit(self) -> None:
        self.assertEqual(CodeGenerator(_settings("anthropic"))._resolve_provider_chain(), ["anthropic"])

    def test_resolve_provider_auto_prefers_anthropic_over_minimax(self) -> None:
        settings = _settings("auto")
        settings.anthropic_api_key = "sk-test"
        settings.minimax_api_key = "minimax-test"

        chain = CodeGenerator(settings)._resolve_provider_chain()
        self.assertEqual(chain[0], "anthropic")
        self.assertIn("minimax", chain)
        self.assertLess(chain.index("anthropic"), chain.index("minimax"))

    def test_call_anthropic_no_key_raises(self) -> None:
        settings = _settings("anthropic")

        with self.assertRaisesRegex(CodegenError, "not configured"):
            CodeGenerator(settings).generate_patch(
                task_id="task-1",
                plan_json={"objective": "Update the greeting."},
                context_files={"app/example.py": "print('hello')\n"},
            )

    def test_retry_on_invalid_diff(self) -> None:
        generator = CodeGenerator(_settings("minimax"))
        calls = []

        def fake_call(prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
            del context_files
            calls.append(prompt)
            if len(calls) == 1:
                raise CodegenError("MiniMax JSON response could not be parsed: bad json")
            return generator._parse_response(
                _diff_for(),
                provider_name="minimax",
                model_name="mock",
                input_tokens=1,
                output_tokens=2,
            )

        generator._call_minimax = fake_call  # type: ignore[method-assign]

        result = generator.generate_patch(
            task_id="task-1",
            plan_json={
                "objective": "Update the greeting.",
                "affected_code_locations": [{"relative_path": "app/example.py"}],
            },
            context_files={"app/example.py": "old line\n"},
        )

        self.assertEqual(result.files_changed, ["app/example.py"])
        self.assertEqual(len(calls), 2)
        self.assertIn("PREVIOUS ATTEMPT FAILED", calls[1])
        self.assertIn("valid JSON", calls[1])

    def test_retry_exhausted(self) -> None:
        generator = CodeGenerator(_settings("minimax"))
        calls = []

        def fake_call(prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
            del context_files
            calls.append(prompt)
            raise CodegenError("MiniMax JSON response contains no files.")

        generator._call_minimax = fake_call  # type: ignore[method-assign]

        with self.assertRaisesRegex(CodegenError, "after 3 attempts"):
            generator.generate_patch(
                task_id="task-1",
                plan_json={
                    "objective": "Update the greeting.",
                    "affected_code_locations": [{"relative_path": "app/example.py"}],
                },
                context_files={"app/example.py": "old line\n"},
            )

        self.assertEqual(len(calls), 3)

    def test_api_error_not_retried(self) -> None:
        generator = CodeGenerator(_settings("minimax"))
        calls = []

        def fake_call(prompt: str, *, context_files: dict[str, str]) -> CodegenResult:
            del context_files
            calls.append(prompt)
            raise CodegenError("API error")

        generator._call_minimax = fake_call  # type: ignore[method-assign]

        with self.assertRaisesRegex(CodegenError, "API error"):
            generator.generate_patch(
                task_id="task-1",
                plan_json={
                    "objective": "Update the greeting.",
                    "affected_code_locations": [{"relative_path": "app/example.py"}],
                },
                context_files={"app/example.py": "old line\n"},
            )

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
