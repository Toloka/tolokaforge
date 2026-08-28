"""Credential-shielding LLM gateway — reverse-proxy HTTP server.

Shields a real provider credential from the trial container. The CLI's
container receives a dummy token + a base URL pointing at the gateway
sidecar (compose service ``tolokaforge-llm-gateway``, port 8080). The
sidecar rewrites the inbound ``Authorization`` header to the real
credential and forwards only allow-listed request paths to the
upstream — the CLI never sees the real token.

:class:`_GatewayHTTPServer` is the whole runtime surface. The
sidecar's ``python -m`` entrypoint
(:mod:`tolokaforge.runner.llm_gateway_serve`) constructs it directly
from environment variables the driver bakes into the sidecar's compose
service. This module ships in the runner subset wheel so the
``tolokaforge-runner:local`` image the driver picks as the sidecar's
image has it importable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

GATEWAY_HOSTNAME = "tolokaforge-llm-gateway"
"""Compose service name the coding-harness driver adds the gateway
sidecar under. Trial containers reach the sidecar over docker's own
DNS at ``http://tolokaforge-llm-gateway:8080`` — same network, no
``extra_hosts`` mapping, no dependence on host-network topology."""

_REQUEST_TIMEOUT = httpx.Timeout(60.0)

# Headers that must not be relayed verbatim between the trial container and
# the upstream — connection-scoped (RFC 7230 §6.1) plus the two this proxy
# itself replaces (Host, Authorization).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "authorization",
    }
)


class CredentialGatewayConfig(Protocol):
    """Structural shape of ``HarnessSpec.credential_gateway`` this proxy reads.

    Both the shipped ``HarnessSpec.credential_gateway`` (Pydantic model) and
    the sidecar entrypoint's frozen-dataclass stub satisfy it — the proxy
    only ever reads these five fields, so it stays usable from either
    caller without importing the harness registry package."""

    upstream_url: str
    upstream_token_env_var: str
    upstream_auth_header: str
    upstream_auth_template: str
    path_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class GatewayHandle:
    """Where a running gateway can be reached, and by what container-facing name.

    The driver constructs one at ``attach()`` time — port + hostname the
    trial container will address the sidecar by — and consults it when
    rewriting compose (``depends_on``, ``ANTHROPIC_BASE_URL``, etc).
    """

    port: int
    hostname: str = GATEWAY_HOSTNAME


class _GatewayHTTPServer(ThreadingHTTPServer):
    """Reverse proxy: swaps the caller's ``Authorization`` header for the
    real upstream token, forwards only allow-listed paths, streams the
    upstream body back to the caller.

    ``_`` prefix marks this as a runtime surface — its sole caller is
    :mod:`tolokaforge.runner.llm_gateway_serve` (the sidecar entrypoint);
    unit tests exercise the same shape by instantiating it directly.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        gateway: CredentialGatewayConfig,
        upstream_token: str,
    ) -> None:
        self.gateway = gateway
        self.upstream_token = upstream_token
        self.upstream_client = httpx.Client(timeout=_REQUEST_TIMEOUT)
        super().__init__(server_address, _GatewayRequestHandler)

    def server_close(self) -> None:
        self.upstream_client.close()
        super().server_close()


class _GatewayRequestHandler(BaseHTTPRequestHandler):
    server: _GatewayHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("llm_gateway: " + format, *args)

    def _proxy(self) -> None:
        self.close_connection = True
        gateway = self.server.gateway
        path = urlparse(self.path).path
        if path not in gateway.path_allowlist:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.rfile.read(content_length) if content_length else b""
        outgoing_headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
        }
        outgoing_headers[gateway.upstream_auth_header] = gateway.upstream_auth_template.format(
            token=self.server.upstream_token
        )
        upstream_url = gateway.upstream_url.rstrip("/") + self.path

        request = self.server.upstream_client.build_request(
            self.command, upstream_url, content=body, headers=outgoing_headers
        )
        try:
            upstream_response = self.server.upstream_client.send(request, stream=True)
        except httpx.HTTPError:
            logger.exception("llm_gateway: upstream request failed for %s", self.path)
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            self.send_response(upstream_response.status_code)
            for name, value in upstream_response.headers.items():
                if name.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in upstream_response.iter_raw():
                self.wfile.write(chunk)
        finally:
            upstream_response.close()


__all__ = [
    "CredentialGatewayConfig",
    "GatewayHandle",
    "GATEWAY_HOSTNAME",
]
