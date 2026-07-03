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

Endpoint resolution uses conventions (defaults + customisation later —
see the follow-up ticket): ``runner_service`` from the manifest at port
50051 → ``runner_url``; a compose service named ``db`` at port 5432 →
``db_url``; a compose service named ``rag`` (or ``rag-service``) at its
declared port → ``rag_url``. Task packs that need to override the
service names or ports will get manifest fields in a follow-up PR.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testcontainers.compose import DockerCompose

from tolokaforge.core.compose_materialisation import (
    RUNNER_PORT_DEFAULT,
    cleanup_partial_materialisation,
    copy_compose_context,
    make_project_temp_dir,
    resolve_env_endpoints,
    resolve_runner_endpoint,
    shutdown_compose,
)
from tolokaforge.core.runtime import EnvHandle, IsolationMode, ProvisionError
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient, RunnerClient
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints

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
    orchestrator's compatibility check so tasks that declare
    ``environment_manifest.isolation: per_trial`` are satisfied."""

    connect_timeout: float = 30.0
    """Seconds to wait for a per-trial runner's gRPC server to become
    healthy after ``.start()`` returns. The compose ``--wait`` flag
    handles the healthcheck-level readiness for containers; this
    covers the extra time for the gRPC server inside the runner to
    bind its port."""

    connect_retry_interval: float = 1.0
    """Poll interval for the runner-side health check during
    :meth:`_connect_runner_client`."""

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
        """Close every connected per-trial runner client. Idempotent."""
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

        temp_dir = make_project_temp_dir(spec.trial_id)
        compose: DockerCompose | None = None
        try:
            copy_compose_context(manifest.compose_file, temp_dir)
            compose = DockerCompose(
                context=str(temp_dir),
                compose_file_name=manifest.compose_file.name,
                pull=False,
                build=False,
                wait=True,
            )
            compose.start()
        except Exception as exc:  # noqa: BLE001 — surface as typed ProvisionError
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=f"docker compose up failed: {exc}",
            ) from exc

        runner_service = manifest.runner_service
        runner_port = RUNNER_PORT_DEFAULT
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

        endpoints = resolve_env_endpoints(compose, runner_host, runner_host_port)
        if endpoints is None:
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=(
                    "PerTrialRuntimeBackend requires a compose service named 'db' "
                    "exposing port 5432; compose file does not declare one. "
                    "Endpoint-resolution customisation is a follow-up ticket."
                ),
            )

        # Client is constructed but not yet connected. Connect is deferred
        # to first per-trial RPC use — see :attr:`_connected_trials`.
        client = GrpcRunnerClient(runner_address=f"{runner_host}:{runner_host_port}")
        self._clients[spec.trial_id] = client
        return _LocalEnvHandle(
            trial_id=spec.trial_id,
            compose=compose,
            runner_service=runner_service,
            runner_port=runner_port,
            temp_dir=temp_dir,
            endpoints=endpoints,
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
        shutdown_compose(handle.compose)
        shutil.rmtree(handle.temp_dir, ignore_errors=True)

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

    # ---- Internal helpers ----

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
