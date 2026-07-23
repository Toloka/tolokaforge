"""``PerTrialRuntimeBackend`` — per-trial docker-compose materialisation.

The concrete :class:`~tolokaforge.core.runtime.RuntimeBackend` for local,
laptop-class development and CI. Each trial materialises as an isolated
docker-compose project via ``testcontainers.compose.DockerCompose``:

* :meth:`PerTrialRuntimeBackend.provision` copies the compose file (and any
  adjacent bind-mount source files) into a per-trial temp directory so
  Docker Compose auto-generates a unique project name per trial, then
  starts the stack via ``.start()`` (which runs ``docker compose up -d
  --wait`` — healthcheck blocks are gated by ``depends_on: condition:
  service_healthy`` and ``--wait``, so :meth:`await_ready` is a no-op).
* :meth:`endpoints` resolves per-trial URLs via
  ``get_service_host_and_port`` on the started stack.
* :meth:`teardown` runs ``docker compose down --volumes`` (via
  ``.stop(down=True)``), closes the per-trial :class:`RunnerClient`, and
  removes the temp directory. Idempotent.

The per-trial RPC methods (``register_trial`` / ``execute_tool`` /
``grade_trial`` / ``get_state`` / ``reset_trial`` / ``cleanup_trial``,
ADR-0013) delegate to a :class:`RunnerClient` cached at provision time
and keyed by ``trial_id``. Contrast with :class:`SharedStackRuntimeBackend` which
carries a single client for the whole run.

Endpoint resolution delegates to :mod:`tolokaforge.core.compose_materialisation`;
see that module for the conventions and their defaults.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testcontainers.compose import DockerCompose

from tolokaforge.core.compose_materialisation import (
    DB_SERVICE_DEFAULT,
    DB_SERVICE_PORT_DEFAULT,
    LogCaptureConfig,
    apply_network_policy_to_compose_file,
    capture_compose_service_logs,
    cleanup_partial_materialisation,
    compose_container_to_snapshot,
    copy_compose_context,
    make_project_temp_dir,
    resolve_env_endpoints,
    resolve_runner_endpoint,
    shutdown_compose,
    trial_services_dir,
    write_capture_manifest,
)
from tolokaforge.core.models import SeedRef
from tolokaforge.core.run_display_events import ContainerSnapshot, build_component_id
from tolokaforge.core.runtime import EnvHandle, IsolationMode, ProvisionError
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient, RunnerClient
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints
from tolokaforge.docker.logging import LogRouter
from tolokaforge.runner.models import EnvironmentManifest

if TYPE_CHECKING:
    from tolokaforge.core.trial import TrialSpec
    from tolokaforge.tools.registry import ToolResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LocalEnvHandle:
    """Concrete handle type returned by :class:`PerTrialRuntimeBackend`.

    Satisfies the :class:`EnvHandle` Protocol structurally via
    ``trial_id``. Every other field is backend-private — callers use
    the handle as an opaque token.

    ``endpoints`` is resolved at provision time and snapshot here so
    :meth:`PerTrialRuntimeBackend.endpoints` is a pure read (no re-query,
    no side effects). If endpoint resolution fails, provision itself
    raises :class:`ProvisionError` before the handle is returned.
    """

    trial_id: str
    compose: DockerCompose
    runner_service: str
    runner_port: int
    temp_dir: Path
    endpoints: EnvEndpoints
    service_names: tuple[str, ...]
    """Compose stack's declared service names, snapshot at provision time.
    Read by :meth:`PerTrialRuntimeBackend.capture_service_logs` so the
    trial-body-failure path captures every service without re-parsing the
    (already torn-down) manifest."""
    log_routers: tuple[LogRouter, ...] = ()
    """Per-container ``LogRouter`` snapshot, one per compose container
    that exposed a docker ``ID`` at provision time. Empty tuple when
    every container failed to yield a router (compose ran but router
    construction raised for each container) or when the compose stack
    was empty. :meth:`PerTrialRuntimeBackend.teardown` stops every
    router BEFORE ``shutdown_compose`` so the streaming threads exit
    before the docker log streams are severed."""


@dataclass
class PerTrialRuntimeBackend:
    """Per-trial ``RuntimeBackend`` backed by ``testcontainers.compose.DockerCompose``.

    Each :meth:`provision` call materialises an isolated docker-compose
    project for one trial, resolvable to its own set of per-trial URLs.
    Concurrent trials get independent projects (unique project name per
    trial, unique host ports for exposed services).

    The backend maintains a per-trial :class:`RunnerClient` cache
    (:attr:`_clients`) populated at provision time and cleared at
    teardown. Every per-trial RPC method (``register_trial`` /
    ``execute_tool`` / ``grade_trial`` / ``get_state`` / ``reset_trial``
    / ``cleanup_trial``) looks up the client by ``trial_id`` and
    delegates. Callers that hit an RPC method before provisioning that
    trial's environment get a clear :class:`RuntimeError`.
    """

    isolation_mode: IsolationMode = IsolationMode.PER_TRIAL_STACK
    """Every trial gets its own compose project. Advertised to the
    orchestrator's compatibility check so tasks whose manifests require
    per-trial substrate materialisation are satisfied."""

    advertised_capabilities: frozenset[str] = frozenset(
        {
            "per_trial_stack",
            "reset_recipes:sql_dump",
            "reset_recipes:filesystem_dir",
            "reset_recipes:redis_dump",
            "reset_recipes:bare",
            "network_isolation:no_internet",
            "network_isolation:limited_internet",
        }
    )
    """Local-docker per-trial capability advertisement. Read by
    :func:`tolokaforge.core.backend_capabilities.check_admission`."""

    connect_timeout: float = 30.0
    """Seconds to wait for a per-trial runner's gRPC server to become
    healthy after ``.start()`` returns. The compose ``--wait`` flag
    handles the healthcheck-level readiness for containers; this
    covers the extra time for the gRPC server inside the runner to
    bind its port."""

    connect_retry_interval: float = 1.0
    """Poll interval for the runner-side health check during
    :meth:`_connect_runner_client`."""

    seeds: dict[str, SeedRef] = field(default_factory=dict)
    """Project-level seed registry — the ``name → SeedRef`` map read
    from ``project.assets.seeds``. Consumed by :meth:`_apply_reset_recipes`
    to resolve ``services.<name>.reset.seed`` references at reset time.
    Empty dict means no reset recipes will fire."""

    log_capture: LogCaptureConfig | None = None
    """Per-service log-capture policy. ``None`` disables capture (tests, or
    construction with no run output root). When set, provision-stage failures
    and :meth:`capture_service_logs` write ``docker compose logs`` output under
    ``log_capture.output_root/trials/<task>/<idx>/services/`` before teardown."""

    _clients: dict[str, RunnerClient] = field(default_factory=dict)
    _connected_trials: set[str] = field(default_factory=set)
    """Trial ids whose runner client has passed ``connect()``. Connect
    is deferred until the first per-trial RPC call: the compose stack's
    ``--wait`` flag has already gated container readiness; connecting
    the gRPC channel at provision time inflates every trial's
    provisioning latency by the connect cost even when the trial never
    exercises the RPC surface. First RPC use triggers connect via
    :meth:`_client_for`."""

    # ---- Run-level lifecycle ----

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """No-op. No shared runner service exists — clients are constructed
        per-trial at :meth:`provision` time. The ``timeout`` and
        ``retry_interval`` on this method exist to satisfy the
        :class:`RuntimeBackend` Protocol signature; per-trial connect
        knobs live on the class constructor.
        """
        # Signature-only presence; nothing to do.
        del timeout, retry_interval

    def close(self) -> None:
        """Close every connected per-trial runner client. Idempotent.

        Per-container ``LogRouter`` teardown lives on :meth:`teardown`,
        not here: the orchestrator's per-trial ``finally`` calls
        ``teardown`` on every provisioned handle, so no handle-level
        router bookkeeping is needed at the class level.
        """
        for trial_id in list(self._connected_trials):
            client = self._clients.get(trial_id)
            if client is None:
                continue
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.exception("PerTrialRuntimeBackend.close: client shutdown failed")
        self._clients.clear()
        self._connected_trials.clear()

    def health_check(self) -> bool:
        """Healthy when every connected per-trial client passes its own
        health check. Provisioned-but-idle trials (client cached, not
        yet connected via first RPC use) do not fail the check —
        that would defeat lazy-connect."""
        return all(
            self._clients[trial_id].health_check()
            for trial_id in self._connected_trials
            if trial_id in self._clients
        )

    # ---- Per-trial provisioning (ADR-0010) ----

    def provision(self, spec: TrialSpec) -> EnvHandle:
        manifest = spec.task.environment_manifest
        if manifest is None:
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=(
                    "PerTrialRuntimeBackend requires TaskDescription.environment_manifest; "
                    "task did not declare one. Use SharedStackRuntimeBackend for shared-stack tasks."
                ),
            )

        service_names = tuple(manifest.load_compose()["services"])
        temp_dir = make_project_temp_dir(spec.trial_id)
        compose: DockerCompose | None = None
        try:
            copy_compose_context(manifest.compose_file, temp_dir)
            apply_network_policy_to_compose_file(
                temp_dir / manifest.compose_file.name,
                manifest.network_policy,
                manifest.runner_service,
                manifest.limited_internet_allowlist,
                restricted_services=manifest.restricted_services,
            )
            compose = DockerCompose(
                context=str(temp_dir),
                compose_file_name=manifest.compose_file.name,
                pull=False,
                build=False,
                wait=True,
            )
            compose.start()
        except Exception as exc:  # noqa: BLE001 — surface as typed ProvisionError
            self._capture_provision_failure_logs(spec.trial_id, service_names, compose)
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=f"docker compose up failed: {exc}",
            ) from exc

        # Reset recipes run against the started stack; a recipe failure is
        # distinct from a compose-up failure (the stack came up fine), so
        # _apply_reset_recipes owns the reset_recipe stage. Teardown is the
        # same either way — the partially materialised stack must come down.
        # The catch is broad on purpose: a typed ProvisionError and a
        # programming error (e.g. an unregistered SeedKind surfacing as
        # KeyError from dispatch) both require the stack torn down. The
        # non-ProvisionError re-raise still propagates fail-fast.
        try:
            self._apply_reset_recipes(manifest, compose, spec)
        except Exception:
            self._capture_provision_failure_logs(spec.trial_id, service_names, compose)
            cleanup_partial_materialisation(compose, temp_dir)
            raise

        runner_service = manifest.runner_service
        runner_port = manifest.runner_port
        runner_endpoint = resolve_runner_endpoint(compose, runner_service, runner_port)
        if runner_endpoint is None:
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=(
                    f"runner_service {runner_service!r} does not expose port "
                    f"{runner_port} in the compose stack"
                ),
            )
        runner_host, runner_host_port = runner_endpoint

        # resolve_env_endpoints is best-effort for db_url + rag_url — a task
        # compose file that omits `db-service:8000` gets endpoints with
        # `db_url=None`. The runner-side DBServiceClient reads DB_SERVICE_URL
        # from its container env, and `db_json.py` tools fall back to the
        # same env var, so a missing db_url is not a provisioning failure.
        endpoints = resolve_env_endpoints(
            compose,
            runner_host,
            runner_host_port,
            db_service=manifest.db_service or DB_SERVICE_DEFAULT,
            db_port=manifest.db_port or DB_SERVICE_PORT_DEFAULT,
            rag_service=manifest.rag_service,
            rag_port=manifest.rag_port,
        )

        # Client is constructed but not yet connected. Connect is deferred
        # to first per-trial RPC use — see :attr:`_connected_trials`.
        client = GrpcRunnerClient(runner_address=f"{runner_host}:{runner_host_port}")
        self._clients[spec.trial_id] = client

        log_routers = self._attach_log_routers(spec.trial_id, compose)
        return _LocalEnvHandle(
            trial_id=spec.trial_id,
            compose=compose,
            runner_service=runner_service,
            runner_port=runner_port,
            temp_dir=temp_dir,
            endpoints=endpoints,
            service_names=service_names,
            log_routers=log_routers,
        )

    def await_ready(self, handle: EnvHandle) -> None:
        # ``.start(wait=True)`` in :meth:`provision` already blocks on
        # docker compose healthchecks. Kept explicit for Protocol shape;
        # future backends that don't gate readiness in provision slot in
        # without changing the surface.
        del handle

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        """Return the endpoints snapshot resolved at :meth:`provision`
        time. Pure read — no re-query against the compose stack, no
        side effects. Callers can invoke ``endpoints`` and ``teardown``
        in any order (or from a ``finally``) without double-cleanup
        concerns."""
        if not isinstance(handle, _LocalEnvHandle):
            raise TypeError(
                f"PerTrialRuntimeBackend.endpoints requires a _LocalEnvHandle; got {type(handle).__name__}"
            )
        return handle.endpoints

    def teardown(self, handle: EnvHandle) -> None:
        if not isinstance(handle, _LocalEnvHandle):
            # Best-effort — Protocol says teardown is idempotent, so
            # a foreign handle is treated as an already-torn-down one.
            return
        client = self._clients.pop(handle.trial_id, None)
        was_connected = handle.trial_id in self._connected_trials
        self._connected_trials.discard(handle.trial_id)
        if client is not None and was_connected:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception(
                    "PerTrialRuntimeBackend.teardown: runner client close failed for %s",
                    handle.trial_id,
                )
        for router in handle.log_routers:
            try:
                router.stop()
            except Exception:  # noqa: BLE001 — teardown must never mask compose cleanup
                logger.exception(
                    "PerTrialRuntimeBackend.teardown: log router stop failed for "
                    "container %r (trial %s)",
                    router.container_name,
                    handle.trial_id,
                )
        shutdown_compose(handle.compose)
        shutil.rmtree(handle.temp_dir, ignore_errors=True)

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        """Capture per-service logs for the trial-body diagnostics path.

        No-op ``{}`` when capture is disabled, the gate (``capture_worthy`` or
        the on-success policy) is not met, or ``handle`` is foreign. Otherwise
        writes ``docker compose logs`` output for the handle's snapshot
        services into the trial ``services/`` dir and returns the byte map.
        Writes only the ``.log`` files — the durable ``metrics.yaml``
        amendment on this path is the executor's responsibility. Never raises.
        """
        if self.log_capture is None:
            return {}
        if not (capture_worthy or self.log_capture.on_success):
            return {}
        if not isinstance(handle, _LocalEnvHandle):
            return {}
        dest_dir = trial_services_dir(self.log_capture.output_root, handle.trial_id)
        return capture_compose_service_logs(
            handle.compose, handle.service_names, dest_dir, self.log_capture.tail
        )

    def get_infrastructure_snapshot(self, handle: EnvHandle) -> list[ContainerSnapshot]:
        """Return the per-trial container snapshot from the compose stack.

        Reads ``handle.compose.get_containers()`` and maps each
        :class:`ComposeContainer` to a :class:`ContainerSnapshot`. Errors
        from the docker CLI are logged and swallowed — the display path
        must never raise past the orchestrator, and a missing infra
        snapshot degrades gracefully to "no infrastructure panel".
        """
        if not isinstance(handle, _LocalEnvHandle):
            return []
        try:
            containers = handle.compose.get_containers()
        except Exception:  # noqa: BLE001 — display must never raise past orchestrator
            logger.exception(
                "PerTrialRuntimeBackend.get_infrastructure_snapshot: docker ps failed for %s",
                handle.trial_id,
            )
            return []
        return [compose_container_to_snapshot(c) for c in containers]

    # ---- Per-trial RPC operations (ADR-0013) ----

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict[str, Any]:
        return self._client_for(trial_id).register_trial(
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
    ) -> ToolResult:
        return self._client_for(trial_id).execute_tool(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            executor=executor,
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._client_for(trial_id).grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._client_for(trial_id).get_state(
            trial_id=trial_id,
            include_unstable=include_unstable,
            tables=tables,
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        return self._client_for(trial_id).reset_trial(
            trial_id=trial_id, execute_init_actions=execute_init_actions
        )

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        client = self._clients.get(trial_id)
        if client is None:
            # Retry-cleanup path called before provision (or after teardown).
            # Contract says cleanup is idempotent; report success.
            return {"success": True, "error": None}
        return client.cleanup_trial(trial_id)

    # ---- Reset seam ----

    def _apply_reset_recipes(
        self,
        manifest: EnvironmentManifest,
        compose: DockerCompose,
        spec: TrialSpec,
    ) -> None:
        """Dispatch the reset recipe for every service labelled ``reset``.

        Runs once per trial, right after ``docker compose up`` returns.
        The stack is already fresh; the recipe seeds it deterministically
        before the trial body starts.
        """
        from tolokaforge.runtime.reset_recipes import dispatch

        for service_name, service_spec in manifest.services.items():
            if service_spec.isolation != "reset":
                continue
            if service_spec.reset is None:
                raise ProvisionError(
                    trial_id=spec.trial_id,
                    stage="reset_recipe",
                    reason=(
                        f"service {service_name!r} labelled 'reset' has no "
                        "'reset.seed' pointer — schema validation should have "
                        "rejected the manifest earlier."
                    ),
                )
            seed_name = service_spec.reset.seed
            seed = self.seeds.get(seed_name)
            if seed is None:
                raise ProvisionError(
                    trial_id=spec.trial_id,
                    stage="reset_recipe",
                    reason=(
                        f"service {service_name!r} names seed {seed_name!r} but "
                        f"the backend has no such seed in its registry "
                        f"(available: {sorted(self.seeds)!r})."
                    ),
                )
            try:
                dispatch(seed, service_name, compose)
            except RuntimeError as exc:
                raise ProvisionError(
                    trial_id=spec.trial_id,
                    stage="reset_recipe",
                    reason=(
                        f"reset recipe for service {service_name!r} "
                        f"(seed {seed_name!r}, kind {seed.kind!r}) failed: {exc}"
                    ),
                ) from exc

    # ---- Internal helpers ----

    def _attach_log_routers(self, trial_id: str, compose: DockerCompose) -> tuple[LogRouter, ...]:
        """Build and start a :class:`LogRouter` per compose container.

        Called after ``compose.start()``, reset recipes, and endpoint
        resolution succeed — the stack is already up and every provision
        failure path above this point has already run its cleanup, so a
        router-only failure here must NOT abort provisioning. Each router
        is constructed and started inside its own ``try/except`` so a
        single failure logs and is skipped while sibling routers still
        attach.

        Component id mirrors what :func:`_container_to_component`
        publishes for the same container so the status row and the log
        tail share one component id.
        """
        routers: list[LogRouter] = []
        for container in compose.get_containers():
            if not container.ID:
                continue
            try:
                router = LogRouter(
                    container_name=container.Name or container.Service or "unknown",
                    container_id=container.ID,
                    component_id=build_component_id(
                        f"trial/{trial_id}",
                        "container",
                        container.Service or "unknown",
                    ),
                )
                router.start()
            except Exception:  # noqa: BLE001 — router failure must not abort provisioning
                logger.exception(
                    "PerTrialRuntimeBackend: failed to attach log router for "
                    "container %r (service=%r, trial=%s)",
                    container.Name,
                    container.Service,
                    trial_id,
                )
                continue
            routers.append(router)
        return tuple(routers)

    def _capture_provision_failure_logs(
        self,
        trial_id: str,
        service_names: tuple[str, ...],
        compose: DockerCompose | None,
    ) -> None:
        """Best-effort per-service log capture on a provision-stage failure,
        before the partial stack is torn down.

        No-op when capture is disabled or nothing materialised (``compose is
        None``). Writes the ``.log`` files plus a ``services/_capture.yaml``
        durable record — the provision path never reaches
        ``conductor.run``, so no ``metrics.yaml`` exists to amend. Never
        raises (the underlying helpers swallow their own errors)."""
        if self.log_capture is None:
            return
        dest_dir = trial_services_dir(self.log_capture.output_root, trial_id)
        captured = capture_compose_service_logs(
            compose, service_names, dest_dir, self.log_capture.tail
        )
        if captured:
            write_capture_manifest(dest_dir, self.log_capture.tail, captured)

    def _client_for(self, trial_id: str) -> RunnerClient:
        client = self._clients.get(trial_id)
        if client is None:
            raise RuntimeError(
                f"PerTrialRuntimeBackend has no runner client for trial_id={trial_id!r}. "
                "provision() must be called before any per-trial RPC method."
            )
        if trial_id not in self._connected_trials:
            client.connect(
                timeout=self.connect_timeout,
                retry_interval=self.connect_retry_interval,
            )
            self._connected_trials.add(trial_id)
        return client
