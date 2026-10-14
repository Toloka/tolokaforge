"""``GrpcNodeDispatchAdapter`` — ships each whole trial to a leased gRPC node.

The adapter is a :class:`~tolokaforge.core.dispatch.adapter.DispatchAdapter`:
:meth:`run_trial_node` leases one node from a :class:`NodeRegistry`, ships the
trial's :class:`TrialNodeInputs` to it over the trial-node ``RunTrial`` RPC, and
returns the graded :class:`TrialResult`. The node runs the conductor, the agent
loop and the trial's substrate itself, so the control plane issues no per-trial
tool or grading RPCs. The adapter holds no run-wide node: every trial owns its
lease and its client, so concurrency is bounded by the number of registered
nodes.

The node-named wire exceptions (:class:`NodeRefusedError` /
:class:`NodeTransientError`) and outcome vocabulary stay internal to this module
and :mod:`tolokaforge.trial_node.protocol`; the adapter translates every failure
into the transport-neutral vocabulary of
:mod:`tolokaforge.core.dispatch.errors` before it crosses the seam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
)
from tolokaforge.core.node_registry import InMemoryNodeRegistry, NodeRegistry, RunnerLease
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.trial_node.client import TrialNodeClient
from tolokaforge.trial_node.protocol import (
    NodeErrorType,
    NodeRefusedError,
    NodeTransientError,
    NodeTrialOutcome,
)

if TYPE_CHECKING:
    from tolokaforge.core.plugin_registry import DispatchAdapterBuildContext

logger = logging.getLogger(__name__)


class NodeClient(Protocol):
    """The client surface :meth:`GrpcNodeDispatchAdapter.run_trial_node` drives."""

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None: ...

    def run_trial(self, trial_node_inputs_json: str) -> NodeTrialOutcome: ...

    def close(self) -> None: ...


NodeClientFactory = Callable[[str, int], NodeClient]
"""Builds the client for a leased node: ``(node_address, max_message_mb)``."""


def _grpc_node_client(node_address: str, max_message_mb: int) -> NodeClient:
    return TrialNodeClient(node_address, max_message_mb=max_message_mb)


@dataclass
class GrpcNodeDispatchAdapter:
    """Dispatch adapter that leases a trial node per trial from a :class:`NodeRegistry`.

    Fields:
        registry: Source of nodes; each trial leases one.
        grpc_max_message_mb: Max gRPC message size, in MB, for each node client.
        connect_timeout_s: Budget for a trial's client to reach its node.
        retry_interval_s: Pause between connect attempts within that budget.
        node_client_factory: Builds the client for a leased address; tests inject
            a fake in place of the gRPC client.
    """

    registry: NodeRegistry
    grpc_max_message_mb: int = DEFAULT_MAX_MESSAGE_MB
    connect_timeout_s: float = 30.0
    retry_interval_s: float = 1.0
    node_client_factory: NodeClientFactory = _grpc_node_client
    _leases: dict[str, RunnerLease] = field(default_factory=dict, init=False)

    # ---- Run-level lifecycle ----

    def connect(self) -> None:
        """No-op: each trial's client connects when its trial is dispatched.

        The contact budget (``connect_timeout_s`` / ``retry_interval_s``) is
        applied per trial in :meth:`run_trial_node`.
        """

    def close(self) -> None:
        """Release every lease still held; called once after the last trial."""
        for trial_id in list(self._leases):
            self.registry.release(trial_id)
        self._leases.clear()

    # ---- Whole-trial dispatch ----

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        """Run one whole trial on a leased node and return its graded result.

        Leases a node, connects within the connect budget (which also runs the
        protocol and message-size skew pre-flight), ships the inputs, and
        releases the node whatever the outcome.

        Raises:
            DispatchProvisionError: the node's substrate failed to come up.
            DispatchRefused: the node, or the skew pre-flight, refused the trial.
            DispatchTransient: the node was unreachable, busy or failed internally.
            TimeoutError: no node became free within the lease timeout.
        """
        trial_id = inputs.trial_spec.trial_id
        lease = self.registry.lease(trial_id)
        self._leases[trial_id] = lease
        logger.debug(
            "Leased node %s (%s) for trial %s", lease.node_id, lease.runner_address, trial_id
        )
        try:
            client = self.node_client_factory(lease.runner_address, self.grpc_max_message_mb)
            try:
                client.connect(timeout=self.connect_timeout_s, retry_interval=self.retry_interval_s)
                outcome = client.run_trial(inputs.model_dump_json())
            finally:
                client.close()
        except NodeRefusedError as refusal:
            raise DispatchRefused(refusal.error_type, refusal.stage, refusal.reason) from refusal
        except NodeTransientError as transient:
            raise DispatchTransient(NodeErrorType.INTERNAL, "", str(transient)) from transient
        finally:
            self._release(trial_id)
        return _result_from(outcome)

    # ---- Internals ----

    def _release(self, trial_id: str) -> None:
        """Free the trial's node; a no-op for an unknown trial."""
        if self._leases.pop(trial_id, None) is None:
            return
        self.registry.release(trial_id)
        logger.debug("Released node for trial %s", trial_id)


