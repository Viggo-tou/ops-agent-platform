from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.enums import EventSource, EventType, RoleName, WorkflowStage


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    session_id: str | None = None
    event_type: EventType
    source: EventSource
    stage: WorkflowStage | None = None
    role: RoleName | None = None
    tool_name: str | None = None
    message: str
    payload_json: dict[str, Any] | None = None
    created_at: datetime
