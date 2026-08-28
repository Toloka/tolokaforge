"""Behaviour-locking tests for :mod:`tolokaforge.runner.llm_gateway`.

Exercises the reverse-proxy HTTP server against a real loopback upstream
so path-allowlist enforcement, header rewriting, and streaming
pass-through are proven over an actual socket rather than mocked
internals. Matches the shape the sidecar entrypoint constructs.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tolokaforge.runner.llm_gateway import _GatewayHTTPServer

pytestmark = pytest.mark.unit

REAL_TOKEN = "sk-real-upstream-token-0123456789"
DUMMY_TOKEN = "sk-tolokaforge-shielded-dummy"


@dataclass(frozen=True)
class FakeGatewayConfig:
    """Structural double for ``HarnessSpec.credential_gateway`` — matches
    the :class:`CredentialGatewayConfig` Protocol the server reads."""

    upstream_url: str
    upstream_token_env_var: str = "OPENROUTER_API_KEY"
    upstream_auth_header: str = "Authorization"
    upstream_auth_template: str = "Bearer {token}"
    path_allowlist: tuple[str, ...] = ("/v1/models",)


class _RecordingUpstreamHandler(BaseHTTPRequestHandler):
    """Loopback upstream: records the request it received, replays a canned response."""

    protocol_version = "HTTP/1.1"
    received: list[dict[str, object]] = []
    response_chunks: list[bytes] = [b'{"data": "ok"}']
    response_status: int = 200
    chunked: bool = False

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append(
            {"path": self.path, "headers": dict(self.headers.items()), "body": body}
        )
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        if type(self).chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in type(self).response_chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            total = sum(len(chunk) for chunk in type(self).response_chunks)
            self.send_header("Content-Length", str(total))
            self.end_headers()
            for chunk in type(self).response_chunks:
                self.wfile.write(chunk)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def upstream() -> Iterator[tuple[str, type[_RecordingUpstreamHandler]]]:
    handler_cls = type(
        "_TestUpstreamHandler",
        (_RecordingUpstreamHandler,),
        {"received": [], "response_chunks": [b'{"data": "ok"}'], "response_status": 200},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler_cls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _serve(
    gateway_config: FakeGatewayConfig, upstream_token: str = REAL_TOKEN
) -> tuple[_GatewayHTTPServer, threading.Thread, int]:
    server = _GatewayHTTPServer(("127.0.0.1", 0), gateway_config, upstream_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


@pytest.fixture
def running_gateway(
    upstream: tuple[str, type[_RecordingUpstreamHandler]],
) -> Iterator[tuple[int, type[_RecordingUpstreamHandler]]]:
    upstream_url, handler_cls = upstream
    config = FakeGatewayConfig(upstream_url=upstream_url)
    server, thread, port = _serve(config)
    try:
        yield port, handler_cls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestPathAllowlist:
    def test_allowed_path_returns_200(
        self, running_gateway: tuple[int, type[_RecordingUpstreamHandler]]
    ) -> None:
        port, _ = running_gateway
        response = httpx.get(f"http://127.0.0.1:{port}/v1/models")
        assert response.status_code == HTTPStatus.OK
        assert response.content == b'{"data": "ok"}'

    def test_denied_path_returns_405(
        self, running_gateway: tuple[int, type[_RecordingUpstreamHandler]]
    ) -> None:
        port, handler_cls = running_gateway
        response = httpx.get(f"http://127.0.0.1:{port}/v1/not-allowlisted")
        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
        assert handler_cls.received == []


class TestHeaderRewriting:
    def test_dummy_incoming_authorization_is_replaced_with_real_upstream_token(
        self, running_gateway: tuple[int, type[_RecordingUpstreamHandler]]
    ) -> None:
        port, handler_cls = running_gateway
        httpx.get(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": f"Bearer {DUMMY_TOKEN}"},
        )
        assert len(handler_cls.received) == 1
        received_auth = handler_cls.received[0]["headers"]["Authorization"]
        assert received_auth == f"Bearer {REAL_TOKEN}"
        assert DUMMY_TOKEN not in received_auth


class TestStreamingPassthrough:
    def test_chunked_upstream_body_passes_through_byte_identical(
        self,
        upstream: tuple[str, type[_RecordingUpstreamHandler]],
    ) -> None:
        upstream_url, handler_cls = upstream
        sse_chunks = [
            b"event: message_start\ndata: {}\n\n",
            b'event: content_block_delta\ndata: {"text": "hel"}\n\n',
            b'event: content_block_delta\ndata: {"text": "lo"}\n\n',
            b"event: message_stop\ndata: {}\n\n",
        ]
        handler_cls.response_chunks = sse_chunks
        handler_cls.chunked = True

        config = FakeGatewayConfig(upstream_url=upstream_url, path_allowlist=("/v1/messages",))
        server, thread, port = _serve(config)
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/v1/messages")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert response.status_code == HTTPStatus.OK
        assert response.content == b"".join(sse_chunks)
