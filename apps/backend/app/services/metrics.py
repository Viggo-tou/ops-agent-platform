from __future__ import annotations

_prometheus_available = False

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _prometheus_available = True
except ImportError:
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    Counter = None  # type: ignore[assignment]
    Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]


if _prometheus_available:
    _task_total = Counter(
        "ops_task_total",
        "Total tasks completed by scenario and status.",
        ["scenario", "status"],
    )
    _task_duration_seconds = Histogram(
        "ops_task_duration_seconds",
        "Task duration in seconds by scenario.",
        ["scenario"],
    )
    _tool_execution_total = Counter(
        "ops_tool_execution_total",
        "Total tool executions by tool name and status.",
        ["tool_name", "status"],
    )
    _tool_duration_seconds = Histogram(
        "ops_tool_duration_seconds",
        "Tool execution duration in seconds by tool name.",
        ["tool_name"],
    )
    _approval_wait_seconds = Histogram(
        "ops_approval_wait_seconds",
        "Approval wait time in seconds by approver role.",
        ["approver_role"],
    )
    _reviewer_verdict_total = Counter(
        "ops_reviewer_verdict_total",
        "Total reviewer verdicts.",
        ["verdict"],
    )
else:
    _task_total = None
    _task_duration_seconds = None
    _tool_execution_total = None
    _tool_duration_seconds = None
    _approval_wait_seconds = None
    _reviewer_verdict_total = None


def record_task_completed(scenario: str, status: str, duration_seconds: float) -> None:
    if not _prometheus_available:
        return
    _task_total.labels(scenario=scenario, status=status).inc()
    _task_duration_seconds.labels(scenario=scenario).observe(duration_seconds)


def record_tool_execution(tool_name: str, status: str, duration_seconds: float) -> None:
    if not _prometheus_available:
        return
    _tool_execution_total.labels(tool_name=tool_name, status=status).inc()
    _tool_duration_seconds.labels(tool_name=tool_name).observe(duration_seconds)


def record_approval_wait(approver_role: str, wait_seconds: float) -> None:
    if not _prometheus_available:
        return
    _approval_wait_seconds.labels(approver_role=approver_role).observe(wait_seconds)


def record_reviewer_verdict(verdict: str) -> None:
    if not _prometheus_available:
        return
    _reviewer_verdict_total.labels(verdict=verdict).inc()


def get_metrics_output() -> tuple[bytes, str] | None:
    """Return Prometheus scrape output, or None when prometheus_client is unavailable."""
    if not _prometheus_available or generate_latest is None:
        return None
    return generate_latest(), CONTENT_TYPE_LATEST
