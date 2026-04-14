from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import get_logger

_request_logger = get_logger(component="http")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)
        _request_logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            actor_role=request.headers.get("X-Actor-Role"),
        )
        return response
