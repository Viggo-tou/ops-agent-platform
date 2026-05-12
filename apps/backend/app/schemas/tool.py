from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.enums import ToolExecutionStatus, ToolPermissionCategory


class ToolRegistryEntryRead(BaseModel):
    name: str
    display_name: str
    description: str
    provider_name: str
    permission_category: ToolPermissionCategory
    enabled: bool
    status_message: str
    missing_configuration: list[str]
    requires_network: bool
    timeout_seconds: float
    retry_count: int
    tags: list[str]


class ToolExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    session_id: str | None = None
    approval_id: str | None = None
    tool_name: str
    provider_name: str
    permission_category: ToolPermissionCategory
    status: ToolExecutionStatus
    actor_name: str | None = None
    attempt_count: int
    max_retries: int
    timeout_seconds: float
    duration_ms: int | None = None
    request_payload_json: dict[str, Any] | None = None
    response_payload_json: dict[str, Any] | None = None
    attempt_log_json: list[Any] | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
