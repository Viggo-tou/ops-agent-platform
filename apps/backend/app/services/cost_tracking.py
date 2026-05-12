from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.llm_usage import LlmUsage


class CostTracker:
    def __init__(self, db: Session):
        self.db = db

    def record_usage(
        self,
        *,
        task_id: str | None,
        actor_name: str | None,
        provider_name: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        purpose: str = "unknown",
    ) -> LlmUsage:
        total = input_tokens + output_tokens
        cost = self._estimate_cost(provider_name, model_name, input_tokens, output_tokens)
        usage = LlmUsage(
            task_id=task_id,
            actor_name=actor_name,
            provider_name=provider_name,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            estimated_cost_usd=cost,
            purpose=purpose,
        )
        self.db.add(usage)
        self.db.flush()
        return usage

    def get_costs(self, *, group_by: str = "task") -> list[dict]:
        """Aggregate costs by task, actor_name, or day."""
        group_columns = {
            "task": LlmUsage.task_id,
            "actor_name": LlmUsage.actor_name,
            "actor": LlmUsage.actor_name,
            "day": func.date(LlmUsage.created_at),
        }
        group_column = group_columns.get(group_by)
        if group_column is None:
            group_column = LlmUsage.task_id
            group_by = "task"

        rows = (
            self.db.query(
                group_column.label("group_key"),
                func.count(LlmUsage.id).label("usage_count"),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0.0).label("estimated_cost_usd"),
            )
            .group_by(group_column)
            .order_by(group_column)
            .all()
        )

        return [
            {
                "group_by": group_by,
                "key": row.group_key,
                "usage_count": int(row.usage_count or 0),
                "input_tokens": int(row.input_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost_usd": float(row.estimated_cost_usd or 0.0),
            }
            for row in rows
        ]

    @staticmethod
    def _estimate_cost(provider: str, model: str, inp: int, out: int) -> float:
        """Simple provider-level cost estimate based on rough per-1K-token rates."""
        rates = {
            "minimax": (0.001, 0.002),
            "openai": (0.01, 0.03),
        }
        inp_rate, out_rate = rates.get(provider.lower(), (0.0, 0.0))
        return (inp * inp_rate + out * out_rate) / 1000.0
