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
    primary_agent_provider: Literal["auto", "mock", "openai", "minimax"] = "auto"
    primary_agent_model: str = "gpt-4o-mini"
    primary_agent_timeout_seconds: float = 30.0
    minimax_planner_timeout_seconds: float = 90.0
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
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
    tool_permission_overrides: str | None = None
    tool_default_timeout_seconds: float = 15.0
    tool_default_retry_count: int = 1
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
        env_file=".env",
        env_prefix="OPS_AGENT_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.knowledge_source_specs and not settings.knowledge_source_path:
        default_candidates = [
            Path(r"D:\项目\HandymanApp-master"),
            Path(r"C:\Users\Tomonkyo\handyman-agent-system"),
        ]
        for candidate in default_candidates:
            if candidate.exists():
                settings.knowledge_source_path = str(candidate)
                break
    return settings
