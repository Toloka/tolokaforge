"""Sidecar-mode entrypoint for the coding-harness credential-shield gateway.

Runs :class:`~tolokaforge.runner.llm_gateway._GatewayHTTPServer` on a
fixed port inside a compose sidecar. Config comes from environment
variables the driver bakes into the sidecar's compose ``environment:``:

* ``TF_GATEWAY_UPSTREAM_URL`` — provider base URL, no trailing slash
  required. Required.
* ``TF_GATEWAY_UPSTREAM_TOKEN`` — the real credential the sidecar swaps
  in on every allow-listed request. Required.
* ``TF_GATEWAY_UPSTREAM_AUTH_HEADER`` — header name (e.g. ``Authorization``
  or ``x-api-key``). Required.
* ``TF_GATEWAY_UPSTREAM_AUTH_TEMPLATE`` — value template with a ``{token}``
  placeholder (e.g. ``Bearer {token}``). Required.
* ``TF_GATEWAY_PATH_ALLOWLIST`` — comma-separated list of allow-listed
  request paths. Required.
* ``TF_GATEWAY_PORT`` — TCP port the sidecar binds. Defaults to 8080.

The trial container reaches this sidecar by its compose service name —
:data:`~tolokaforge.runner.llm_gateway.GATEWAY_HOSTNAME` — on the
declared port. Unlike the host-mode launcher, the sidecar mode lives on
the same compose network as the CLI's container, so no ``extra_hosts``
mapping is needed and the shield works under every network policy the
netpolicy enforcement produces — including ``no_internet`` — provided
the sidecar's service is bridged (:attr:`~tolokaforge.runner.models\
.EnvironmentManifest.bridged_services`) so it also has edge egress.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from tolokaforge.runner.llm_gateway import _GatewayHTTPServer

logger = logging.getLogger(__name__)

_ENV_UPSTREAM_URL = "TF_GATEWAY_UPSTREAM_URL"
_ENV_UPSTREAM_TOKEN = "TF_GATEWAY_UPSTREAM_TOKEN"  # noqa: S105  env-var name, not a secret
_ENV_UPSTREAM_AUTH_HEADER = "TF_GATEWAY_UPSTREAM_AUTH_HEADER"
_ENV_UPSTREAM_AUTH_TEMPLATE = "TF_GATEWAY_UPSTREAM_AUTH_TEMPLATE"  # noqa: S105
_ENV_PATH_ALLOWLIST = "TF_GATEWAY_PATH_ALLOWLIST"
_ENV_PORT = "TF_GATEWAY_PORT"

_DEFAULT_PORT = 8080


@dataclass(frozen=True)
class _SidecarGatewayConfig:
    """Stub :class:`~tolokaforge.runner.llm_gateway.CredentialGatewayConfig`
    structurally matched to the server's Protocol contract — populated
    from environment variables rather than from ``HarnessSpec`` since the
    sidecar has no reason to import the harness registry."""

    upstream_url: str
    upstream_token_env_var: str  # unused inside the server, but part of the shape
    upstream_auth_header: str
    upstream_auth_template: str
    path_allowlist: tuple[str, ...]


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"llm_gateway_serve: required environment variable {name} is empty or unset"
        )
    return value


def _parse_allowlist(raw: str) -> tuple[str, ...]:
    entries = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    if not entries:
        raise SystemExit(f"llm_gateway_serve: {_ENV_PATH_ALLOWLIST} must list at least one path")
    return entries


def _parse_port(raw: str | None) -> int:
    if not raw:
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"llm_gateway_serve: {_ENV_PORT}={raw!r} is not an integer") from exc
    if not (1 <= port <= 65535):
        raise SystemExit(f"llm_gateway_serve: {_ENV_PORT}={port} out of TCP range [1, 65535]")
    return port


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s llm_gateway_serve %(message)s",
    )
    gateway = _SidecarGatewayConfig(
        upstream_url=_require(_ENV_UPSTREAM_URL),
        upstream_token_env_var=_ENV_UPSTREAM_TOKEN,
        upstream_auth_header=_require(_ENV_UPSTREAM_AUTH_HEADER),
        upstream_auth_template=_require(_ENV_UPSTREAM_AUTH_TEMPLATE),
        path_allowlist=_parse_allowlist(_require(_ENV_PATH_ALLOWLIST)),
    )
    upstream_token = _require(_ENV_UPSTREAM_TOKEN)
    port = _parse_port(os.environ.get(_ENV_PORT))

    server = _GatewayHTTPServer(("0.0.0.0", port), gateway, upstream_token)
    logger.info(
        "listening port=%d upstream=%s allowlist=%s",
        port,
        gateway.upstream_url,
        ",".join(gateway.path_allowlist),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover — entrypoint
    main()
    sys.exit(0)
