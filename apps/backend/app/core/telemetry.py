from __future__ import annotations

import os
from types import TracebackType
from typing import Any

_tracer = None
_otel_available = False
_telemetry_configured = False


def configure_telemetry() -> None:
    """Set up OpenTelemetry tracing. No-op if packages are missing."""
    global _tracer, _otel_available, _telemetry_configured

    if _telemetry_configured:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        _otel_available = False
        return

    resource = Resource.create({"service.name": "ops-agent-platform"})
    provider = TracerProvider(resource=resource)

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        else:
            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("ops-agent-platform")
    _otel_available = True
    _telemetry_configured = True


def get_tracer() -> Any:
    """Return the configured tracer, or a no-op proxy."""
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace
    except ImportError:
        return _NoOpTracer()
    return trace.get_tracer("ops-agent-platform")


def is_otel_available() -> bool:
    return _otel_available


def get_current_trace_id() -> str | None:
    """Return the current span's trace ID as a hex string, or None."""
    if not _otel_available:
        return None
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:
        return None
    return None


class _NoOpTracer:
    """Fallback when OpenTelemetry is not installed."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> "_NoOpContextManager":
        return _NoOpContextManager()


class _NoOpContextManager:
    def __enter__(self) -> "_NoOpSpan":
        return _NoOpSpan()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, status: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None
