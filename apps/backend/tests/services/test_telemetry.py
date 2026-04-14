from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.telemetry import (  # noqa: E402
    _NoOpTracer,
    configure_telemetry,
    get_current_trace_id,
    get_tracer,
    is_otel_available,
)


class TelemetryTests(unittest.TestCase):
    def test_noop_tracer_works(self) -> None:
        tracer = _NoOpTracer()

        with tracer.start_as_current_span("test.noop") as span:
            span.set_attribute("test.key", "value")

    def test_get_tracer_returns_object(self) -> None:
        tracer = get_tracer()

        self.assertTrue(hasattr(tracer, "start_as_current_span"))

    def test_get_current_trace_id_without_span(self) -> None:
        trace_id = get_current_trace_id()

        self.assertTrue(trace_id is None or re.fullmatch(r"[0-9a-f]{32}", trace_id))

    def test_configure_telemetry_no_crash(self) -> None:
        configure_telemetry()

    def test_is_otel_available(self) -> None:
        self.assertIsInstance(is_otel_available(), bool)


if __name__ == "__main__":
    unittest.main()
