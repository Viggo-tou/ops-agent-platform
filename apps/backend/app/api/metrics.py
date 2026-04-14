from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.cost_tracking import CostTracker
from app.services.metrics import get_metrics_output

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint."""
    metrics_output = get_metrics_output()
    if metrics_output is None:
        raise HTTPException(status_code=501, detail="Prometheus_client is not installed.")
    body, content_type = metrics_output
    return Response(content=body, media_type=content_type)


@router.get("/api/admin/costs")
def get_costs(group_by: str = "task", db: Session = Depends(get_db)) -> list[dict]:
    """LLM cost aggregation."""
    return CostTracker(db).get_costs(group_by=group_by)
