"""Built-in readiness probes against real in-process localhost listeners.

Each concrete probe is exercised against a real listener on an ephemeral port
(a started gRPC server, an ``http.server`` serving ``/health``, a raw socket) —
no mocks — and then against a free-but-unbound port to lock the failure branch:
``ok=False`` with a populated ``detail`` inside the timeout budget.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer

import grpc
import pytest

from tolokaforge.core.service_readiness import (
    GrpcReadinessProbe,
    HttpReadinessProbe,
    ResolvedEndpoint,
    TcpReadinessProbe,
)

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def grpc_endpoint() -> Iterator[ResolvedEndpoint]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield ResolvedEndpoint(host="127.0.0.1", port=port)
    finally:
        server.stop(grace=None)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        status = 200 if self.path == "/health" else 404
        self.send_response(status)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def http_endpoint() -> Iterator[ResolvedEndpoint]:
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ResolvedEndpoint(host="127.0.0.1", port=server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def tcp_endpoint() -> Iterator[ResolvedEndpoint]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield ResolvedEndpoint(host="127.0.0.1", port=listener.getsockname()[1])
    finally:
        listener.close()


def test_grpc_probe_ready_against_live_server(grpc_endpoint: ResolvedEndpoint) -> None:
    result = GrpcReadinessProbe().probe(grpc_endpoint, timeout=5.0)
    assert result.ok is True
    assert result.latency_s >= 0.0
    assert result.detail is None


def test_grpc_probe_fails_against_closed_port() -> None:
    endpoint = ResolvedEndpoint(host="127.0.0.1", port=_free_port())
    result = GrpcReadinessProbe().probe(endpoint, timeout=1.0)
    assert result.ok is False
    assert result.detail is not None
    assert result.latency_s < 5.0


def test_http_probe_ready_against_live_server(http_endpoint: ResolvedEndpoint) -> None:
    result = HttpReadinessProbe().probe(http_endpoint, timeout=5.0)
    assert result.ok is True
    assert result.latency_s >= 0.0
    assert result.detail is None


def test_http_probe_fails_against_closed_port() -> None:
    endpoint = ResolvedEndpoint(host="127.0.0.1", port=_free_port())
    result = HttpReadinessProbe().probe(endpoint, timeout=1.0)
    assert result.ok is False
    assert result.detail is not None
    assert result.latency_s < 5.0


def test_tcp_probe_ready_against_live_listener(tcp_endpoint: ResolvedEndpoint) -> None:
    result = TcpReadinessProbe().probe(tcp_endpoint, timeout=5.0)
    assert result.ok is True
    assert result.latency_s >= 0.0
    assert result.detail is None


def test_tcp_probe_fails_against_closed_port() -> None:
    endpoint = ResolvedEndpoint(host="127.0.0.1", port=_free_port())
    result = TcpReadinessProbe().probe(endpoint, timeout=1.0)
    assert result.ok is False
    assert result.detail is not None
    assert result.latency_s < 5.0
