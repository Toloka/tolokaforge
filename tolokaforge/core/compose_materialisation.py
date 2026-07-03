"""Compose-materialisation primitives shared by runtime backends.

The concrete `RuntimeBackend` implementations that materialise a task's
``environment_manifest`` via testcontainers-python (``PerTrialRuntimeBackend``
today, ``SharedStackRuntimeBackend`` under Phase 4) share the same lifecycle
primitives: copy the compose context into an isolated project directory,
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

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from testcontainers.compose import DockerCompose

from tolokaforge.core.trial import EnvEndpoints

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint-resolution conventions
# ---------------------------------------------------------------------------


RUNNER_PORT_DEFAULT = 50051
"""gRPC port the tolokaforge runner container listens on. Matches
``GrpcRunnerClient``'s default ``runner:50051``. Task-pack authors that
need to override this will get a manifest field in a follow-up PR."""

DB_SERVICE_DEFAULT = "db"
"""Compose service name convention for the raw database backing the
runner's state (the ``db-service`` HTTP wrapper sits on top of this)."""

DB_PORT_DEFAULT = 5432
"""Postgres default port for the ``db`` service."""

RAG_SERVICE_CANDIDATES = ("rag", "rag-service")
"""Compose service names checked when resolving the RAG endpoint.
First match wins; ``None`` returned if no match is exposed."""


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
    if host == "0.0.0.0":  # noqa: S104 — testcontainers returns this on macOS/Linux
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
    db_port: int = DB_PORT_DEFAULT,
) -> EnvEndpoints | None:
    """Resolve the full :class:`EnvEndpoints` triple (runner_url, db_url,
    rag_url) from a running compose stack. Returns ``None`` if the
    required db service does not exist or its port is not exposed —
    callers surface that as a typed error with their own context.

    ``rag_url`` is best-effort: absent means ``None`` on the endpoints,
    not a resolution failure.
    """
    db_host, db_host_port = resolve_host_port(compose, db_service, db_port)
    if db_host is None or db_host_port is None:
        return None
    return EnvEndpoints(
        db_url=f"http://{db_host}:{db_host_port}",
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
