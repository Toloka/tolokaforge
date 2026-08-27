"""Compose-materialisation primitives shared by runtime backends.

The concrete `RuntimeBackend` implementations that materialise a task's
``environment_manifest`` via testcontainers-python
(``PerTrialRuntimeBackend`` and ``SharedStackRuntimeBackend``) share the
same lifecycle primitives: copy the compose context into an isolated
project directory,
start the stack with ``.start()``, resolve host-side endpoints via
``get_service_host_and_port``, and tear the stack down with
``.stop(down=True)``.

This module holds those primitives as pure functions so both backends call
into one place. Lifecycle differs by backend — per-trial constructs one
project per trial; shared constructs one project per run — but the mechanics
of "get a compose stack up, resolve endpoints, shut it down" are the same.

Contains no runtime-backend concepts (no trial_id, no per-trial cache, no
handles). The backends layer those on top.
"""

from __future__ import annotations

import copy
import logging
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from testcontainers.compose import DockerCompose
from testcontainers.compose.compose import ComposeContainer

from tolokaforge.core.netpolicy_constants import (
    NETPOLICY_EDGE_NETWORK,
    NETPOLICY_INTERNAL_NETWORK,
    NETPOLICY_PROXY_IMAGE,
    NETPOLICY_PROXY_PORT,
    NETPOLICY_PROXY_SERVICE,
)
from tolokaforge.core.run_display_events import ContainerSnapshot
from tolokaforge.core.trial import EnvEndpoints, NetworkPolicy
from tolokaforge.runner.compose_naming import compose_trial_slug
from tolokaforge.runner.models import bind_mount_source
from tolokaforge.secrets import (
    CONTAINER_SECRETS_ENV_VAR,
    compose_escaped,
    container_secrets_env,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint-resolution conventions
# ---------------------------------------------------------------------------


RUNNER_PORT_DEFAULT = 50051
"""gRPC port the tolokaforge runner container listens on. Matches
``GrpcRunnerClient``'s default ``runner:50051``. Convention default for
``manifest.runner_port``, which task-pack authors override via
``stack.runner_port``."""

DB_SERVICE_DEFAULT = "db-service"
"""Compose service name convention for the HTTP JSON state backend the
runner's tool layer POSTs to (the in-memory-sqlite service that lives
behind the ``/query|/update|/sql|/schema`` endpoints)."""

DB_SERVICE_PORT_DEFAULT = 8000
"""HTTP port ``db-service`` listens on. Matches the port the built-in
core stack exposes and the port task compose files publish for the
``tolokaforge-db-service:local`` alias."""

RAG_SERVICE_CANDIDATES = ("rag", "rag-service")
"""Compose service names checked when resolving the RAG endpoint.
First match wins; ``None`` returned if no match is exposed."""


# ---------------------------------------------------------------------------
# Network-policy enforcement
# ---------------------------------------------------------------------------


SQUID_CONFIG_FILENAME = "squid.conf"
"""Basename of the generated squid config written into the copied project
context dir; the proxy service bind-mounts it read-only."""

SQUID_CONFIG_CONTAINER_PATH = "/etc/squid/squid.conf"
"""Absolute path the generated config is mounted at inside the proxy — a
complete config replacing the image default, so the effective ``http_access``
ordering does not depend on the image's ``conf.d`` include order."""


def enforce_network_policy(
    compose_doc: dict[str, Any],
    policy: NetworkPolicy,
    runner_service: str,
    allowlist: list[str],
    restricted_services: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return a compose doc rewritten to enforce ``policy``.

    ``full_internet`` returns ``compose_doc`` unchanged (same object);
    ``allowlist`` and ``restricted_services`` are ignored. ``no_internet``
    returns a deep copy in which every service **not named in
    ``restricted_services``** joins the injected
    :data:`NETPOLICY_INTERNAL_NETWORK` (``internal: true`` — no egress),
    any task-declared network is forced ``internal: true`` too, and
    ``runner_service`` additionally joins the non-internal
    :data:`NETPOLICY_EDGE_NETWORK`; ``allowlist`` is ignored.

    ``limited_internet`` returns a deep copy that keeps the same internal/edge
    topology but additionally injects the :data:`NETPOLICY_PROXY_SERVICE`
    forward proxy (on both networks) and points every non-restricted
    application service's ``HTTP(S)_PROXY`` at it; restricted services get
    neither the injected-net attach nor the proxy env — they see only the
    networks their compose entry declares. The runner keeps direct edge egress
    and is not proxied. The proxy's squid config — which encodes ``allowlist``
    — is written separately by :func:`apply_network_policy_to_compose_file` /
    :func:`render_squid_config`; this function only rewrites topology.

    ``runner_service`` is guaranteed present in ``services``, and never
    contained in ``restricted_services``, by :class:`EnvironmentManifest`
    validation.
    """
    if policy is NetworkPolicy.FULL_INTERNET:
        return compose_doc
    doc = copy.deepcopy(compose_doc)
    if policy is NetworkPolicy.LIMITED_INTERNET:
        return _enforce_limited_internet(doc, runner_service, allowlist, restricted_services)
    return _enforce_no_internet(doc, runner_service, restricted_services)


def _inject_isolation_networks(doc: dict[str, Any]) -> None:
    """Force every task-declared network ``internal: true`` and inject the
    internal (no-egress) + edge (egress) networks. Shared by ``no_internet``
    and ``limited_internet`` — both isolate app services on the internal net
    and reserve edge egress for the runner (plus, under limited, the proxy)."""
    networks: dict[str, Any] = doc.setdefault("networks", {})
    for name, config in networks.items():
        merged = dict(config) if isinstance(config, dict) else {}
        merged["internal"] = True
        networks[name] = merged
    networks[NETPOLICY_INTERNAL_NETWORK] = {"internal": True}
    networks[NETPOLICY_EDGE_NETWORK] = {}


def _enforce_no_internet(
    doc: dict[str, Any], runner_service: str, restricted_services: frozenset[str]
) -> dict[str, Any]:
    _inject_isolation_networks(doc)
    services: dict[str, Any] = doc["services"]
    for service_name, service in services.items():
        if service_name in restricted_services:
            continue
        attachments = [NETPOLICY_INTERNAL_NETWORK]
        if service_name == runner_service:
            attachments.append(NETPOLICY_EDGE_NETWORK)
        service["networks"] = _merge_service_networks(service.get("networks"), attachments)
    return doc


def _enforce_limited_internet(
    doc: dict[str, Any],
    runner_service: str,
    allowlist: list[str],
    restricted_services: frozenset[str],
) -> dict[str, Any]:
    if not allowlist:
        raise ValueError(
            "limited_internet enforcement requires a non-empty allowlist; "
            "EnvironmentManifest validation guarantees one is present."
        )
    _inject_isolation_networks(doc)
    services: dict[str, Any] = doc["services"]
    no_proxy = ",".join([*services, NETPOLICY_PROXY_SERVICE, "localhost", "127.0.0.1"])
    proxy_url = f"http://{NETPOLICY_PROXY_SERVICE}:{NETPOLICY_PROXY_PORT}"
    for service_name, service in services.items():
        if service_name in restricted_services:
            continue
        if service_name == runner_service:
            service["networks"] = _merge_service_networks(
                service.get("networks"), [NETPOLICY_INTERNAL_NETWORK, NETPOLICY_EDGE_NETWORK]
            )
            continue
        service["networks"] = _merge_service_networks(
            service.get("networks"), [NETPOLICY_INTERNAL_NETWORK]
        )
        service["environment"] = _merge_proxy_env(service.get("environment"), proxy_url, no_proxy)
    services[NETPOLICY_PROXY_SERVICE] = _proxy_service_definition()
    return doc


def _proxy_service_definition() -> dict[str, Any]:
    """Compose definition for the injected squid forward proxy: digest-pinned
    image, the generated config bind-mounted read-only, both isolation
    networks, and a TCP-liveness health probe so ``docker compose up --wait``
    blocks until squid is listening on :data:`NETPOLICY_PROXY_PORT`."""
    return {
        "image": NETPOLICY_PROXY_IMAGE,
        "volumes": [f"./{SQUID_CONFIG_FILENAME}:{SQUID_CONFIG_CONTAINER_PATH}:ro"],
        "networks": [NETPOLICY_INTERNAL_NETWORK, NETPOLICY_EDGE_NETWORK],
        "healthcheck": {
            "test": ["CMD", "bash", "-c", f"echo > /dev/tcp/127.0.0.1/{NETPOLICY_PROXY_PORT}"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
            "start_period": "3s",
        },
    }


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def _merge_service_env(existing: Any, values: Mapping[str, str]) -> Any:
    """Return a service-level ``environment:`` value carrying ``values`` on top
    of ``existing``, preserving its declared shape (mapping or ``KEY=value``
    list). Absent ``existing`` yields a mapping.

    A new object every time: ``yaml.safe_load`` resolves a YAML alias to the
    same object, so an anchored ``environment:`` mapping shared between
    services would carry an in-place update into every service aliasing it.
    Callers assign the return value onto the one service they mean.

    List-shape handling drops any prior ``KEY=…`` entry whose ``KEY`` is in
    ``values`` before appending the new entries. Docker's last-wins semantics
    for env lists would otherwise leave a duplicate leftover that reads as a
    bug to a compose-file auditor, and the mapping branch's ``{**a, **b}``
    already has the clean-overwrite semantics we want here.
    """
    if isinstance(existing, list):
        override_keys = set(values.keys())
        filtered = [
            entry
            for entry in existing
            if not (isinstance(entry, str) and entry.split("=", 1)[0] in override_keys)
        ]
        return [*filtered, *(f"{key}={value}" for key, value in values.items())]
    if isinstance(existing, Mapping):
        return {**existing, **values}
    return dict(values)


def _merge_proxy_env(existing: Any, proxy_url: str, no_proxy: str) -> Any:
    """Add the proxy env vars to a service-level ``environment:`` value.

    Any ``NO_PROXY`` / ``no_proxy`` value already carried by ``existing`` is
    unioned into the merged value (comma-separated, order-preserving with
    dedup) rather than overwritten. A caller upstream — the coding-harness
    driver's credential shield, for example — can pre-declare a hostname it
    needs to bypass the squid forward proxy for (its host-side gateway,
    reachable via ``extra_hosts`` but not resolvable inside squid's own
    container), and the netpolicy enforcement respects it here.
    """
    caller_no_proxy = _extract_no_proxy(existing)
    merged_no_proxy = _union_no_proxy(caller_no_proxy, no_proxy)
    values = dict.fromkeys(_PROXY_ENV_KEYS, proxy_url)
    values.update(dict.fromkeys(_NO_PROXY_KEYS, merged_no_proxy))
    return _merge_service_env(existing, values)


def _extract_no_proxy(existing: Any) -> str:
    """Read whatever NO_PROXY value ``existing`` already carries (mapping or
    ``KEY=value`` list shape). Returns an empty string when absent."""
    if isinstance(existing, Mapping):
        for key in _NO_PROXY_KEYS:
            value = existing.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if isinstance(existing, list):
        for entry in existing:
            if not isinstance(entry, str) or "=" not in entry:
                continue
            key, _, value = entry.partition("=")
            if key in _NO_PROXY_KEYS and value:
                return value
    return ""


def _union_no_proxy(caller: str, netpolicy: str) -> str:
    """Order-preserving union of two comma-separated no_proxy strings."""
    seen: dict[str, None] = {}
    for source in (caller, netpolicy):
        for token in source.split(","):
            trimmed = token.strip()
            if trimmed and trimmed not in seen:
                seen[trimmed] = None
    return ",".join(seen)


def render_squid_config(allowlist: list[str], service_names: list[str]) -> str:
    """Render a complete, default-deny ``squid.conf`` for the forward proxy.

    Egress is permitted only to ``allowlist`` hosts (exact ``api.host`` →
    ``dstdomain api.host``; leading-wildcard ``*.host`` → squid domain-suffix
    ``dstdomain .host``) and, as inter-service defence-in-depth, the compose
    ``service_names``. CONNECT tunnels are restricted to port 443 and non-CONNECT
    requests to ports 80 and 443; every other destination hits the terminal
    ``http_access deny all``. Raises
    ``ValueError`` on an allowlist entry that is not an exact or single
    leading-wildcard hostname (unreachable after manifest validation)."""
    lines = [
        "# Generated by tolokaforge network_policy=limited_internet enforcement.",
        "# Default-deny forward proxy: egress permitted only to the declared allowlist.",
        f"visible_hostname {NETPOLICY_PROXY_SERVICE}",
        f"http_port {NETPOLICY_PROXY_PORT}",
        "",
        "acl SSL_ports port 443",
        "acl Safe_ports port 80 443",
        "acl CONNECT method CONNECT",
        "",
    ]
    lines += [f"acl allowed_dsts dstdomain {_squid_dstdomain(entry)}" for entry in allowlist]
    lines += [f"acl internal_dsts dstdomain {name}" for name in service_names]
    lines += [
        "",
        "http_access deny CONNECT !SSL_ports",
        "http_access deny !Safe_ports",
        "http_access allow allowed_dsts",
        "http_access allow internal_dsts",
        "http_access deny all",
        "",
        "cache deny all",
    ]
    return "\n".join(lines) + "\n"


def _squid_dstdomain(entry: str) -> str:
    """Map one validated allowlist entry to its squid ``dstdomain`` argument.
    Defensive: rejects schemes, ports, paths, whitespace, and interior
    wildcards that manifest validation should already have excluded."""
    if not entry or any(char in entry for char in " \t/:") or "://" in entry:
        raise ValueError(f"limited_internet allowlist entry not a bare hostname: {entry!r}")
    if entry.startswith("*."):
        suffix = entry[2:]
        if not suffix or "*" in suffix:
            raise ValueError(
                f"limited_internet allowlist entry has a malformed wildcard: {entry!r}"
            )
        return f".{suffix}"
    if "*" in entry:
        raise ValueError(f"limited_internet allowlist entry has a non-leading wildcard: {entry!r}")
    return entry


def _merge_service_networks(existing: Any, additions: list[str]) -> Any:
    """Add each name in ``additions`` to a service-level ``networks:`` value,
    preserving its declared shape (list or mapping) and any per-network config
    (aliases, static IPs). Absent ``existing`` yields a plain list. Idempotent:
    a name already present is left untouched."""
    if isinstance(existing, dict):
        merged = dict(existing)
        for name in additions:
            if name not in merged:
                merged[name] = None
        return merged
    current = list(existing) if isinstance(existing, list) else []
    for name in additions:
        if name not in current:
            current.append(name)
    return current


def apply_network_policy_to_compose_file(
    compose_file: Path,
    policy: NetworkPolicy,
    runner_service: str,
    allowlist: list[str],
    restricted_services: frozenset[str] = frozenset(),
) -> None:
    """Read ``compose_file``, apply :func:`enforce_network_policy`, and write
    the result back in place. ``full_internet`` leaves the file byte-identical
    (the transform is identity), so its comments and formatting survive.

    For ``limited_internet`` the rendered ``squid.conf`` is written alongside
    the compose file (into ``compose_file.parent`` — the copied project
    context dir), so the injected proxy service's relative bind mount
    resolves."""
    with compose_file.open() as f:
        doc = yaml.safe_load(f)
    transformed = enforce_network_policy(
        doc, policy, runner_service, allowlist, restricted_services
    )
    if transformed is doc:
        return
    with compose_file.open("w") as f:
        yaml.safe_dump(transformed, f, sort_keys=False)
    if policy is NetworkPolicy.LIMITED_INTERNET:
        squid_conf = render_squid_config(allowlist, list(doc["services"]))
        (compose_file.parent / SQUID_CONFIG_FILENAME).write_text(squid_conf)


DOCKER_SOCKET_PATH = "/var/run/docker.sock"
"""Host docker socket, bind-mounted into the runner for compose-variant tools."""


def _volumes_mount_docker_socket(volumes: Iterable[Any]) -> bool:
    """True iff ``volumes`` already bind-mounts :data:`DOCKER_SOCKET_PATH`.

    Handles both compose volume forms: the short ``"src:dst[:mode]"`` string and
    the long ``{"source": ..., "target": ...}`` mapping."""
    for entry in volumes:
        if isinstance(entry, str) and entry.split(":", 1)[0] == DOCKER_SOCKET_PATH:
            return True
        if isinstance(entry, Mapping) and entry.get("source") == DOCKER_SOCKET_PATH:
            return True
    return False


def mount_docker_socket_into_runner(compose_file: Path, runner_service: str) -> None:
    """Bind-mount the host docker socket into ``runner_service`` in place.

    The compose-variant ``bash_session`` / ``str_replace_editor`` tools run on
    the runner and ``docker exec`` into a sibling service, so the runner must
    reach the docker daemon. The runner image carries the docker CLI (baked in
    when ``_run_needs_docker_cli`` fires), but the socket is a runtime bind mount
    the task-declared compose file does not supply — materialisation injects it
    here on the same trigger. Idempotent: an existing socket mount is left as-is.

    The extended list is assigned, never appended to in place: ``yaml.safe_load``
    resolves a YAML alias to the same object, so an anchored ``volumes:`` list
    shared with a sibling service would otherwise hand that sibling the socket
    too.
    """
    with compose_file.open() as f:
        doc = yaml.safe_load(f)
    service = doc["services"][runner_service]
    volumes = service.get("volumes") or []
    if _volumes_mount_docker_socket(volumes):
        return
    service["volumes"] = [*volumes, f"{DOCKER_SOCKET_PATH}:{DOCKER_SOCKET_PATH}"]
    with compose_file.open("w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)


CREDENTIALLED_COMPOSE_MODE = 0o600
"""Mode the materialised compose file is left at once it carries the credential
payload in cleartext. ``copy_compose_context`` uses ``shutil.copy2``, which
carries the repo file's mode in; the temp project dir is already ``0700``."""


def _service_declares_env_var(environment: Any, name: str) -> bool:
    """True iff a service-level ``environment:`` value declares ``name`` — as a
    mapping key, as a ``KEY=value`` list entry, or as a bare ``KEY``
    pass-through list entry."""
    if isinstance(environment, Mapping):
        return name in environment
    if isinstance(environment, list):
        return any(
            isinstance(entry, str) and entry.split("=", 1)[0] == name for entry in environment
        )
    return False


def _exposing_bind_mount(volumes: Iterable[Any], compose_file_name: str) -> str | None:
    """Return the first bind-mount source that reaches the credentialled compose
    file — the project context root, or the file itself — or ``None``.

    Sources are relative to the context dir; absolute sources and ``..``
    segments are already refused by ``EnvironmentManifest`` validation, so
    normalising is enough to recognise both shapes (``.``, ``./`` and
    ``./<compose file>`` all collapse here)."""
    for entry in volumes:
        source = bind_mount_source(entry)
        if source is None:
            continue
        if PurePosixPath(source).parts in ((), (compose_file_name,)):
            return source
    return None


def _refuse_credential_exposure(services: Mapping[str, Any], compose_file: Path) -> None:
    """Raise ``ValueError`` on a compose doc no service may receive the payload
    under: one declaring the engine-owned variable itself, or one whose bind
    mount would read the credentialled file out of the project context dir."""
    for service_name, service in services.items():
        if _service_declares_env_var(service.get("environment"), CONTAINER_SECRETS_ENV_VAR):
            raise ValueError(
                f"compose service {service_name!r} declares {CONTAINER_SECRETS_ENV_VAR}; "
                f"the variable is engine-owned and supplied at materialisation — "
                f"delete the entry from the compose file."
            )
        exposing = _exposing_bind_mount(service.get("volumes") or [], compose_file.name)
        if exposing is not None:
            raise ValueError(
                f"compose service {service_name!r} bind-mounts {exposing!r}, which reaches "
                f"the materialised compose file — that file carries the engine's credential "
                f"payload. Mount the specific paths the service needs from a subdirectory "
                f"instead of the project context root."
            )


def inject_runner_credentials(compose_file: Path, runner_service: str) -> None:
    """Give ``runner_service`` the host's credential payload, in place.

    The task-declared stack's runner runs the same in-container LLM-as-judge
    grading the engine-built stack's runner does, so it reads the same
    :func:`~tolokaforge.secrets.container_secrets_env` entry. Only
    ``runner_service`` receives it.

    ``$`` is doubled in the injected value: Docker Compose interpolates ``$``
    in ``environment`` values, so a credential containing one is otherwise
    silently truncated. The escape belongs to this path alone — the
    engine-built stack hands its value to the Docker SDK, which interpolates
    nothing. The written value is therefore *not* the value the log redactor
    holds, so it does not scrub out of a compose error surfaced verbatim; the
    redactor snapshot carries the doubled form too (see
    ``tolokaforge.secrets.log_filter``).

    Raises ``ValueError`` on a compose doc that would defeat "only the runner
    holds the payload": any service declaring
    :data:`~tolokaforge.secrets.CONTAINER_SECRETS_ENV_VAR` itself (the variable
    is engine-owned; a pack declaring it either commits a credential or shadows
    the engine's payload), and any service bind-mounting the project context
    root or the compose file, which would read the payload back off disk.
    Also raises when ``runner_service`` is absent from the compose doc —
    unreachable through :class:`EnvironmentManifest`, whose validator
    guarantees the service exists, and kept so an internal inconsistency is
    loud rather than silently credential-less. Nothing is written on any
    refusal, and a host that resolves no secrets leaves the file
    byte-identical.
    """
    with compose_file.open() as f:
        doc = yaml.safe_load(f)
    services: dict[str, Any] = doc["services"]
    _refuse_credential_exposure(services, compose_file)
    if runner_service not in services:
        raise ValueError(
            f"runner_service {runner_service!r} is not declared in the compose file "
            f"{compose_file.name!r}; declared services are {sorted(services)!r}."
        )
    payload = container_secrets_env()
    if not payload:
        return
    escaped = {key: compose_escaped(value) for key, value in payload.items()}
    services[runner_service]["environment"] = _merge_service_env(
        services[runner_service].get("environment"), escaped
    )
    with compose_file.open("w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    compose_file.chmod(CREDENTIALLED_COMPOSE_MODE)


# ---------------------------------------------------------------------------
# Filesystem + project layout
# ---------------------------------------------------------------------------


def make_project_temp_dir(prefix_slug: str) -> Path:
    """Create a temp directory whose basename embeds ``prefix_slug``.

    Docker Compose auto-generates a project name from the context
    directory basename; encoding a caller-supplied slug (a trial id for
    per-trial, a run id for shared) into that basename gives each
    materialisation a unique compose project. Sanitisation delegates to
    :func:`compose_trial_slug` so the host-side project name and the
    runner-side compose-exec container resolver share one definition.
    """
    return Path(tempfile.mkdtemp(prefix=f"tolokaforge-{compose_trial_slug(prefix_slug)}-"))


# ---------------------------------------------------------------------------
# Per-trial compose variables (docker compose ``.env``)
# ---------------------------------------------------------------------------


TOLOKAFORGE_ENV_PREFIX = "TOLOKAFORGE_"
"""Reserved prefix for engine-authored variables written into the per-trial
``.env``. Task-declared ``stack_inputs`` keys that begin with this prefix are
rejected before provision starts — a task overriding an engine-reserved
variable would be silent shadowing."""

TOLOKAFORGE_TRIAL_SLUG_ENV = "TOLOKAFORGE_TRIAL_SLUG"
"""Reserved compose variable that resolves to :func:`compose_trial_slug` of the
trial id. Task compose files interpolate ``${TOLOKAFORGE_TRIAL_SLUG}`` into
``container_name:`` to make container names per-trial unique — the same slug
the per-trial temp-dir basename embeds, so the wrapper's exec targets the
container the stack actually brought up."""


def write_compose_env_file(
    context_dir: Path, *, trial_id: str, stack_inputs: Mapping[str, str]
) -> None:
    """Write the per-trial compose ``.env`` into ``context_dir``.

    Docker Compose reads ``<project-dir>/.env`` automatically on every command
    invoked with that project directory as its context. Content order (later
    entries win, per compose's own ``.env`` resolution):

    1. any ``.env`` already copied into ``context_dir`` from the task-authored
       compose folder — preserved verbatim so task defaults survive;
    2. ``stack_inputs`` — the manifest's task-declared variables;
    3. the engine-reserved block — currently just
       :data:`TOLOKAFORGE_TRIAL_SLUG_ENV`.

    Values are written with the raw shell-safe form ``KEY=value``; validation
    of the reserved-prefix rule runs at the call site
    (:class:`PerTrialRuntimeBackend.provision`) *before* the compose
    lifecycle's ``try`` block, so a violation reads as a manifest error not
    a compose-up failure.
    """
    lines: list[str] = []
    existing = context_dir / ".env"
    if existing.exists():
        lines.append(existing.read_text().rstrip("\n"))
    for key, value in stack_inputs.items():
        lines.append(f"{key}={value}")
    lines.append(f"{TOLOKAFORGE_TRIAL_SLUG_ENV}={compose_trial_slug(trial_id)}")
    existing.write_text("\n".join(lines) + "\n")


def copy_compose_context(compose_file: Path, dest_dir: Path) -> None:
    """Copy the compose file (and its directory's sibling files) to
    ``dest_dir`` so Docker Compose sees an isolated context with a
    unique project-name basename. Bind mount source paths declared with
    relative paths inside the original compose file are resolved
    relative to the context directory, so copying the whole directory
    preserves them.
    """
    src_dir = compose_file.parent
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True)
    for entry in src_dir.iterdir():
        target = dest_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def resolve_host_port(
    compose: DockerCompose, service_name: str, container_port: int
) -> tuple[str | None, int | None]:
    """Look up the host + host-side port that map to ``service_name``'s
    ``container_port``. Returns ``(None, None)`` if the service or the
    port is not exposed.

    Testcontainers raises varied exception types (``KeyError``,
    ``ValueError``, ``NoSuchPortExposed``, and any ``subprocess`` error
    a docker call can surface). Catch broadly and treat every failure
    as "not exposed" — but ``logger.debug`` the exception so a genuine
    daemon issue is diagnosable (otherwise it silently reads as a
    compose-file misconfiguration downstream).

    Testcontainers returns ``0.0.0.0`` as the host on macOS / Linux —
    correct as a listen address inside the container, but not reachable
    as a client host from the orchestrator process. Rewrite it to
    ``localhost`` so a gRPC/HTTP client on the host can actually
    connect."""
    try:
        host, port = compose.get_service_host_and_port(
            service_name=service_name, port=container_port
        )
    except Exception as exc:  # noqa: BLE001 — testcontainers raises varied types
        logger.debug(
            "compose_materialisation: service %r port %d not resolvable: %s",
            service_name,
            container_port,
            exc,
        )
        return None, None
    if (
        host == "0.0.0.0"
    ):  # noqa: S104 — comparing to (not binding) the testcontainers macOS/Linux return value
        host = "localhost"
    return host, port


def first_published_port(container: Any) -> int | None:
    """Extract the first *host-published* container-side port from a
    Testcontainers ``ComposeContainer``. Returns ``None`` when nothing
    is published (or the shape is not what we expect).

    ``docker compose ps`` lists a Dockerfile ``EXPOSE``d-but-unpublished port
    as a publisher with ``PublishedPort == 0``; such an entry is skipped so the
    returned container port is one that actually maps to a host port (a bare
    ``image: nginx`` service otherwise reports its exposed ``80`` ahead of the
    port the compose file publishes)."""
    ports = getattr(container, "Publishers", None) or []
    for entry in ports:
        target = getattr(entry, "TargetPort", None)
        published = getattr(entry, "PublishedPort", None)
        if isinstance(target, int) and target > 0 and isinstance(published, int) and published > 0:
            return target
    return None


def capture_container_diagnostics(
    compose: DockerCompose, service_name: str
) -> tuple[dict[str, str], tuple[str, ...], dict[str, str]]:
    """Best-effort docker-side view of where ``service_name`` actually listens.

    Returns ``(docker_port_map, container_listen_addrs, per_network_ips)``:

    * ``docker_port_map`` — ``{"<container_port>/<proto>": "<host_ip>:<host_port>"}``
      from the container's compose publishers.
    * ``container_listen_addrs`` — human-readable ``host:port`` (IPv6 bracketed)
      for every LISTEN socket in the container's ``/proc/net/tcp[6]``, so a
      loopback-only bind is visible next to a wildcard one.
    * ``per_network_ips`` — ``{network_name: ip}`` from ``docker inspect``.

    Assembled at readiness-gate failure time, against the still-running
    container, so the ``ProvisionError`` names the mechanism (e.g. a service
    that listens on ``127.0.0.1`` while its port is published to the host).
    Never raises: each capture is independently guarded and degrades to an
    empty structure, mirroring :func:`_fetch_service_logs`. A partial docker
    failure yields partial diagnostics but never masks the readiness failure
    behind an introspection error.
    """
    try:
        container = compose.get_container(service_name=service_name)
    except Exception as exc:  # noqa: BLE001 — service may be gone; diagnostics are best-effort
        logger.debug(
            "compose_materialisation: container for %r not resolvable: %s", service_name, exc
        )
        return {}, (), {}
    port_map = _docker_port_map(container)
    container_id = getattr(container, "ID", None) or ""
    if not container_id:
        return port_map, (), {}
    return port_map, _container_listen_addrs(container_id), _container_network_ips(container_id)


def _docker_port_map(container: Any) -> dict[str, str]:
    """Map ``<container_port>/<proto>`` to ``<host_ip>:<host_port>`` from a
    container's compose publishers. Empty when nothing is published."""
    port_map: dict[str, str] = {}
    for publisher in getattr(container, "Publishers", None) or []:
        target = getattr(publisher, "TargetPort", None)
        published = getattr(publisher, "PublishedPort", None)
        if target is None or published is None:
            continue
        proto = getattr(publisher, "Protocol", None) or "tcp"
        host_ip = getattr(publisher, "URL", None) or "0.0.0.0"  # noqa: S104 — report only
        port_map[f"{target}/{proto}"] = f"{host_ip}:{published}"
    return port_map


def _container_listen_addrs(container_id: str) -> tuple[str, ...]:
    """Decode LISTEN sockets from a container's ``/proc/net/tcp`` + ``tcp6``.

    Runs ``docker exec <id> cat /proc/net/tcp /proc/net/tcp6`` and returns the
    de-duplicated ``host:port`` listen addresses (IPv6 bracketed). Empty tuple
    on any docker/parse failure — diagnostics are best-effort."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "cat", "/proc/net/tcp", "/proc/net/tcp6"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; subprocess raises varied types
        logger.debug(
            "compose_materialisation: listen-addr capture for %s failed: %s", container_id, exc
        )
        return ()
    return _parse_proc_net_listen(result.stdout)


def _parse_proc_net_listen(proc_net_output: str) -> tuple[str, ...]:
    """Extract LISTEN (state ``0A``) local addresses from ``/proc/net/tcp[6]``
    text, de-duplicated in first-seen order. The header line is skipped
    naturally: its 4th column is the string ``local_address``, not ``0A``."""
    addrs: list[str] = []
    for line in proc_net_output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        decoded = _decode_proc_net_endpoint(fields[1])
        if decoded is not None:
            addrs.append(decoded)
    return tuple(dict.fromkeys(addrs))


def _decode_proc_net_endpoint(token: str) -> str | None:
    """Decode one ``<hex_ip>:<hex_port>`` ``/proc/net`` local-address token to
    ``host:port`` (IPv6 bracketed). ``None`` on a malformed token."""
    hex_ip, _, hex_port = token.partition(":")
    if not hex_port:
        return None
    try:
        port = int(hex_port, 16)
    except ValueError:
        return None
    ip = _decode_proc_net_ip(hex_ip)
    if ip is None:
        return None
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _decode_proc_net_ip(hex_ip: str) -> str | None:
    """Decode a ``/proc/net/tcp[6]`` hex address to a printable IP. The kernel
    stores each 32-bit word little-endian, so bytes are reversed per word
    before :func:`socket.inet_ntop`. ``None`` on an unexpected width."""
    try:
        raw = bytes.fromhex(hex_ip)
    except ValueError:
        return None
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    if len(raw) == 16:
        network_order = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, network_order)
    return None


def _container_network_ips(container_id: str) -> dict[str, str]:
    """Return ``{network_name: ip}`` for a container via ``docker inspect``.
    Empty on any docker/parse failure — diagnostics are best-effort."""
    fmt = "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", fmt, container_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; subprocess raises varied types
        logger.debug(
            "compose_materialisation: network-ip capture for %s failed: %s", container_id, exc
        )
        return {}
    ips: dict[str, str] = {}
    for token in result.stdout.split():
        name, sep, ip = token.partition("=")
        if sep and name and ip:
            ips[name] = ip
    return ips


def resolve_rag_url(
    compose: DockerCompose,
    *,
    rag_service: str | None = None,
    rag_port: int | None = None,
) -> str | None:
    """Best-effort ``rag_url`` — resolve a RAG service from the compose
    stack. Returns ``None`` when no such service is declared or its port
    is not exposed. Lookup failures are treated as "no rag" but
    ``logger.debug``-logged for diagnosability.

    ``rag_service`` scans only that named service; ``None`` scans the
    :data:`RAG_SERVICE_CANDIDATES` convention (first match wins).
    ``rag_port`` pins the container port; ``None`` auto-detects the first
    published port."""
    candidates = (rag_service,) if rag_service is not None else RAG_SERVICE_CANDIDATES
    for candidate in candidates:
        try:
            container = compose.get_container(service_name=candidate)
        except Exception as exc:  # noqa: BLE001 — service not declared
            logger.debug(
                "compose_materialisation: rag candidate %r not in compose: %s",
                candidate,
                exc,
            )
            continue
        container_port = rag_port if rag_port is not None else first_published_port(container)
        if container_port is None:
            continue
        host, port = resolve_host_port(compose, candidate, container_port)
        if host is None or port is None:
            continue
        return f"http://{host}:{port}"
    return None


def resolve_runner_endpoint(
    compose: DockerCompose,
    runner_service: str,
    runner_port: int = RUNNER_PORT_DEFAULT,
) -> tuple[str, int] | None:
    """Resolve the host-side ``(host, port)`` for a compose stack's
    runner service. Returns ``None`` if the service does not exist or
    the port is not exposed — callers surface that as a typed error
    with their own context (trial id, run id, whatever)."""
    host, port = resolve_host_port(compose, runner_service, runner_port)
    if host is None or port is None:
        return None
    return host, port


def resolve_env_endpoints(
    compose: DockerCompose,
    runner_host: str,
    runner_port: int,
    *,
    db_service: str = DB_SERVICE_DEFAULT,
    db_port: int = DB_SERVICE_PORT_DEFAULT,
    rag_service: str | None = None,
    rag_port: int | None = None,
) -> EnvEndpoints:
    """Resolve the :class:`EnvEndpoints` triple (runner_url, db_url,
    rag_url) from a running compose stack.

    ``runner_url`` is required — the caller already resolved the runner
    endpoint before invoking this function and passes the host/port in.

    ``db_url`` and ``rag_url`` are **both best-effort**. When the task's
    compose file declares ``db-service`` on port 8000 (the well-known
    convention that matches the engine's built-in stack), the returned
    ``db_url`` points at the host-mapped HTTP endpoint. When the compose
    file omits ``db-service``, ``db_url`` stays ``None`` — the runner
    container reads ``DB_SERVICE_URL`` from its own environment (task
    compose files set it via the ``runner`` service's ``environment``
    block), and ``db_json.py`` tools fall back to the same env var when
    constructed without a URL, so a missing ``db_url`` is not a failure.
    """
    db_host, db_host_port = resolve_host_port(compose, db_service, db_port)
    db_url: str | None
    if db_host is None or db_host_port is None:
        db_url = None
    else:
        db_url = f"http://{db_host}:{db_host_port}"
    return EnvEndpoints(
        db_url=db_url,
        rag_url=resolve_rag_url(compose, rag_service=rag_service, rag_port=rag_port),
        runner_url=f"http://{runner_host}:{runner_port}",
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def shutdown_compose(compose: DockerCompose) -> None:
    """Best-effort ``docker compose down --volumes``. Never raises."""
    try:
        compose.stop(down=True)
    except Exception:  # noqa: BLE001 — best-effort teardown
        logger.exception("compose_materialisation: docker compose down failed")


def compose_container_to_snapshot(container: ComposeContainer) -> ContainerSnapshot:
    """Map a testcontainers ``ComposeContainer`` to a :class:`ContainerSnapshot`.

    Fields the display panel reads: container name, compose service, docker
    state, health (``None`` if the service declared no health probe), and
    the ``TargetPort → PublishedPort`` mapping for published ports.
    """
    ports: dict[int, int] = {}
    for publisher in container.Publishers or []:
        if publisher.TargetPort is not None and publisher.PublishedPort is not None:
            ports[publisher.TargetPort] = publisher.PublishedPort
    return ContainerSnapshot(
        name=container.Name or "",
        service=container.Service or "",
        state=container.State or "unknown",
        health=container.Health if container.Health else None,
        ports=ports,
    )


def cleanup_partial_materialisation(compose: DockerCompose | None, temp_dir: Path) -> None:
    """Best-effort teardown after a partial-materialisation failure.

    Called from every ``except`` block during materialisation, before
    a typed error is re-raised. Handles both the early-failure case
    (``compose is None`` — the DockerCompose was never constructed) and
    the late-failure case (containers up but endpoint resolution
    failed).
    """
    if compose is not None:
        shutdown_compose(compose)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Per-service log capture on failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogCaptureConfig:
    """Per-run policy for capturing per-service compose logs on trial failure.

    ``output_root`` is the run's ``output_dir``; capture writes under
    ``output_root/trials/<task>/<idx>/services/``. ``tail`` is the
    ``docker compose logs --tail`` bound. ``on_success`` is the debug escape
    hatch that captures logs for successful trials too.
    """

    output_root: Path
    tail: int
    on_success: bool


def capture_compose_service_logs(
    compose: DockerCompose | None,
    service_names: Iterable[str],
    dest_dir: Path,
    tail: int,
) -> dict[str, int]:
    """Write ``docker compose logs`` output per service to ``dest_dir``.

    Returns ``{service_name: bytes_written}`` for the services that produced
    output. Best-effort diagnostics captured *because* a trial already failed:
    a per-service fetch error is ``logger.debug``-logged and that service is
    omitted from the map — this function never raises.

    The compose CLI is driven directly (testcontainers' ``get_logs`` has no
    tail bound): the base command comes from ``compose.compose_command_property``
    (``docker compose -f <file>``), and the subprocess runs with
    ``cwd=str(compose.context)`` so Docker Compose derives the project name from
    the context-dir basename — the same basename the per-trial temp dir encodes.
    A wrong cwd silently targets no project and returns empty logs.

    ``compose is None`` (nothing materialised) or an empty ``service_names``
    returns ``{}`` without creating ``dest_dir``. ``dest_dir`` is created
    (parents included) only when there is at least one service to attempt.
    """
    if compose is None:
        return {}
    names = list(service_names)
    if not names:
        return {}

    dest_dir.mkdir(parents=True, exist_ok=True)
    base_command = list(compose.compose_command_property)
    context = str(compose.context)
    captured: dict[str, int] = {}
    for name in names:
        output = _fetch_service_logs(base_command, context, name, tail)
        if not output:
            continue
        (dest_dir / f"{name}.log").write_bytes(output)
        captured[name] = len(output)
    return captured


def _fetch_service_logs(
    base_command: list[str], context: str, service: str, tail: int
) -> bytes | None:
    """Run ``docker compose logs`` for one service, returning its raw bytes.

    ``None`` on any subprocess failure (logged at debug) — the caller omits the
    service. Empty output is returned as-is and the caller skips it.
    """
    command = [*base_command, "logs", "--no-color", "--no-log-prefix", "--tail", str(tail), service]
    try:
        result = subprocess.run(command, cwd=context, capture_output=True, check=True)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — best-effort diagnostics; subprocess raises varied types
        logger.debug("compose_materialisation: logs fetch for service %r failed: %s", service, exc)
        return None
    return result.stdout


def trial_services_dir(output_root: Path, trial_id: str) -> Path:
    """Return the per-trial ``services/`` capture dir for ``trial_id``.

    ``trial_id`` is the canonical ``"{task_id}:{trial_index}"`` id; the tail
    ``:index`` becomes the trial subdir, matching the conductor's
    ``output_root/trials/<task_id>/<index>/`` bundle layout.
    """
    task_id, trial_index = trial_id.rsplit(":", 1)
    return output_root / "trials" / task_id / trial_index / "services"


def run_services_dir(output_root: Path) -> Path:
    """Return the run-level ``services/`` capture dir under ``output_root``.

    The run-scoped counterpart to :func:`trial_services_dir`: where a
    shared-stack materialise failure writes the run-wide stack's per-service
    logs, one level up from the per-trial bundles."""
    return output_root / "services"


def write_capture_manifest(
    dest_dir: Path,
    tail: int,
    captured: Mapping[str, int],
    capture_reason: str = "provision_error",
) -> None:
    """Write ``dest_dir/_capture.yaml`` recording captured per-service byte counts.

    The durable record for the provision-failure path, where no ``metrics.yaml``
    exists to amend. Shape:
    ``{"tail": int, "capture_reason": str, "services": {"<name>": {"bytes": int}}}``.
    ``dest_dir`` must already exist (the ``.log`` writer creates it).
    """
    manifest = {
        "tail": tail,
        "capture_reason": capture_reason,
        "services": {name: {"bytes": count} for name, count in captured.items()},
    }
    with (dest_dir / "_capture.yaml").open("w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False)