def _result_from(outcome: NodeTrialOutcome) -> TrialResult:
    """Return the node's graded result, or raise the neutral error its failure maps to.

    A substrate failure is a :class:`DispatchProvisionError`; a node that is busy
    or failed internally is :class:`DispatchTransient`; every other node-reported
    failure is a deterministic :class:`DispatchRefused`. The node's classified
    ``error_type`` / ``stage`` / ``reason`` are copied onto the neutral error.
    """
    if outcome.success:
        return TrialResult.model_validate_json(outcome.trial_result_json)
    if outcome.error_type == NodeErrorType.PROVISION_ERROR:
        raise DispatchProvisionError(outcome.error_type, outcome.stage, outcome.reason)
    if outcome.error_type in (NodeErrorType.NODE_BUSY, NodeErrorType.INTERNAL):
        raise DispatchTransient(outcome.error_type, outcome.stage, outcome.reason)
    raise DispatchRefused(outcome.error_type, outcome.stage, outcome.reason)


class _GrpcNodeConfig(BaseModel):
    """The ``grpc_node`` adapter's own options block (``orchestrator.dispatch.config``)."""

    model_config = {"extra": "forbid"}

    nodes: list[str]
    """``host:port`` gRPC address of each trial node. Each node serves one trial
    at a time, so the list length bounds trial concurrency."""

    lease_timeout_s: float = Field(default=300.0, gt=0.0)
    """How long a trial waits for a free node before the run fails."""

    grpc_max_message_mb: int = Field(default=DEFAULT_MAX_MESSAGE_MB, gt=0)
    """Max gRPC message size (MiB) for every node client; the run's
    ``compute.grpc_max_message_mb``."""


def grpc_node_dispatch_adapter_factory(
    ctx: DispatchAdapterBuildContext,
) -> GrpcNodeDispatchAdapter:
    """Build a :class:`GrpcNodeDispatchAdapter` from a dispatch build context.

    Reads the node list, lease timeout and gRPC message bound from
    ``ctx.config`` and the connect budget from ``ctx.connect_timeout_s`` /
    ``ctx.connect_retry_interval_s``; builds the :class:`InMemoryNodeRegistry`
    from the node list.
    """
    config = _parse_config(ctx.config)
    return GrpcNodeDispatchAdapter(
        registry=InMemoryNodeRegistry(config.nodes, lease_timeout_s=config.lease_timeout_s),
        grpc_max_message_mb=config.grpc_max_message_mb,
        connect_timeout_s=ctx.connect_timeout_s,
        retry_interval_s=ctx.connect_retry_interval_s,
    )


def _parse_config(config: Mapping[str, object]) -> _GrpcNodeConfig:
    try:
        return _GrpcNodeConfig.model_validate(dict(config))
    except Exception as exc:  # noqa: BLE001 — re-raise as a loud, adapter-named error
        raise ValueError(
            "The 'grpc_node' dispatch adapter config is invalid; it needs a 'nodes' list of "
            f"'host:port' trial nodes under orchestrator.dispatch.config. {exc}"
        ) from exc


__all__ = [
    "GrpcNodeDispatchAdapter",
    "NodeClient",
    "NodeClientFactory",
    "grpc_node_dispatch_adapter_factory",
]
