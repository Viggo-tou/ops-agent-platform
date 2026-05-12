from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import ToolExecutionStatus
from app.models.tool_execution import ToolExecution
from app.schemas.tool import ToolRegistryEntryRead
from app.tools.gateway import ToolGateway

router = APIRouter(prefix="/tools", tags=["tools"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/registry", response_model=list[ToolRegistryEntryRead])
def list_tool_registry(db: DbSession) -> list[ToolRegistryEntryRead]:
    gateway = ToolGateway(db)
    return gateway.list_registry_entries()


class McpToolDescriptor(BaseModel):
    name: str
    description: str
    input_schema: dict


class McpServerStatus(BaseModel):
    name: str
    connected: bool
    error: str | None = None
    tool_count: int
    tools: list[McpToolDescriptor]


@router.get("/mcp/servers", response_model=list[McpServerStatus])
def list_mcp_servers() -> list[McpServerStatus]:
    """Snapshot of every configured MCP server: connection state + tools."""
    from app.services.mcp_client import get_mcp_client

    client = get_mcp_client()
    states = client.list_servers()
    tools_by_server = client.list_tools()
    out: list[McpServerStatus] = []
    for name, state in states.items():
        server_tools = tools_by_server.get(name, [])
        out.append(
            McpServerStatus(
                name=name,
                connected=bool(state.get("connected")),
                error=state.get("error"),
                tool_count=int(state.get("tool_count") or 0),
                tools=[
                    McpToolDescriptor(
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        input_schema=t.get("input_schema") or {},
                    )
                    for t in server_tools
                ],
            )
        )
    return out


# --- Aggregated tool usage stats (for /tasks dashboard donut) ----------------


class ToolUsageItem(BaseModel):
    tool_name: str
    total: int
    succeeded: int
    failed: int
    success_rate: float


class ToolUsageStatsResponse(BaseModel):
    total_invocations: int
    succeeded: int
    failed: int
    success_rate: float
    window_days: int
    by_tool: list[ToolUsageItem]


@router.get("/usage-stats", response_model=ToolUsageStatsResponse)
def tool_usage_stats(
    db: DbSession,
    window_days: int = Query(default=7, ge=1, le=90),
    top: int = Query(default=10, ge=1, le=50),
) -> ToolUsageStatsResponse:
    """Aggregate tool execution outcomes for the past N days.

    Used by the task list dashboard donut. Counts only finished invocations
    (succeeded / failed / timed_out); skips still-running ones.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    finished_states = [
        ToolExecutionStatus.SUCCEEDED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.TIMED_OUT,
    ]

    rows = (
        db.query(
            ToolExecution.tool_name,
            ToolExecution.status,
            func.count(ToolExecution.id),
        )
        .filter(ToolExecution.started_at >= cutoff)
        .filter(ToolExecution.status.in_(finished_states))
        .group_by(ToolExecution.tool_name, ToolExecution.status)
        .all()
    )

    by_tool_acc: dict[str, dict[str, int]] = {}
    for tool_name, st, count in rows:
        acc = by_tool_acc.setdefault(tool_name, {"succeeded": 0, "failed": 0})
        if st == ToolExecutionStatus.SUCCEEDED:
            acc["succeeded"] += int(count)
        else:
            acc["failed"] += int(count)

    items: list[ToolUsageItem] = []
    total = succeeded = failed = 0
    for tool_name, acc in by_tool_acc.items():
        s = acc["succeeded"]
        f = acc["failed"]
        t = s + f
        rate = (s / t) if t else 0.0
        items.append(
            ToolUsageItem(
                tool_name=tool_name, total=t, succeeded=s, failed=f, success_rate=rate
            )
        )
        total += t
        succeeded += s
        failed += f

    items.sort(key=lambda i: i.total, reverse=True)
    items = items[:top]

    overall_rate = (succeeded / total) if total else 0.0
    return ToolUsageStatsResponse(
        total_invocations=total,
        succeeded=succeeded,
        failed=failed,
        success_rate=overall_rate,
        window_days=window_days,
        by_tool=items,
    )
