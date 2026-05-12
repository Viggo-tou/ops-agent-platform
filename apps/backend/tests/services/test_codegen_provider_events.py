from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import CodegenResult  # noqa: E402
from app.services.codegen import CodeGenerator, CodegenError  # noqa: E402


def _settings(provider: str = "mock") -> SimpleNamespace:
    return SimpleNamespace(
        primary_agent_provider=provider,
        codegen_provider=provider,
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
        internal_api_retry_count=0,
        internal_db_url=None,
        internal_db_timeout_seconds=8.0,
        internal_db_retry_count=0,
        claude_code_command="npx",
        claude_code_args="--print",
        claude_code_timeout_seconds=30.0,
        cli_max_retries=0,
        codex_command="codex",
        codex_timeout_seconds=30.0,
    )


def test_attempt_history_on_single_provider_success() -> None:
    result = CodeGenerator(_settings("mock")).generate_patch(
        task_id="t1",
        plan_json={"objective": "x", "steps": []},
        context_files={"app/example.py": "print('hello')\n"},
    )

    assert result.attempt_history == [{"provider": "mock", "status": "succeeded"}]


def test_attempt_history_records_failed_providers_then_success(monkeypatch) -> None:
    generator = CodeGenerator(_settings("mock"))

    monkeypatch.setattr(generator, "_resolve_provider_chain", lambda: ["codex", "mock"])

    def _try_provider(**kwargs):
        provider = kwargs["provider"]
        if provider == "codex":
            raise CodegenError("503 service unavailable")
        return CodegenResult(
            diff="diff --git a/app/example.py b/app/example.py\n--- a/app/example.py\n+++ b/app/example.py\n@@ -1 +1 @@\n-old\n+new\n",
            summary="Updated example",
            files_changed=["app/example.py"],
            provider_name="mock",
        )

    monkeypatch.setattr(generator, "_try_provider", _try_provider)

    result = generator.generate_patch(
        task_id="t1",
        plan_json={"objective": "x", "steps": []},
        context_files={},
    )

    assert result.attempt_history == [
        {"provider": "codex", "status": "failed", "error": "503 service unavailable"},
        {"provider": "mock", "status": "succeeded"},
    ]
