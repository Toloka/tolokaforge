"""Behaviour-locking tests for :mod:`tolokaforge.runner.llm_gateway`.

Exercises the endpoint against a real loopback upstream so path-allowlist
enforcement, header rewriting, and streaming pass-through are proven over
an actual socket rather than mocked internals.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tolokaforge.runner.llm_gateway import GatewayHandle, LLMGatewayEndpoint
from tolokaforge.secrets import DictProvider, SecretManager

pytestmark = pytest.mark.unit

REAL_TOKEN = "sk-real-upstream-token-0123456789"
DUMMY_TOKEN = "sk-tolokaforge-shielded-dummy"


@dataclass(frozen=True)
class FakeGatewayConfig:
    """Structural double for the not-yet-shipped ``CredentialGateway`` model."""

    upstream_url: str
    upstream_token_env_var: str = "OPENROUTER_API_KEY"
    upstream_auth_header: str = "Authorization"
    upstream_auth_template: str = "Bearer {token}"
    path_allowlist: tuple[str, ...] = ("/v1/models",)


@dataclass(frozen=True)
class FakeSpec:
    """Structural double for ``HarnessSpec`` — only ``credential_gateway`` matters."""

    credential_gateway: FakeGatewayConfig | None


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


def _secret_manager(token: str = REAL_TOKEN) -> SecretManager:
    return SecretManager([DictProvider({"OPENROUTER_API_KEY": token})])


@pytest.fixture
def running_gateway(
    upstream: tuple[str, type[_RecordingUpstreamHandler]], isolated_secret_manager: None
) -> Iterator[tuple[GatewayHandle, type[_RecordingUpstreamHandler]]]:
    upstream_url, handler_cls = upstream
    spec = FakeSpec(credential_gateway=FakeGatewayConfig(upstream_url=upstream_url))
    endpoint = LLMGatewayEndpoint(spec, _secret_manager())
    handle = endpoint.start()
    try:
        yield handle, handler_cls
    finally:
        endpoint.stop()


class TestPathAllowlist:
    def test_allowed_path_returns_200(
        self, running_gateway: tuple[GatewayHandle, type[_RecordingUpstreamHandler]]
    ) -> None:
        handle, _ = running_gateway
        response = httpx.get(f"http://127.0.0.1:{handle.port}/v1/models")
        assert response.status_code == HTTPStatus.OK
        assert response.content == b'{"data": "ok"}'

    def test_denied_path_returns_405(
        self, running_gateway: tuple[GatewayHandle, type[_RecordingUpstreamHandler]]
    ) -> None:
        handle, handler_cls = running_gateway
        response = httpx.get(f"http://127.0.0.1:{handle.port}/v1/not-allowlisted")
        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
        assert handler_cls.received == []


class TestHeaderRewriting:
    def test_dummy_incoming_authorization_is_replaced_with_real_upstream_token(
        self, running_gateway: tuple[GatewayHandle, type[_RecordingUpstreamHandler]]
    ) -> None:
        handle, handler_cls = running_gateway
        httpx.get(
            f"http://127.0.0.1:{handle.port}/v1/models",
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
        isolated_secret_manager: None,
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

        spec = FakeSpec(
            credential_gateway=FakeGatewayConfig(
                upstream_url=upstream_url, path_allowlist=("/v1/messages",)
            )
        )
        endpoint = LLMGatewayEndpoint(spec, _secret_manager())
        handle = endpoint.start()
        try:
            response = httpx.get(f"http://127.0.0.1:{handle.port}/v1/messages")
        finally:
            endpoint.stop()

        assert response.status_code == HTTPStatus.OK
        assert response.content == b"".join(sse_chunks)


class TestNoDirectEnvironAccess:
    def test_constructor_never_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("LLMGatewayEndpoint touched os.environ directly")

        monkeypatch.setattr(os.environ, "get", _forbidden)
        monkeypatch.setattr(os, "getenv", _forbidden)

        spec = FakeSpec(
            credential_gateway=FakeGatewayConfig(upstream_url="http://upstream.invalid")
        )
        endpoint = LLMGatewayEndpoint(spec, _secret_manager())
        assert endpoint is not None
