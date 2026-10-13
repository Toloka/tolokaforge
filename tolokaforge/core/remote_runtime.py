"""``RemoteRuntimeBackend`` — dispatches each whole trial to a leased remote node.

The backend is a :class:`~tolokaforge.core.trial_executor.TrialOwningBackend`:
:meth:`run_trial_node` leases one node from the :class:`NodeRegistry`, ships the
trial's :class:`TrialNodeInputs` to it over the trial-node ``RunTrial`` RPC, and
returns the graded :class:`TrialResult`. The node runs the conductor, the agent
loop and the trial's substrate itself, so the control plane issues no per-trial
tool or grading RPCs. The backend holds no run-wide node: every trial owns its
lease and its client, so concurrency is bounded by the number of registered nodes.

The backend exposes no ``runner_address`` attribute. A run-wide address would
name no single node, and the per-trial ``RuntimeBackend`` operations below exist
only to satisfy the protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, Protocol

from tolokaforge.core.node_registry import NodeRegistry, RunnerLease
from tolokaforge.core.runtime import EnvHandle, IsolationMode, ProvisionError
from tolokaforge.core.shared_stack_runtime import _normalise_runner_url
from tolokaforge.core.trial import (
    DEFAULT_TOOL_TIMEOUT_S,
    EnvEndpoints,
    TrialNodeInputs,
    TrialResult,
)
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.tools.registry import ToolResult
from tolokaforge.trial_node.client import TrialNodeClient
from tolokaforge.trial_node.protocol import (
    NodeErrorType,
    NodeRefusedError,
    NodeTransientError,
    NodeTrialOutcome,
)

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


class NodeClient(Protocol):
    """The client surface :meth:`RemoteRuntimeBackend.run_trial_node` drives."""

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None: ...

    def run_trial(self, trial_node_inputs_json: str) -> NodeTrialOutcome: ...

    def close(self) -> None: ...


NodeClientFactory = Callable[[str, int], NodeClient]
"""Builds the client for a leased node: ``(node_address, max_message_mb)``."""


def _grpc_node_client(node_address: str, max_message_mb: int) -> NodeClient:
    return TrialNodeClient(node_address, max_message_mb=max_message_mb)


@dataclass(frozen=True)
class _RemoteHandle:
    """Handle returned by :meth:`RemoteRuntimeBackend.provision`."""

    trial_id: str


def _node_owns_the_trial(operation: str) -> NoReturn:
    raise NotImplementedError(
        f"RemoteRuntimeBackend.{operation}: the node owns the whole trial, so the control "
        "plane issues no per-trial RPCs; see RemoteRuntimeBackend.run_trial_node."
    )


@dataclass
class RemoteRuntimeBackend:
    """Runtime backend that leases a trial node per trial from a :class:`NodeRegistry`.

    Fields:
        registry: Source of nodes; each trial leases one.
        grpc_max_message_mb: Max gRPC message size, in MB, for each node client.
        connect_timeout_s: Budget for a trial's client to reach its node.
        retry_interval_s: Pause between connect attempts within that budget.
        node_client_factory: Builds the client for a leased address; tests inject
            a fake in place of the gRPC client.
    """

    # The leased node owns a whole trial, so no local stack backs this backend;
    # every consumer that branches on isolation takes the shared-stack path.
    isolation_mode: ClassVar[IsolationMode] = IsolationMode.SHARED_STACK
    advertised_capabilities: ClassVar[frozenset[str]] = frozenset()

    registry: NodeRegistry
    grpc_max_message_mb: int = DEFAULT_MAX_MESSAGE_MB
    connect_timeout_s: float = 30.0
    retry_interval_s: float = 1.0
    node_client_factory: NodeClientFactory = _grpc_node_client
    _leases: dict[str, RunnerLease] = field(default_factory=dict, init=False)

    # ---- Run-level lifecycle ----

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """No-op: each trial's client connects when its trial is dispatched."""
        del timeout, retry_interval

    def close(self) -> None:
        for trial_id in list(self._leases):
            self.registry.release(trial_id)
        self._leases.clear()

    def health_check(self) -> bool:
        """Healthy when the registry lists at least one node."""
        return self.registry.has_nodes()

    # ---- Whole-trial dispatch ----

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        """Run one whole trial on a leased node and return its graded result.

        Leases a node, connects within the connect budget (which also runs the
        protocol and message-size skew pre-flight), ships the inputs, and
        releases the node whatever the outcome.

        Raises:
            ProvisionError: the node's substrate failed to come up.
            NodeRefusedError: the node, or the skew pre-flight, refused the trial.
            NodeTransientError: the node was unreachable, busy or failed internally.
            TimeoutError: no node became free within the lease timeout.
        """
        trial_id = inputs.trial_spec.trial_id
        handle = self.provision(inputs.trial_spec)
        try:
            client = self.node_client_factory(
                self._leases[trial_id].runner_address, self.grpc_max_message_mb
            )
            try:
                client.connect(timeout=self.connect_timeout_s, retry_interval=self.retry_interval_s)
                outcome = client.run_trial(inputs.model_dump_json())
            finally:
                client.close()
        finally:
            self.teardown(handle)
        return _result_from(outcome, trial_id)

    # ---- Per-trial provisioning: a node lease ----

    def provision(self, spec: TrialSpec) -> EnvHandle:
        lease = self.registry.lease(spec.trial_id)
        self._leases[spec.trial_id] = lease
        logger.debug(
            "Leased node %s (%s) for trial %s", lease.node_id, lease.runner_address, spec.trial_id
        )
        return _RemoteHandle(trial_id=spec.trial_id)

    def await_ready(self, handle: EnvHandle) -> None:  # noqa: ARG002 — Protocol conformance
        """No-op: the node is reached when the trial is dispatched."""

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        lease = self._lease(handle.trial_id)
        return EnvEndpoints(
            runner_url=_normalise_runner_url(lease.runner_address), db_url=None, rag_url=None
        )

    def teardown(self, handle: EnvHandle) -> None:
        """Free the trial's node; idempotent."""
        self._release(handle.trial_id)

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

    # ---- Per-trial RPC operations: owned by the node ----

    def register_trial(
        self,
        trial_id: str,  # noqa: ARG002
        trial_spec_json: str,  # noqa: ARG002
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,  # noqa: ARG002
    ) -> dict[str, Any]:
        _node_owns_the_trial("register_trial")

    def execute_tool(
        self,
        trial_id: str,  # noqa: ARG002
        tool_name: str,  # noqa: ARG002
        arguments: dict[str, Any],  # noqa: ARG002
        executor: str = "agent",  # noqa: ARG002
        *,
        call_id: str,  # noqa: ARG002
    ) -> ToolResult:
        _node_owns_the_trial("execute_tool")

    def grade_trial(
        self,
        trial_id: str,  # noqa: ARG002
        llm_messages_json: str | None = None,  # noqa: ARG002
        grading_components: list[str] | None = None,  # noqa: ARG002
        termination_reason: str | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        _node_owns_the_trial("grade_trial")

    def get_state(
        self,
        trial_id: str,  # noqa: ARG002
        include_unstable: bool = True,  # noqa: ARG002
        tables: list[str] | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        _node_owns_the_trial("get_state")

    def reset_trial(
        self,
        trial_id: str,  # noqa: ARG002
        execute_init_actions: bool = False,  # noqa: ARG002
    ) -> dict[str, Any]:
        _node_owns_the_trial("reset_trial")

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        """Free ``trial_id``'s node; idempotent.

        The node holds nothing between trials, so there is no registration to
        forget. A trial that holds no lease (the retry-cleanup path runs after a
        trial already released it) has nothing to free and succeeds.
        """
        self._release(trial_id)
        return {"success": True, "error": None}

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

    def _lease(self, trial_id: str) -> RunnerLease:
        lease = self._leases.get(trial_id)
        if lease is None:
            raise RuntimeError(
                f"RemoteRuntimeBackend: trial {trial_id!r} holds no leased node; "
                "provision() must run first."
            )
        return lease

    def _release(self, trial_id: str) -> None:
        """Free the trial's node; a no-op for an unknown trial."""
        if self._leases.pop(trial_id, None) is None:
            return
        self.registry.release(trial_id)
        logger.debug("Released node for trial %s", trial_id)


def _result_from(outcome: NodeTrialOutcome, trial_id: str) -> TrialResult:
    """Return the node's graded result, or raise the exception its failure maps to.

    A substrate failure is a :class:`ProvisionError`; a node that is busy or failed
    internally is transient; every other node-reported failure is a deterministic
    refusal.
    """
    if outcome.success:
        return TrialResult.model_validate_json(outcome.trial_result_json)
    if outcome.error_type == NodeErrorType.PROVISION_ERROR:
        raise ProvisionError(trial_id=trial_id, stage=outcome.stage, reason=outcome.reason)
    if outcome.error_type in (NodeErrorType.NODE_BUSY, NodeErrorType.INTERNAL):
        raise NodeTransientError(f"[{outcome.error_type}] {outcome.reason}")
    raise NodeRefusedError(outcome.error_type, outcome.stage, outcome.reason)


def remote_runtime_backend_factory(ctx: RuntimeBackendBuildContext) -> RemoteRuntimeBackend:
    """Build a :class:`RemoteRuntimeBackend` from a build context."""
    if ctx.node_registry is None:
        raise ValueError(
            "The 'remote' runtime backend needs a node registry on the build context; "
            "list trial nodes under orchestrator.remote.nodes."
        )
    return RemoteRuntimeBackend(
        registry=ctx.node_registry,
        grpc_max_message_mb=ctx.grpc_max_message_mb,
        connect_timeout_s=ctx.connect_timeout_s,
        retry_interval_s=ctx.connect_retry_interval_s,
    )


__all__ = [
    "NodeClient",
    "NodeClientFactory",
    "RemoteRuntimeBackend",
    "remote_runtime_backend_factory",
]
