"""Docker-compose :class:`ComposeMaterialiser` — the built-in implementation.

Extracts the materialise / teardown sequence shared by the compose-mode
runtime backends into a stateless adapter that satisfies
:class:`~tolokaforge.core.composition_runtime.ComposeMaterialiser`
structurally. Each :meth:`materialise` call:

1. Creates an isolated project temp dir (compose auto-derives its
   project name from the basename — see
   :func:`~tolokaforge.core.compose_materialisation.make_project_temp_dir`
   and ADR-0044 § 5 INV-10).
2. Copies the compose context, optionally writes the per-trial
   ``.env``, applies the run's :class:`NetworkPolicy`, injects the
   engine's credential payload into the runner service, and mounts the
   docker socket into the runner when the run needs compose-variant
   tools.
3. Constructs a ``testcontainers.compose.DockerCompose`` via the
   injectable :attr:`DockerComposeMaterialiser.docker_compose_factory`
   seam (defaults to the real class; tests substitute a stub) and
   drives ``.start()``.
4. Attaches one :class:`~tolokaforge.docker.logging.LogRouter` per
   compose container so the display gets a component row per service.

A failure at any point captures per-service logs (when
``ctx.log_capture`` is set), tears down the partial stack, removes the
temp dir, and re-raises :class:`ProvisionError` with ``stage="provision"``.
:meth:`teardown` reverses the sequence — routers stop before compose
comes down so the streaming threads exit before their log streams are
severed — and is idempotent.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from testcontainers.compose import DockerCompose

from tolokaforge.core.compose_materialisation import (
    apply_network_policy_to_compose_file,
    capture_compose_service_logs,
    cleanup_partial_materialisation,
    compose_container_to_snapshot,
    copy_compose_context,
    inject_runner_credentials,
    make_project_temp_dir,
    mount_docker_socket_into_runner,
    resolve_host_port,
    shutdown_compose,
    write_capture_manifest,
    write_compose_env_file,
)
from tolokaforge.core.composition_runtime import (
    MaterialiseContext,
    StackHandle,
)
from tolokaforge.core.run_display_events import (
    ComponentKind,
    ContainerSnapshot,
    build_component_id,
)
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.docker.logging import LogRouter
from tolokaforge.runner.models import StackDecl, StackScope

logger = logging.getLogger(__name__)


_SCOPE_COMPONENT_KIND: dict[StackScope, ComponentKind] = {
    "run": "docker.service",
    "task": "container",
    "trial": "container",
}
"""Which :data:`ComponentKind` a scope's log-router rows use.

