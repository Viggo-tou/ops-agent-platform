from __future__ import annotations

import httpx


def external_http_timeout(read_seconds: float | None = None) -> httpx.Timeout:
    """Return explicit per-phase timeouts for outbound provider calls."""
    read = float(read_seconds if read_seconds is not None else 120.0)
    return httpx.Timeout(
        connect=10.0,
        read=read,
        write=30.0,
        pool=30.0,
    )
