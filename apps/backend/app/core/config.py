from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Ops Agent Platform API"
    debug: bool = True
    api_prefix: str = "/api"
    database_url: str = f"sqlite:///{(BASE_DIR / 'ops_agent_platform.db').as_posix()}"
    primary_agent_provider: Literal["auto", "mock", "openai", "minimax", "anthropic", "deepseek", "ollama", "claude_code", "codex"] = "auto"
    planner_provider: Literal["auto", "claude_code", "anthropic", "openai", "minimax", "mock"] | None = None
    codegen_provider: Literal["auto", "claude_code", "codex", "anthropic", "openai", "minimax", "deepseek", "ollama", "mock"] | None = None
    primary_agent_model: str = "gpt-4o-mini"
    primary_agent_timeout_seconds: float = 30.0
    minimax_planner_timeout_seconds: float = 90.0
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-coder"
    deepseek_timeout_seconds: float = 120.0
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen3.5"
    ollama_timeout_seconds: float = 600.0
    ollama_max_context_files: int = 2
    ollama_max_file_chars: int = 8000
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-20250514"
    # Claude Code CLI (planner)
    claude_code_command: str = "npx"
    claude_code_args: str = "--yes @anthropic-ai/claude-code"
    claude_code_timeout_seconds: float = 300.0
    claude_code_git_bash_path: str | None = None
    # Codex CLI (codegen)
    codex_command: str = "codex"
    codex_timeout_seconds: float = 300.0
    codex_model: str = "gpt-5.4"
    # CLI retry: retry codegen/planning subprocess on timeout or transient failure
    cli_max_retries: int = 1
    gate_repair_max_attempts: int = 1
    gate_repair_timeout_seconds: float = 300.0
    semantic_translator_provider: Literal["auto", "mock", "minimax"] = "auto"
    semantic_translator_model: str = "MiniMax-M2.7"
    semantic_translator_timeout_seconds: float = 30.0
    minimax_api_key: str | None = None
    minimax_base_url: str = "https://api.minimaxi.com"
    knowledge_source_name: str = "handymanapp"
    knowledge_source_path: str | None = None
    knowledge_source_specs: str | None = None
    knowledge_top_k: int = 4
    knowledge_max_file_bytes: int = 120_000
    knowledge_upload_root: str = str((BASE_DIR / "data" / "uploads").as_posix())
    knowledge_upload_default_source: str = "uploads"
    knowledge_upload_max_bytes: int = 2_000_000
    tool_permission_overrides: str | None = None
    tool_default_timeout_seconds: float = 15.0
    tool_default_retry_count: int = 1
    sandbox_base_dir: str = "data/sandboxes"
    sandbox_clone_timeout_seconds: float = 120.0
    sandbox_command_timeout_seconds: float = 60.0
    sandbox_max_output_bytes: int = 65536
    alert_webhook_url: str | None = None
    slack_base_url: str = "https://slack.com"
    slack_bot_token: str | None = None
    slack_default_channel: str | None = None
    slack_post_message_timeout_seconds: float = 10.0
    slack_post_message_retry_count: int = 1
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_bearer_token: str | None = None
    jira_project_key: str | None = None
    jira_issue_type: str = "Task"
    jira_timeout_seconds: float = 15.0
    jira_retry_count: int = 1
    # T-039: require human approval between code-generation-pass and
    # jira.transition_issue writeback. When True, the develop pipeline
    # pauses in AWAITING_APPROVAL after spec_conformance.attest pass,
    # exposing the diff + goal_attestation in the approval record, and
    # only transitions Jira once the approval is granted.
    develop_require_jira_approval: bool = True
    internal_api_base_url: str | None = None
    internal_api_token: str | None = None
    internal_api_auth_header: str = "Authorization"
    internal_api_timeout_seconds: float = 10.0
    internal_api_retry_count: int = 1
    internal_db_url: str | None = None
    internal_db_timeout_seconds: float = 8.0
    internal_db_retry_count: int = 0
    internal_db_max_rows: int = 50
    frontend_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_prefix="OPS_AGENT_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.knowledge_source_specs and not settings.knowledge_source_path:
        default_candidates = [
            Path(r"D:\项目\HostedDashboard\handyman-admin-dashboard"),
            Path(r"D:\项目\HandymanApp-master"),
            Path(r"C:\Users\Tomonkyo\handyman-agent-system"),
        ]
        for candidate in default_candidates:
            if candidate.exists():
                settings.knowledge_source_path = str(candidate)
                break
    return settings
