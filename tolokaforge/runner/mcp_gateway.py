"""Authenticated, per-trial streamable-HTTP MCP gateway for BYOH agents."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]


@dataclass
class MCPTrialRegistration:
    """Connection details returned only to the host-side trial runner."""

    namespace: str
    bearer_token: str
    url: str


@dataclass
class _Namespace:
    token: str
    manager: StreamableHTTPSessionManager
    ready: asyncio.Event | None = None
    stop: asyncio.Event | None = None
    task: asyncio.Task[None] | None = None


class RunnerMCPGateway:
    """Route one authenticated MCP namespace to each registered trial."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._namespaces: dict[str, _Namespace] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self) -> None:
        """Start the internal HTTP server in a daemon thread."""

        if self._thread and self._thread.is_alive():
            return
        config = uvicorn.Config(
            self,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="runner-mcp-gateway",
        )
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("Runner MCP gateway did not start within 10 seconds")

    def stop(self) -> None:
        """Stop every namespace and the HTTP server."""

        if self._loop and self._loop.is_running():
            for namespace in list(self._namespaces):
                self.unregister(namespace)
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        self._server = None

    def register(
        self,
        trial_id: str,
        tool_schemas: list[dict[str, Any]],
        execute: ToolExecutor,
    ) -> MCPTrialRegistration:
        """Create a token-isolated namespace for a registered Runner trial."""

        if self._loop is None:
            raise RuntimeError("Runner MCP gateway is not started")
        namespace = hashlib.sha256(trial_id.encode("utf-8")).hexdigest()[:32]
        token = secrets.token_urlsafe(32)
        server = Server(name=f"tolokaforge-{trial_id}", version="1.0.0")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=schema["name"],
                    description=schema.get("description"),
                    inputSchema=schema.get("parameters", {"type": "object", "properties": {}}),
                )
                for schema in tool_schemas
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            output, is_error = await execute(name, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=output)],
                isError=is_error,
            )

        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            stateless=True,
        )
        item = _Namespace(token=token, manager=manager)
        future = asyncio.run_coroutine_threadsafe(
            self._start_namespace(namespace, item), self._loop
        )
        future.result(timeout=10)
        return MCPTrialRegistration(
            namespace=namespace,
            bearer_token=token,
            url=f"http://runner:{self.port}/mcp/{namespace}",
        )

    def unregister(self, namespace_or_trial_id: str) -> None:
        """Remove a namespace; unknown values are an idempotent no-op."""

        if self._loop is None:
            return
        namespace = namespace_or_trial_id
        if namespace not in self._namespaces:
            namespace = hashlib.sha256(namespace_or_trial_id.encode("utf-8")).hexdigest()[:32]
        future = asyncio.run_coroutine_threadsafe(self._stop_namespace(namespace), self._loop)
        future.result(timeout=10)

    async def _start_namespace(self, namespace: str, item: _Namespace) -> None:
        if namespace in self._namespaces:
            raise ValueError(f"MCP namespace already registered: {namespace}")
        item.ready = asyncio.Event()
        item.stop = asyncio.Event()
        item.task = asyncio.create_task(self._run_namespace(item))
        self._namespaces[namespace] = item
        await asyncio.wait_for(item.ready.wait(), timeout=5)

    async def _run_namespace(self, item: _Namespace) -> None:
        assert item.ready is not None
        assert item.stop is not None
        async with item.manager.run():
            item.ready.set()
            await item.stop.wait()

    async def _stop_namespace(self, namespace: str) -> None:
        item = self._namespaces.pop(namespace, None)
        if item is None:
            return
        if item.stop is not None:
            item.stop.set()
        if item.task is not None:
            await item.task

    async def __call__(self, scope, receive, send) -> None:
        """ASGI entry point with bearer authentication before MCP dispatch."""

        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self._loop = asyncio.get_running_loop()
                    self._started.set()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    for namespace in list(self._namespaces):
                        await self._stop_namespace(namespace)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope["type"] != "http":
            await self._respond(send, 404, {"error": "not found"})
            return

        prefix = "/mcp/"
        path = scope.get("path", "")
        if not path.startswith(prefix):
            await self._respond(send, 404, {"error": "not found"})
            return
        namespace = path[len(prefix) :].strip("/")
        item = self._namespaces.get(namespace)
        if item is None:
            await self._respond(send, 404, {"error": "unknown trial namespace"})
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        expected = f"Bearer {item.token}"
        if not hmac.compare_digest(supplied, expected):
            await self._respond(
                send,
                401,
                {"error": "missing or invalid bearer token"},
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return

        forwarded_scope = dict(scope)
        forwarded_scope["path"] = "/mcp"
        forwarded_scope["raw_path"] = b"/mcp"
        await item.manager.handle_request(forwarded_scope, receive, send)

    @staticmethod
    async def _respond(
        send,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        headers.extend(extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
