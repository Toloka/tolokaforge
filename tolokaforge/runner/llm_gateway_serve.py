"""Sidecar entrypoint for the coding-harness credential-shield gateway.

Runs :class:`~tolokaforge.runner.llm_gateway._GatewayHTTPServer` on a
fixed port inside the ``tolokaforge-llm-gateway`` compose service the
:class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver`
adds to every shielded trial stack. Config comes from environment
variables the driver bakes into the sidecar service's ``environment:``:

* ``TF_GATEWAY_UPSTREAM_URL`` — provider base URL. Required.
* ``TF_GATEWAY_UPSTREAM_TOKEN`` — the real credential the sidecar swaps
  in on every allow-listed request. Required.
* ``TF_GATEWAY_UPSTREAM_AUTH_HEADER`` — header name (e.g.
  ``Authorization`` or ``x-api-key``). Required.
* ``TF_GATEWAY_UPSTREAM_AUTH_TEMPLATE`` — value template with a
  ``{token}`` placeholder (e.g. ``Bearer {token}``). Required.
* ``TF_GATEWAY_PATH_ALLOWLIST`` — comma-separated request paths.
  Required.
* ``TF_GATEWAY_PORT`` — TCP port the sidecar binds. Defaults to 8080.

Credential handling. The ``TF_GATEWAY_UPSTREAM_TOKEN`` env var is
consumed exactly once at bootstrap — the same shape
``tolokaforge/runner/__main__.py`` uses to read
``TOLOKAFORGE_SECRETS_JSON`` — and immediately handed off to a scoped
:class:`~tolokaforge.secrets.SecretManager` (via
:func:`~tolokaforge.secrets.init_default_from` +
:func:`~tolokaforge.secrets.register_runtime_secret` +
:func:`~tolokaforge.secrets.install_global_redactor`). The real value is
then fetched off the manager for the server's own use and lands on the
log-redaction scrub set. Nothing else in this process reads env for a
credential.

The trial container reaches this sidecar by its compose service name —
:data:`~tolokaforge.runner.llm_gateway.GATEWAY_HOSTNAME` — on the
declared port. The sidecar's compose service is
:attr:`~tolokaforge.runner.models.EnvironmentManifest.bridged_services`,
so the netpolicy enforcement attaches it to BOTH the internal (isolated,
CLI-reachable) and edge (has egress) networks — the shield works
whichever ``NetworkPolicy`` the pack declared, including
``no_internet``.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from tolokaforge.runner.llm_gateway import _GatewayHTTPServer
from tolokaforge.secrets import (
    DictProvider,
    SecretManager,
    get_default,
    init_default_from,
    install_global_redactor,
    register_runtime_secret,
)

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
    populated from environment variables — the sidecar has no reason to
    import the harness registry."""

    upstream_url: str
    upstream_token_env_var: str
    upstream_auth_header: str
    upstream_auth_template: str
    path_allowlist: tuple[str, ...]


def _require_non_secret(name: str) -> str:
    """Read a non-credential env var — URL, header name, allowlist. Fail
    loud on empty/unset so a misconfigured sidecar exits at startup rather
    than serving 5xx forever."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"llm_gateway_serve: required environment variable {name} is empty or unset"
        )
    return value


def _bootstrap_secret_manager(token_env_var: str) -> str:
    """Read the real upstream credential from ``token_env_var`` exactly once,
    install it on a scoped :class:`SecretManager` as the process default,
    register it for log redaction, and return the resolved value for the
    server to hand upstream.

    Same shape as :mod:`tolokaforge.runner.__main__`'s
    ``TOLOKAFORGE_SECRETS_JSON`` bootstrap: one env read at boot to seed a
    :class:`DictProvider`, then every subsequent credential access goes
    through :func:`get_default`. Nothing else in this process reads env for
    a credential."""
    raw = os.environ.get(token_env_var, "").strip()
    if not raw:
        raise SystemExit(
            f"llm_gateway_serve: required credential env variable {token_env_var} is "
            "empty or unset"
        )
    init_default_from(SecretManager([DictProvider({token_env_var: raw})]))
    register_runtime_secret(token_env_var, raw)
    install_global_redactor()
    return get_default().get_secret_or_raise(token_env_var)


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
    upstream_token = _bootstrap_secret_manager(_ENV_UPSTREAM_TOKEN)
    gateway = _SidecarGatewayConfig(
        upstream_url=_require_non_secret(_ENV_UPSTREAM_URL),
        upstream_token_env_var=_ENV_UPSTREAM_TOKEN,
        upstream_auth_header=_require_non_secret(_ENV_UPSTREAM_AUTH_HEADER),
        upstream_auth_template=_require_non_secret(_ENV_UPSTREAM_AUTH_TEMPLATE),
        path_allowlist=_parse_allowlist(_require_non_secret(_ENV_PATH_ALLOWLIST)),
    )
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
