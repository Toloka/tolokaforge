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
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from testcontainers.compose import DockerCompose
from testcontainers.compose.compose import ComposeContainer

from tolokaforge.core.run_display_events import ContainerSnapshot
from tolokaforge.core.trial import EnvEndpoints, NetworkPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint-resolution conventions
# ---------------------------------------------------------------------------


RUNNER_PORT_DEFAULT = 50051
"""gRPC port the tolokaforge runner container listens on. Matches
``GrpcRunnerClient``'s default ``runner:50051``. Task-pack authors that
need to override this will get a manifest field in a follow-up PR."""

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


NETPOLICY_INTERNAL_NETWORK = "tolokaforge_netpolicy_internal"
"""Injected ``internal: true`` network every task service joins under
``no_internet``. No public egress; inter-service DNS stays intact because
every service shares it. Compose prefixes it with the per-run/per-trial
project name, so the fully-qualified network is unique on the daemon and
cannot collide with a task-declared network of the same base name."""

NETPOLICY_EDGE_NETWORK = "tolokaforge_netpolicy_edge"
"""Injected non-internal network the runner service *additionally* joins
under ``no_internet``, so its published gRPC port stays host-reachable and
it retains egress for in-container LLM-as-judge grading."""


class NetworkPolicyError(ValueError):
    """A declared ``network_policy`` has no docker enforcement available."""


def verify_network_policy_supported(policy: NetworkPolicy) -> None:
    """Raise :class:`NetworkPolicyError` if ``policy`` cannot be enforced by
    the docker provisioner. ``limited_internet`` needs an egress-allowlist
    proxy sidecar (#323); refusing to run is the only honest option —
    silently granting full or no internet would under/over-enforce a
    declared security posture. ``no_internet`` and ``full_internet`` return
    cleanly. Reads only ``policy`` — no compose I/O — so callers can hoist it
    before any temp-dir creation or docker work."""
    if policy is NetworkPolicy.LIMITED_INTERNET:
        raise NetworkPolicyError(
            "network_policy 'limited_internet' is not enforceable yet: it needs an "
            "egress-allowlist proxy sidecar (#323). Declare 'no_internet' or "
            "'full_internet' explicitly."
        )


def enforce_network_policy(
    compose_doc: dict[str, Any],
    policy: NetworkPolicy,
    runner_service: str,
) -> dict[str, Any]:
    """Return a compose doc rewritten to enforce ``policy``.

    ``full_internet`` returns ``compose_doc`` unchanged (same object).
    ``no_internet`` returns a deep copy in which (1) every service joins the
    injected :data:`NETPOLICY_INTERNAL_NETWORK` (``internal: true`` — no
    egress), any network the task already declared is forced ``internal:
    true`` too, and (2) ``runner_service`` additionally joins the non-internal
    :data:`NETPOLICY_EDGE_NETWORK`. ``limited_internet`` raises
    :class:`NetworkPolicyError` (belt-and-suspenders — the enforcement point
    is :func:`verify_network_policy_supported`; reaching here with it is a
    wiring bug and must fail loud rather than return an egress-capable doc).

    ``runner_service`` is guaranteed present in ``services`` by
    :class:`EnvironmentManifest` validation.
    """
    if policy is NetworkPolicy.FULL_INTERNET:
        return compose_doc
    if policy is NetworkPolicy.LIMITED_INTERNET:
        raise NetworkPolicyError(
            "enforce_network_policy reached with 'limited_internet'; this value has no "
            "docker enforcement (#323) and must be refused by verify_network_policy_supported "
            "before any compose rewrite."
        )

    doc = copy.deepcopy(compose_doc)
    networks: dict[str, Any] = doc.setdefault("networks", {})
    for name, config in networks.items():
        merged = dict(config) if isinstance(config, dict) else {}
        merged["internal"] = True
        networks[name] = merged
    networks[NETPOLICY_INTERNAL_NETWORK] = {"internal": True}
    networks[NETPOLICY_EDGE_NETWORK] = {}

    services: dict[str, Any] = doc["services"]
    for service_name, service in services.items():
        attachments = [NETPOLICY_INTERNAL_NETWORK]
        if service_name == runner_service:
            attachments.append(NETPOLICY_EDGE_NETWORK)
        service["networks"] = _merge_service_networks(service.get("networks"), attachments)
    return doc


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
    compose_file: Path, policy: NetworkPolicy, runner_service: str
) -> None:
    """Read ``compose_file``, apply :func:`enforce_network_policy`, and write
    the result back in place. ``full_internet`` leaves the file byte-identical
    (the transform is identity), so its comments and formatting survive."""
    with compose_file.open() as f:
        doc = yaml.safe_load(f)
    transformed = enforce_network_policy(doc, policy, runner_service)
    if transformed is doc:
        return
    with compose_file.open("w") as f:
        yaml.safe_dump(transformed, f, sort_keys=False)


# ---------------------------------------------------------------------------
# Filesystem + project layout
# ---------------------------------------------------------------------------


def make_project_temp_dir(prefix_slug: str) -> Path:
    """Create a temp directory whose basename embeds ``prefix_slug``.

    Docker Compose auto-generates a project name from the context
    directory basename; encoding a caller-supplied slug (a trial id for
    per-trial, a run id for shared) into that basename gives each
    materialisation a unique compose project.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in prefix_slug)
    return Path(tempfile.mkdtemp(prefix=f"tolokaforge-{safe}-"))


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
    """Extract the first published container-side port from a
    Testcontainers ``ComposeContainer``. Returns ``None`` when nothing
    is published (or the shape is not what we expect)."""
    ports = getattr(container, "Publishers", None) or []
    for entry in ports:
        target = getattr(entry, "TargetPort", None)
        if isinstance(target, int) and target > 0:
            return target
    return None


def resolve_rag_url(compose: DockerCompose) -> str | None:
    """Best-effort ``rag_url`` — resolve the first RAG service found in
    the compose stack. Returns ``None`` when no such service is
    declared or its port is not exposed. Lookup failures are treated
    as "no rag" but ``logger.debug``-logged for diagnosability."""
    for candidate in RAG_SERVICE_CANDIDATES:
        try:
            container = compose.get_container(service_name=candidate)
        except Exception as exc:  # noqa: BLE001 — service not declared
            logger.debug(
                "compose_materialisation: rag candidate %r not in compose: %s",
                candidate,
                exc,
            )
            continue
        published = first_published_port(container)
        if published is None:
            continue
        host, port = resolve_host_port(compose, candidate, published)
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
        rag_url=resolve_rag_url(compose),
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