The engine-owned run-scope stack publishes ``docker.service`` rows (the
display groups them under the engine namespace); task- and trial-scope
stacks publish per-container rows keyed to the trial's namespace. The
mapping keeps the materialiser stateless — the composer supplies the
namespace via :attr:`MaterialiseContext.component_id_prefix`, the scope
picks the kind.
"""


@dataclass(frozen=True)
class _DockerComposeStackHandle:
    """Live handle for one docker-compose-backed stack.

    Satisfies :class:`~tolokaforge.core.composition_runtime.StackHandle`
    structurally via the three metadata attributes; the compose object,
    temp dir, routers, and service-name snapshot are backend-private and
    consumed exclusively by :class:`DockerComposeMaterialiser`.
    """

    stack_id: str
    stack_scope: StackScope
    runner_service: str | None
    compose: DockerCompose
    temp_dir: Path
    log_routers: tuple[LogRouter, ...]
    service_names: tuple[str, ...]


@dataclass
class DockerComposeMaterialiser:
    """Built-in :class:`ComposeMaterialiser` — testcontainers-driven docker-compose.

    Stateless across calls: every handle carries its own compose object,
    temp dir, and router set. Injection seam:
    :attr:`docker_compose_factory` defaults to the real
    ``testcontainers.compose.DockerCompose`` and lets tests substitute
    an in-process stub without monkeypatching the module symbol.
    """

    name: str = "docker_compose"
    docker_compose_factory: Callable[..., Any] = field(default=DockerCompose)

    def materialise(self, decl: StackDecl, ctx: MaterialiseContext) -> StackHandle:
        temp_dir = make_project_temp_dir(ctx.scope_key, ctx.stack_id)
        compose_file = temp_dir / decl.compose_file.name
        compose: DockerCompose | None = None
        try:
            copy_compose_context(decl.compose_file, temp_dir)
            if ctx.write_compose_env is not None:
                write_compose_env_file(
                    temp_dir,
                    trial_id=ctx.write_compose_env.trial_id,
                    stack_inputs=ctx.write_compose_env.stack_inputs,
                )
            apply_network_policy_to_compose_file(
                compose_file,
                ctx.network_policy,
                decl.runner_service or "",
                list(ctx.limited_internet_allowlist),
                restricted_services=ctx.restricted_services,
            )
            if decl.runner_service is not None:
                inject_runner_credentials(compose_file, decl.runner_service)
                if ctx.mount_docker_socket:
                    mount_docker_socket_into_runner(compose_file, decl.runner_service)
            compose = self.docker_compose_factory(
                context=str(temp_dir),
                compose_file_name=decl.compose_file.name,
                pull=False,
                build=False,
                wait=True,
            )
            compose.start()
        except Exception as exc:  # noqa: BLE001 — surface as typed ProvisionError
            self._capture_materialise_failure(decl, ctx, compose)
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=ctx.scope_key,
                stage="provision",
                reason=(
                    f"docker compose up failed for stack {decl.stack_id!r} "
                    f"(scope={decl.stack_scope}): {exc}"
                ),
            ) from exc

        service_names = _load_service_names(decl.compose_file)
        log_routers = self._attach_log_routers(decl, ctx, compose)
        return _DockerComposeStackHandle(
            stack_id=decl.stack_id,
            stack_scope=decl.stack_scope,
            runner_service=decl.runner_service,
            compose=compose,
            temp_dir=temp_dir,
            log_routers=log_routers,
            service_names=service_names,
        )

    def resolve_endpoint(
        self, handle: StackHandle, service: str, container_port: int
    ) -> tuple[str, int] | None:
        typed = _cast_handle(handle)
        host, port = resolve_host_port(typed.compose, service, container_port)
        if host is None or port is None:
            return None
        return host, port

    def get_containers(self, handle: StackHandle) -> list[ContainerSnapshot]:
        typed = _cast_handle(handle)
        try:
            containers = typed.compose.get_containers()
        except Exception:  # noqa: BLE001 — display must never raise past orchestrator
            logger.exception(
                "DockerComposeMaterialiser.get_containers: docker ps failed for stack %r",
                typed.stack_id,
            )
            return []
        return [compose_container_to_snapshot(c) for c in containers]

    def capture_logs(
        self,
        handle: StackHandle,
        services: tuple[str, ...],
        dest_dir: Path,
        tail: int,
    ) -> dict[str, int]:
        typed = _cast_handle(handle)
        return capture_compose_service_logs(typed.compose, services, dest_dir, tail)

    def teardown(self, handle: StackHandle) -> None:
        typed = _cast_handle(handle)
        for router in typed.log_routers:
            try:
                router.stop()
            except Exception:  # noqa: BLE001 — teardown must never mask compose cleanup
                logger.exception(
                    "DockerComposeMaterialiser.teardown: log router stop failed for "
                    "container %r (stack %s)",
                    router.container_name,
                    typed.stack_id,
                )
        shutdown_compose(typed.compose)
        shutil.rmtree(typed.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Failure-time diagnostics
    # ------------------------------------------------------------------

    def _capture_materialise_failure(
        self,
        decl: StackDecl,
        ctx: MaterialiseContext,
        compose: DockerCompose | None,
    ) -> None:
        """Write per-service compose logs to ``ctx.log_capture.dest_dir``.

        No-op when capture is disabled or nothing materialised. Wrapped
        in a fail-safe boundary so any capture error never masks the
        :class:`ProvisionError` the caller is about to raise.
        """
        if ctx.log_capture is None or compose is None:
            return
        try:
            service_names = _load_service_names(decl.compose_file)
            captured = capture_compose_service_logs(
                compose,
                service_names,
                ctx.log_capture.dest_dir,
                ctx.log_capture.tail,
            )
            if captured:
                write_capture_manifest(
                    ctx.log_capture.dest_dir,
                    ctx.log_capture.tail,
                    captured,
                    capture_reason="materialise_error",
                )
        except Exception:  # noqa: BLE001 — fail-safe: must never mask the ProvisionError
            logger.exception(
                "DockerComposeMaterialiser: materialise-failure log capture failed for stack %r",
                decl.stack_id,
            )

    def _attach_log_routers(
        self,
        decl: StackDecl,
        ctx: MaterialiseContext,
        compose: DockerCompose,
    ) -> tuple[LogRouter, ...]:
        """One :class:`LogRouter` per compose container, log-and-continue on error.

        Component ids match today's inline flows: run-scope stacks use
        ``docker.service`` (engine-owned display row), trial- and
        task-scope stacks use ``container`` (per-trial row). The
        namespace comes from
        :attr:`MaterialiseContext.component_id_prefix`.
        """
        routers: list[LogRouter] = []
        kind = _SCOPE_COMPONENT_KIND[decl.stack_scope]
        try:
            containers = compose.get_containers()
        except Exception:  # noqa: BLE001 — router attach must not abort provisioning
            logger.exception(
                "DockerComposeMaterialiser: get_containers failed for stack %r; "
                "no log routers attached",
                decl.stack_id,
            )
            return ()
        for container in containers:
            if not container.ID:
                continue
            service = container.Service or "unknown"
            try:
                router = LogRouter(
                    container_name=container.Name or service,
                    container_id=container.ID,
                    component_id=build_component_id(ctx.component_id_prefix, kind, service),
                )
                router.start()
            except Exception:  # noqa: BLE001 — router failure must not abort provisioning
                logger.exception(
                    "DockerComposeMaterialiser: failed to attach log router for "
                    "container %r (service=%r, stack=%r)",
                    container.Name,
                    service,
                    decl.stack_id,
                )
                continue
            routers.append(router)
        return tuple(routers)


def _cast_handle(handle: StackHandle) -> _DockerComposeStackHandle:
    """Narrow a :class:`StackHandle` to this materialiser's private type.

    Foreign handles raise :class:`TypeError` naming both families — a
    materialiser must refuse a handle another family produced rather
    than fall through the docker-compose attribute reads.
    """
    if not isinstance(handle, _DockerComposeStackHandle):
        raise TypeError(
            f"DockerComposeMaterialiser expected a _DockerComposeStackHandle; "
            f"got {type(handle).__name__}."
        )
    return handle


def _load_service_names(compose_file: Path) -> tuple[str, ...]:
    """Read the compose file's declared service names.

    Snapshot at materialise time so the failure-log-capture path and the
    handle both see the same set even if the file on disk is later
    rewritten by an unrelated caller.
    """
    with compose_file.open() as f:
        doc = yaml.safe_load(f)
    services = (doc or {}).get("services") or {}
    return tuple(services)
