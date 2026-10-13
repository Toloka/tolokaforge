"""``RemoteRuntimeBackend`` — dispatches each trial to a leased remote runner node.

:meth:`provision` leases one node from the :class:`NodeRegistry` and the trial's
per-trial RPCs travel to that node's runner over a :class:`GrpcRunnerClient`.
The backend holds no run-wide runner: every trial owns its lease, its client,
and its connect, so concurrency is bounded by the number of registered nodes.

The backend exposes no ``runner_address`` attribute. A run-wide address would
name no single node, so the trial grader is wired through the in-process shim
that routes :meth:`grade_trial` to the trial's leased client.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.node_registry import NodeRegistry, RunnerLease
from tolokaforge.core.runtime import EnvHandle, IsolationMode
from tolokaforge.core.shared_stack_runtime import (
    GrpcRunnerClient,
    RunnerClient,
    _normalise_runner_url,
)
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.tools.registry import ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.grading.bundle import GradeBundleManifest
    from tolokaforge.core.models.run_config import ModelConfig
    from tolokaforge.core.models.trajectory import Trajectory
    from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext
    from tolokaforge.core.run_display_events import ContainerSnapshot
    from tolokaforge.core.trial import TrialSpec
    from tolokaforge.runner.models import TaskDescription

logger = logging.getLogger(__name__)

RunnerClientFactory = Callable[[str, int], RunnerClient]
"""Builds the runner client for a leased node: ``(runner_address, max_message_mb)``."""


def _grpc_runner_client(runner_address: str, max_message_mb: int) -> RunnerClient:
    return GrpcRunnerClient(runner_address, max_message_mb=max_message_mb)


@dataclass(frozen=True)
class _RemoteHandle:
    """Handle returned by :meth:`RemoteRuntimeBackend.provision`."""

    trial_id: str


@dataclass
class RemoteRuntimeBackend:
    """Runtime backend that leases a runner node per trial from a :class:`NodeRegistry`.

    Fields:
        registry: Source of runner nodes; :meth:`provision` leases one per trial.
        grpc_max_message_mb: Max gRPC message size, in MB, for each node client.
        connect_timeout_s: Budget for a trial's client to reach its node on the first RPC.
        retry_interval_s: Pause between connect attempts within that budget.
        runner_client_factory: Builds the client for a leased address; tests inject
            a recording factory in place of the gRPC client.
    """

    # The only consumer branches on PER_TRIAL_STACK (no local per-trial stack to
    # check against dispatchers); every other mode takes the shared-stack path.
    isolation_mode: ClassVar[IsolationMode] = IsolationMode.SHARED_STACK
    advertised_capabilities: ClassVar[frozenset[str]] = frozenset()

    registry: NodeRegistry
    grpc_max_message_mb: int = DEFAULT_MAX_MESSAGE_MB
    connect_timeout_s: float = 30.0
    retry_interval_s: float = 1.0
    runner_client_factory: RunnerClientFactory = _grpc_runner_client
    _trials: dict[str, tuple[RunnerLease, RunnerClient]] = field(default_factory=dict, init=False)
    _connected_trials: set[str] = field(default_factory=set, init=False)

    # ---- Run-level lifecycle ----

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """No-op: each trial's client connects on its first RPC."""
        del timeout, retry_interval

    def close(self) -> None:
        for trial_id in list(self._trials):
            self._drop_trial(trial_id)

    def health_check(self) -> bool:
        """Healthy when the registry has nodes and every connected trial's runner answers."""
        if not self.registry.has_nodes():
            return False
        for trial_id in list(self._connected_trials):
            held = self._trials.get(trial_id)
            if held is not None and not held[1].health_check():
                return False
        return True

    # ---- Per-trial provisioning ----

    def provision(self, spec: TrialSpec) -> EnvHandle:
        lease = self.registry.lease(spec.trial_id)
        try:
            client = self.runner_client_factory(lease.runner_address, self.grpc_max_message_mb)
        except BaseException:
            self.registry.release(spec.trial_id)
            raise
        self._trials[spec.trial_id] = (lease, client)
        logger.debug(
            "Leased node %s (%s) for trial %s", lease.node_id, lease.runner_address, spec.trial_id
        )
        return _RemoteHandle(trial_id=spec.trial_id)

    def await_ready(self, handle: EnvHandle) -> None:  # noqa: ARG002 — Protocol conformance
        """No-op: the node's runner is reached lazily on the trial's first RPC."""

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        lease, _ = self._held(handle.trial_id)
        return EnvEndpoints(
            runner_url=_normalise_runner_url(lease.runner_address), db_url=None, rag_url=None
        )

    def teardown(self, handle: EnvHandle) -> None:
        """Forget the trial on its node, then close its client and free the node."""
        trial_id = handle.trial_id
        try:
            self._forget_registration(trial_id)
        finally:
            self._drop_trial(trial_id)

    def capture_service_logs(
        self,
        handle: EnvHandle,  # noqa: ARG002
        *,
        capture_worthy: bool,  # noqa: ARG002
    ) -> dict[str, int]:
        return {}

    def get_infrastructure_snapshot(
        self,
        handle: EnvHandle,  # noqa: ARG002
    ) -> list[ContainerSnapshot]:
        return []

    # ---- Per-trial RPC operations ----

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict[str, Any]:
        return self._connected_client(trial_id).register_trial(
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        return self._connected_client(trial_id).execute_tool(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
            call_id=call_id,
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._connected_client(trial_id).grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
            termination_reason=termination_reason,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._connected_client(trial_id).get_state(
            trial_id=trial_id, include_unstable=include_unstable, tables=tables
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        return self._connected_client(trial_id).reset_trial(
            trial_id=trial_id, execute_init_actions=execute_init_actions
        )

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        """Forget ``trial_id`` on its node and free the node; idempotent.

        Returns the node runner's ``{"success", "error"}`` result, so a failed
        cleanup RPC is reported to the caller rather than raised. The node is
        freed either way. A trial that holds no lease (the retry-cleanup path
        runs before :meth:`provision`) has nothing to forget and succeeds.
        """
        try:
            return self._forget_registration(trial_id)
        finally:
            self._drop_trial(trial_id)

    def remember_trial_inputs(
        self,
        trial_id: str,
        trajectory: Trajectory,
        task_description: TaskDescription,
        judge_model_config: ModelConfig | None = None,
    ) -> None:
        """No-op: this backend produces no grade bundle, so nothing reads the inputs."""
        del trial_id, trajectory, task_description, judge_model_config

    def build_grade_bundle(self, trial_id: str, *, out_dir: Path) -> GradeBundleManifest:
        raise NotImplementedError(
            "RemoteRuntimeBackend does not produce grade bundles; "
            "grader.snapshot.enabled is unsupported under the remote runtime."
        )

    # ---- Internals ----

    def _held(self, trial_id: str) -> tuple[RunnerLease, RunnerClient]:
        held = self._trials.get(trial_id)
        if held is None:
            raise RuntimeError(
                f"RemoteRuntimeBackend: trial {trial_id!r} holds no leased runner; "
                "provision() must run before any per-trial RPC."
            )
        return held

    def _connected_client(self, trial_id: str) -> RunnerClient:
        _, client = self._held(trial_id)
        if trial_id not in self._connected_trials:
            client.connect(timeout=self.connect_timeout_s, retry_interval=self.retry_interval_s)
            self._connected_trials.add(trial_id)
        return client

    def _forget_registration(self, trial_id: str) -> dict[str, Any]:
        """Ask a connected trial's node to drop its registration."""
        if trial_id not in self._connected_trials:
            return {"success": True, "error": None}
        _, client = self._held(trial_id)
        return client.cleanup_trial(trial_id)

    def _drop_trial(self, trial_id: str) -> None:
        """Close the trial's client and free its node; a no-op for an unknown trial."""
        held = self._trials.pop(trial_id, None)
        self._connected_trials.discard(trial_id)
        if held is None:
            return
        try:
            held[1].close()
        finally:
            self.registry.release(trial_id)
            logger.debug("Released node for trial %s", trial_id)


def remote_runtime_backend_factory(ctx: RuntimeBackendBuildContext) -> RemoteRuntimeBackend:
    """Build a :class:`RemoteRuntimeBackend` from a build context."""
    if ctx.node_registry is None:
        raise ValueError(
            "The 'remote' runtime backend needs a node registry on the build context; "
            "list runner nodes under orchestrator.remote.nodes."
        )
    return RemoteRuntimeBackend(
        registry=ctx.node_registry,
        grpc_max_message_mb=ctx.grpc_max_message_mb,
        connect_timeout_s=ctx.connect_timeout_s,
        retry_interval_s=ctx.connect_retry_interval_s,
    )


__all__ = ["RemoteRuntimeBackend", "RunnerClientFactory", "remote_runtime_backend_factory"]
