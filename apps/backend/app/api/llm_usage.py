"""LLM token usage statistics.

GET /api/llm-usage/stats          — aggregate totals + breakdowns
GET /api/llm-usage/timeseries     — daily totals for charting (last N days)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import ActorContext, require_permission
from app.models.llm_usage import LlmUsage

router = APIRouter(prefix="/llm-usage", tags=["llm-usage"])
DbSession = Annotated[Session, Depends(get_db)]
ViewActorCtx = Annotated[ActorContext, Depends(require_permission("settings:view"))]


class ByModelItem(BaseModel):
    model_name: str
    provider_name: str
    invocations: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class ByPurposeItem(BaseModel):
    purpose: str
    invocations: int
    total_tokens: int
    estimated_cost_usd: float


class StatsResponse(BaseModel):
    window_days: int
    total_invocations: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: float
    by_model: list[ByModelItem]
    by_purpose: list[ByPurposeItem]


class TimeseriesPoint(BaseModel):
    date: str  # YYYY-MM-DD (UTC)
    total_tokens: int
    total_cost_usd: float
    invocations: int


class TimeseriesResponse(BaseModel):
    window_days: int
    points: list[TimeseriesPoint]


@router.get("/stats", response_model=StatsResponse)
def get_stats(
    db: DbSession,
    _actor: ViewActorCtx,
    window_days: int = Query(default=30, ge=1, le=365),
    top: int = Query(default=20, ge=1, le=100),
) -> StatsResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    overall = (
        db.query(
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.input_tokens), 0),
            func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0.0),
        )
        .filter(LlmUsage.created_at >= cutoff)
        .one()
    )
    inv, in_tok, out_tok, total_tok, cost = overall

    model_rows = (
        db.query(
            LlmUsage.model_name,
            LlmUsage.provider_name,
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.input_tokens), 0),
            func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0.0),
        )
        .filter(LlmUsage.created_at >= cutoff)
        .group_by(LlmUsage.model_name, LlmUsage.provider_name)
        .order_by(func.sum(LlmUsage.total_tokens).desc())
        .limit(top)
        .all()
    )

    purpose_rows = (
        db.query(
            LlmUsage.purpose,
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0.0),
        )
        .filter(LlmUsage.created_at >= cutoff)
        .group_by(LlmUsage.purpose)
        .order_by(func.sum(LlmUsage.total_tokens).desc())
        .all()
    )

    return StatsResponse(
        window_days=window_days,
        total_invocations=int(inv or 0),
        total_input_tokens=int(in_tok or 0),
        total_output_tokens=int(out_tok or 0),
        total_tokens=int(total_tok or 0),
        total_cost_usd=float(cost or 0.0),
        by_model=[
            ByModelItem(
                model_name=row[0],
                provider_name=row[1],
                invocations=int(row[2]),
                input_tokens=int(row[3]),
                output_tokens=int(row[4]),
                total_tokens=int(row[5]),
                estimated_cost_usd=float(row[6]),
            )
            for row in model_rows
        ],
        by_purpose=[
            ByPurposeItem(
                purpose=row[0] or "unknown",
                invocations=int(row[1]),
                total_tokens=int(row[2]),
                estimated_cost_usd=float(row[3]),
            )
            for row in purpose_rows
        ],
    )


@router.get("/timeseries", response_model=TimeseriesResponse)
def get_timeseries(
    db: DbSession,
    _actor: ViewActorCtx,
    window_days: int = Query(default=14, ge=1, le=180),
) -> TimeseriesResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # SQLite-friendly daily bucket: cast to date string.
    day_expr = func.strftime("%Y-%m-%d", LlmUsage.created_at)

    rows = (
        db.query(
            day_expr.label("day"),
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0.0),
        )
        .filter(LlmUsage.created_at >= cutoff)
        .group_by("day")
        .order_by("day")
        .all()
    )

    points = [
        TimeseriesPoint(
            date=str(row[0]),
            invocations=int(row[1]),
            total_tokens=int(row[2]),
            total_cost_usd=float(row[3]),
        )
        for row in rows
        if row[0] is not None
    ]

    return TimeseriesResponse(window_days=window_days, points=points)
