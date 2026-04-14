from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.enums import EventSource, EventType, WorkflowStage  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.core.middleware import RequestLoggingMiddleware  # noqa: E402
from app.services.events import record_event  # noqa: E402


class StructlogTests(unittest.TestCase):
    def test_configure_logging_json(self) -> None:
        configure_logging(json_output=True)
        get_logger(component="test").info("json_log_test", value=1)

    def test_configure_logging_console(self) -> None:
        configure_logging(json_output=False)
        get_logger(component="test").info("console_log_test", value=1)

    def test_get_logger_returns_bound_logger(self) -> None:
        logger = get_logger(component="test")

        self.assertTrue(hasattr(logger, "info"))
        self.assertTrue(hasattr(logger, "warning"))
        self.assertTrue(hasattr(logger, "error"))

    def test_event_bridge_emits_log(self) -> None:
        db = Mock()
        logger = Mock()

        with patch("app.services.events._event_logger", logger):
            record_event(
                db,
                task_id="task-1",
                session_id="session-1",
                event_type=EventType.TASK_CREATED,
                source=EventSource.API,
                stage=WorkflowStage.INTAKE,
                message="created task",
            )

        logger.info.assert_called_once()
        args, kwargs = logger.info.call_args
        self.assertEqual(args, ("lifecycle_event",))
        self.assertEqual(kwargs["task_id"], "task-1")
        self.assertEqual(kwargs["event_type"], "task_created")
        self.assertEqual(kwargs["message"], "created task")

    def test_request_middleware_logs(self) -> None:
        logger = Mock()
        app = FastAPI()

        @app.get("/ping")
        def ping() -> dict[str, str]:
            return {"status": "ok"}

        with patch("app.core.middleware._request_logger", logger):
            app.add_middleware(RequestLoggingMiddleware)
            response = TestClient(app).get("/ping", headers={"X-Actor-Role": "admin"})

        self.assertEqual(response.status_code, 200)
        logger.info.assert_called_once()
        args, kwargs = logger.info.call_args
        self.assertEqual(args, ("http_request",))
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["path"], "/ping")
        self.assertEqual(kwargs["status_code"], 200)
        self.assertIsInstance(kwargs["duration_ms"], int)
        self.assertEqual(kwargs["actor_role"], "admin")


if __name__ == "__main__":
    unittest.main()
