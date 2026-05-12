"""MCP (Model Context Protocol) client.

Why this exists
---------------
MCP servers expose typed tools (filesystem read, git operations, custom
internal APIs, etc.) over JSON-RPC 2.0 via stdio. This module spawns the
configured MCP servers as subprocesses, holds their sessions open, and
exposes a sync ``call_tool()`` API so the rest of the backend (which is
mostly sync — orchestrator, codegen, gateway) can invoke them without
having to deal with the MCP SDK's async-only surface.

Architecture
------------
Sync callers ────► call_tool(server, name, args, timeout)
                          │
                          │  asyncio.run_coroutine_threadsafe
                          ▼
                  ┌──────────────────┐
                  │  background      │
                  │  asyncio loop    │   ← runs in a daemon thread
                  │  (one per app)   │
                  └────────┬─────────┘
                           │
              one task per server, each holding:
                  ┌──────────────────┐
                  │ stdio_client     │   ← subprocess + JSON-RPC
                  │ ClientSession    │
                  │ asyncio.Queue    │   ← inbound call requests
                  └──────────────────┘

Sessions stay open for the lifetime of the backend. If a server's
subprocess dies, that server is marked unavailable and its task exits;
other servers keep running. We don't auto-restart in this iteration —
that's a follow-up.

Empty config or import-time failures are non-fatal: the rest of the
backend boots normally and MCP simply has no tools to offer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mcp")


@dataclass
class _ServerState:
    """Per-server runtime state held by _MCPClient."""

    name: str
    config: dict[str, Any]
    request_queue: asyncio.Queue | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    connected: bool = False
    error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    task: asyncio.Task | None = None


class _ShutdownSentinel:
    """Marker placed on the request queue to tell a server task to exit."""


_SHUTDOWN = _ShutdownSentinel()


class MCPNotRunningError(RuntimeError):
    pass


class MCPServerError(RuntimeError):
    pass


class _MCPClient:
    """Process-wide singleton that owns the MCP event loop + server tasks.

    Use :func:`get_mcp_client` rather than instantiating directly.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, _ServerState] = {}
        self._lock = threading.Lock()
        self._init_timeout = 30.0
        self._call_timeout = 60.0
        self._started = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(
        self,
        servers_config_json: str,
        *,
        init_timeout: float = 30.0,
        call_timeout: float = 60.0,
    ) -> None:
        """Parse config and spawn one stdio session per server.

        No-op if already started or if the config is empty / malformed.
        Failures parsing individual server entries are logged and skipped.
        """
        with self._lock:
            if self._started:
                logger.debug("mcp.start called twice; ignoring")
                return
            self._init_timeout = init_timeout
            self._call_timeout = call_timeout

            servers = self._parse_config(servers_config_json)
            if not servers:
                logger.info("mcp_disabled_no_servers")
                self._started = True
                return

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="mcp-event-loop",
                daemon=True,
            )
            self._thread.start()

            for name, cfg in servers.items():
                state = _ServerState(name=name, config=cfg)
                self._servers[name] = state
                # Schedule task creation on the loop so the queue lives
                # on the right thread.
                future = asyncio.run_coroutine_threadsafe(
                    self._spawn_server(state), self._loop
                )
                # Don't block on init here — wait_ready handles that
                # asynchronously so a single slow server can't stall
                # backend boot.
                future.result(timeout=5)

            self._started = True
            logger.info("mcp_started", extra={"server_count": len(self._servers)})

    def wait_ready(self, timeout: float | None = None) -> None:
        """Block until every server has either connected or failed.

        Useful for tests and for the registry build step that wants to
        know which tools are actually available before serving a request.
        Per-server timeout is bounded by mcp_init_timeout_seconds.
        """
        deadline = None
        if timeout is not None:
            import time

            deadline = time.monotonic() + timeout
        for state in self._servers.values():
            remaining = None
            if deadline is not None:
                import time

                remaining = max(0.0, deadline - time.monotonic())
            state.ready.wait(timeout=remaining if remaining is not None else self._init_timeout)

    def stop(self) -> None:
        """Send shutdown to every server task and tear down the loop."""
        with self._lock:
            if not self._started or self._loop is None:
                return
            for state in self._servers.values():
                if state.request_queue is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            state.request_queue.put(_SHUTDOWN), self._loop
                        ).result(timeout=2)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                pass
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop = None
            self._thread = None
            self._servers.clear()
            self._started = False
            logger.info("mcp_stopped")

    # ------------------------------------------------------------------
    # public read API
    # ------------------------------------------------------------------
    def list_servers(self) -> dict[str, dict[str, Any]]:
        """Snapshot of every configured server's connection state."""
        return {
            name: {
                "connected": state.connected,
                "error": state.error,
                "tool_count": len(state.tools),
            }
            for name, state in self._servers.items()
        }

    def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        """All tools, grouped by server name. Each tool is {name, description, input_schema}."""
        return {
            name: list(state.tools)
            for name, state in self._servers.items()
            if state.connected
        }

    # ------------------------------------------------------------------
    # public call API
    # ------------------------------------------------------------------
    def call_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Synchronously invoke a tool. Blocks the calling thread.

        Returns a JSON-serializable dict matching the MCP CallToolResult
        shape: ``{"is_error": bool, "content": [{"type":"text","text":"..."}]}``.
        Raises MCPNotRunningError / MCPServerError / TimeoutError.
        """
        if not self._started or self._loop is None:
            raise MCPNotRunningError("MCP client is not running")
        state = self._servers.get(server)
        if state is None:
            raise MCPServerError(f"Unknown MCP server: {server}")
        if not state.connected:
            raise MCPServerError(
                f"MCP server '{server}' is not connected: {state.error or 'unknown error'}"
            )

        effective_timeout = timeout if timeout is not None else self._call_timeout
        coro = self._call_async(state, tool_name, arguments or {})
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=effective_timeout)

    # ------------------------------------------------------------------
    # internals — runs on the event loop thread
    # ------------------------------------------------------------------
    async def _spawn_server(self, state: _ServerState) -> None:
        state.request_queue = asyncio.Queue()
        # Capture the running loop so the actual server task lives on it.
        state.task = asyncio.create_task(
            self._run_server(state), name=f"mcp-{state.name}"
        )

    async def _run_server(self, state: _ServerState) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:  # noqa: BLE001
            state.error = f"mcp SDK import failed: {exc}"
            logger.warning("mcp_sdk_missing", extra={"error": state.error})
            state.ready.set()
            return

        try:
            params = StdioServerParameters(
                command=state.config["command"],
                args=list(state.config.get("args", [])),
                env=state.config.get("env"),
                cwd=state.config.get("cwd"),
            )
        except KeyError as exc:
            state.error = f"missing required config field: {exc}"
            logger.warning(
                "mcp_server_config_invalid",
                extra={"server": state.name, "error": state.error},
            )
            state.ready.set()
            return

        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    try:
                        await asyncio.wait_for(
                            session.initialize(), timeout=self._init_timeout
                        )
                    except asyncio.TimeoutError:
                        state.error = f"initialize timeout (>{self._init_timeout}s)"
                        logger.warning(
                            "mcp_init_timeout",
                            extra={"server": state.name, "timeout": self._init_timeout},
                        )
                        state.ready.set()
                        return

                    try:
                        tools_result = await asyncio.wait_for(
                            session.list_tools(), timeout=self._init_timeout
                        )
                        state.tools = [
                            {
                                "name": t.name,
                                "description": t.description or "",
                                "input_schema": getattr(t, "inputSchema", {}) or {},
                            }
                            for t in tools_result.tools
                        ]
                    except Exception as exc:  # noqa: BLE001
                        state.error = f"list_tools failed: {exc}"
                        logger.warning(
                            "mcp_list_tools_failed",
                            extra={"server": state.name, "error": state.error},
                        )
                        state.ready.set()
                        return

                    state.connected = True
                    state.ready.set()
                    logger.info(
                        "mcp_server_ready",
                        extra={"server": state.name, "tool_count": len(state.tools)},
                    )

                    # Service incoming call requests until shutdown.
                    while True:
                        req = await state.request_queue.get()
                        if isinstance(req, _ShutdownSentinel):
                            logger.info(
                                "mcp_server_shutdown_requested",
                                extra={"server": state.name},
                            )
                            return
                        tool_name, arguments, fut = req
                        try:
                            result = await session.call_tool(tool_name, arguments)
                            fut.set_result(_serialize_call_result(result))
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001
            state.connected = False
            state.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "mcp_server_crashed",
                extra={"server": state.name, "error": state.error},
            )
            state.ready.set()

    async def _call_async(
        self, state: _ServerState, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if state.request_queue is None:
            raise MCPServerError(f"MCP server '{state.name}' has no request queue")
        fut: asyncio.Future = asyncio.Future()
        await state.request_queue.put((tool_name, arguments, fut))
        return await fut

    @staticmethod
    def _parse_config(raw_json: str) -> dict[str, dict[str, Any]]:
        if not raw_json or not raw_json.strip():
            return {}
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("mcp_config_parse_failed", extra={"error": str(exc)})
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "mcp_config_unexpected_shape",
                extra={"got": type(data).__name__},
            )
            return {}
        # Normalize: drop entries missing 'command'.
        cleaned: dict[str, dict[str, Any]] = {}
        for name, cfg in data.items():
            if not isinstance(cfg, dict) or "command" not in cfg:
                logger.warning(
                    "mcp_server_entry_invalid",
                    extra={"server": name, "reason": "missing 'command' field"},
                )
                continue
            cleaned[str(name)] = cfg
        return cleaned


def _serialize_call_result(result: Any) -> dict[str, Any]:
    """Convert MCP CallToolResult to a JSON-serializable dict.

    Different SDK versions expose slightly different shapes (ListContent
    vs raw dicts). We normalize to ``{"is_error": bool, "content": [...]}``
    where each content item is ``{"type": ..., "text"|"data"|...: ...}``.
    """
    is_error = bool(getattr(result, "isError", False))
    raw_content = getattr(result, "content", None) or []
    content: list[dict[str, Any]] = []
    for item in raw_content:
        if hasattr(item, "model_dump"):
            content.append(item.model_dump())
            continue
        if isinstance(item, dict):
            content.append(item)
            continue
        # Fallback: stringify
        content.append({"type": "text", "text": str(item)})
    return {"is_error": is_error, "content": content}


# Singleton accessor ----------------------------------------------------
_singleton: _MCPClient | None = None
_singleton_lock = threading.Lock()


def get_mcp_client() -> _MCPClient:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = _MCPClient()
        return _singleton
