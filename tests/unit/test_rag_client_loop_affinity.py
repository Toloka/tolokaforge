"""``RAGServiceClient`` survives being driven from more than one event loop.

The runner's shared ``RAGServiceClient`` binds its pooled httpx client to the
first loop it issues a request on. Reusing that client for a request on
``RunnerServiceImpl``'s dedicated loop then raised
``<asyncio.locks.Event ...> is bound to a different event loop`` and aborted
every rag trial at ``register_trial``. ``_get_client`` rebuilds the client when
the running loop changes, so a request on a second loop succeeds.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tolokaforge.runner.rag_client import RAGServiceClient

pytestmark = pytest.mark.unit


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API name
        if self.path == "/health":
            body = json.dumps(
                {"status": "healthy", "version": "test", "active_indices": 0}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: Any) -> None:  # silence the default access log
        pass


@pytest.fixture
def rag_health_url() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_requests_succeed_across_two_loops(rag_health_url: str) -> None:
    """A real GET on one loop then another is the exact ``register_trial``
    regression: the unfixed client raises 'bound to a different event loop' on
    the second-loop request. Rebuilding on loop change lets it succeed."""
    client = RAGServiceClient(rag_health_url)
    probe_loop = asyncio.new_event_loop()
    servicer_loop = asyncio.new_event_loop()
    try:
        first = probe_loop.run_until_complete(client.health_check())
        probe_client = client._bound[0]  # the client bound to probe_loop
        # Reuse the SAME RAGServiceClient for a real request on a second loop.
        second = servicer_loop.run_until_complete(client.health_check())

        assert first.status == "healthy"
        assert second.status == "healthy"
        assert client._bound[1] is servicer_loop, "the request rebuilt onto the new loop"
    finally:
        # Close each underlying httpx client on the loop it is bound to, so the
        # forced rebuild leaves nothing unclosed.
        servicer_loop.run_until_complete(client.close())
        if not probe_client.is_closed:
            probe_loop.run_until_complete(probe_client.aclose())
        probe_loop.close()
        servicer_loop.close()


async def test_same_loop_reuses_the_cached_client() -> None:
    """On one loop the client is built once and reused — the common runner path
    (every trial drives it from the one servicer loop)."""
    client = RAGServiceClient("http://rag-service:8001")
    try:
        first = await client._get_client()
        assert await client._get_client() is first
    finally:
        await client.close()
