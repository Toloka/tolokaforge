"""Credential-shielding LLM gateway — Layer 2 of the three-layer split.

Layer 1 (Launcher, varies by deployment) starts and stops this endpoint;
Layer 2 (this module) is a portable reverse proxy that resolves the real
provider credential via :class:`~tolokaforge.secrets.manager.SecretManager`
and forwards only allow-listed paths to the upstream, rewriting the
inbound (dummy) auth header to the real one. Layer 3 (the vendor CLI in
the trial container) never sees a real credential.

:class:`LLMGatewayEndpoint` never imports the harness registry package —
it depends on :class:`HarnessSpecLike`, the structural subset of
``tolokaforge_coding_harnesses.HarnessSpec`` it actually needs, so this
module stays usable from a future cluster-mode sidecar entrypoint that has
no reason to import the local-mode launcher or the harness registry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import urlparse

import httpx

from tolokaforge.secrets import install_global_redactor, register_runtime_secret
from tolokaforge.secrets.manager import SecretManager

logger = logging.getLogger(__name__)

GATEWAY_HOSTNAME = "tolokaforge-llm-gateway"
"""Docker ``extra_hosts`` alias a local-mode launcher binds the gateway
under. Trial containers resolve it via a ``host-gateway`` entry pointing
back at the orchestrator host."""

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
    """Structural shape of ``HarnessSpec.credential_gateway`` this endpoint reads."""

    upstream_url: str
    upstream_token_env_var: str
    upstream_auth_header: str
    upstream_auth_template: str
    path_allowlist: tuple[str, ...]


class HarnessSpecLike(Protocol):
    """Structural subset of ``HarnessSpec`` the gateway needs.

    Any object exposing a ``credential_gateway`` attribute of this shape
    satisfies this protocol — the real ``HarnessSpec`` does so once its
    ``credential_gateway`` field lands.
    """

    credential_gateway: CredentialGatewayConfig | None


@dataclass(frozen=True)
class GatewayHandle:
    """Where a running gateway can be reached, and by what container-facing name."""

    port: int
    hostname: str = GATEWAY_HOSTNAME


class LLMGatewayEndpoint:
    """Reverse proxy that shields a real provider credential from a trial container.

    Resolves the upstream token via ``secret_manager`` at construction time
    — never via ``os.environ`` — and, once started, forwards only
    :attr:`CredentialGatewayConfig.path_allowlist` paths to
    :attr:`CredentialGatewayConfig.upstream_url`, replacing whatever
    ``Authorization`` header the caller sent with the real one.
    """

    def __init__(self, spec: HarnessSpecLike, secret_manager: SecretManager) -> None:
        gateway = spec.credential_gateway
        if gateway is None:
            raise ValueError(f"{spec!r} has no credential_gateway configured")
        self._gateway = gateway
        self._upstream_token = secret_manager.get_secret_or_raise(gateway.upstream_token_env_var)
        self._server: _GatewayHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> GatewayHandle:
        """Bind an ephemeral port and start serving on a background thread."""
        if self._server is not None:
            raise RuntimeError("LLMGatewayEndpoint.start() called while already running")
        register_runtime_secret(self._gateway.upstream_token_env_var, self._upstream_token)
        install_global_redactor()
        self._server = _GatewayHTTPServer(("0.0.0.0", 0), self._gateway, self._upstream_token)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return GatewayHandle(port=self._server.server_address[1])

    def stop(self) -> None:
        """Stop serving and release the port. Safe to call without a prior ``start()``."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


class HostGatewayLauncher(Protocol):
    """Layer 1 contract: starts and stops an :class:`LLMGatewayEndpoint`.

    :class:`LocalHostGatewayLauncher` is the local-mode implementation. A
    future ``SidecarGatewayLauncher`` implements the cluster-mode adapter
    against the same contract.
    """

    def launch(self, endpoint: LLMGatewayEndpoint) -> GatewayHandle:
        """Start ``endpoint`` and return where it can be reached."""
        ...

    def teardown(self, handle: GatewayHandle) -> None:
        """Stop the endpoint previously started as ``handle``."""
        ...


class LocalHostGatewayLauncher:
    """Local-mode :class:`HostGatewayLauncher`: runs the gateway as a thread
    in the orchestrator process."""

    def __init__(self) -> None:
        self._endpoints: dict[int, LLMGatewayEndpoint] = {}

    def launch(self, endpoint: LLMGatewayEndpoint) -> GatewayHandle:
        handle = endpoint.start()
        self._endpoints[handle.port] = endpoint
        return handle

    def teardown(self, handle: GatewayHandle) -> None:
        endpoint = self._endpoints.pop(handle.port, None)
        if endpoint is not None:
            endpoint.stop()


class _GatewayHTTPServer(ThreadingHTTPServer):
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
        logger.debug("LLMGatewayEndpoint: " + format, *args)

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
            logger.exception("LLMGatewayEndpoint: upstream request failed for %s", self.path)
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
    "HarnessSpecLike",
    "GatewayHandle",
    "LLMGatewayEndpoint",
    "HostGatewayLauncher",
    "LocalHostGatewayLauncher",
    "GATEWAY_HOSTNAME",
]
