"""``TrialNodeClient`` — the gRPC client the ``grpc_node`` dispatch adapter dials a node with.

One client talks to one leased node. :meth:`connect` waits for the node within a
budget and then runs the skew pre-flight against the node's own report: the
node's protocol version must not be older than this engine's, and its message
bound must be at least the engine's, so a mismatched pair fails before a trial
is shipped rather than in the middle of one. :meth:`run_trial` is the single long
unary call that runs a whole trial; the channel carries keepalive pings so a node
that dies mid-trial surfaces as an unavailable node instead of a silent hang.
"""

from __future__ import annotations

import time

import grpc

from tolokaforge.runner.grpc_limits import (
    DEFAULT_MAX_MESSAGE_MB,
    channel_options,
    keepalive_options,
    resource_exhausted_message,
)
from tolokaforge.trial_node import trial_node_pb2, trial_node_pb2_grpc
from tolokaforge.trial_node.protocol import (
    NODE_PROTOCOL_VERSION,
    NodeErrorType,
    NodeRefusedError,
    NodeTransientError,
    NodeTrialOutcome,
    check_node_version,
)

_TRANSIENT_CODES = frozenset(
    {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.CANCELLED}
)
_HEALTH_PROBE_TIMEOUT_S = 5.0


class TrialNodeClient:
    """gRPC client for one :class:`~tolokaforge.trial_node.service.TrialNodeServiceImpl`.

    ``max_message_mb`` bounds the channel's messages — the run's
    ``compute.grpc_max_message_mb`` — and is the floor the node's own bound must
    meet. Usable as a context manager; :meth:`close` is idempotent.
    """

    def __init__(self, address: str, *, max_message_mb: int = DEFAULT_MAX_MESSAGE_MB) -> None:
        self.address = address
        self.max_message_mb = max_message_mb
        self._channel: grpc.Channel | None = None
        self._stub: trial_node_pb2_grpc.TrialNodeServiceStub | None = None

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Wait up to ``timeout`` seconds for the node, then run the skew pre-flight.

        Raises:
            NodeTransientError: the node did not answer within the budget.
            NodeRefusedError: the node's protocol is older than this engine's, its
                message bound is smaller, or it does not serve the trial-node API.
        """
        if self._stub is None:
            self._channel = grpc.insecure_channel(
                self.address, options=channel_options(self.max_message_mb) + keepalive_options()
            )
            self._stub = trial_node_pb2_grpc.TrialNodeServiceStub(self._channel)
        health = self._await_health(timeout, retry_interval)
        check_node_version(health.node_protocol_version, node_address=self.address)
        if health.node_max_message_mb < self.max_message_mb:
            raise NodeRefusedError(
                NodeErrorType.MESSAGE_SIZE_SKEW,
                "preflight",
                f"node {self.address} serves messages up to {health.node_max_message_mb} MiB but "
                f"this engine sends up to {self.max_message_mb} MiB "
                "(`compute.grpc_max_message_mb`); set TOLOKAFORGE_GRPC_MAX_MESSAGE_MB on the "
                "node to at least the engine's value.",
            )

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def __enter__(self) -> TrialNodeClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def run_trial(self, trial_node_inputs_json: str) -> NodeTrialOutcome:
        """Run one whole trial on the node and return its structured outcome.

        A node-reported failure comes back as an unsuccessful outcome; only a
        transport fault or a skewed node raises.

        Raises:
            NodeTransientError: the node became unreachable or the call was cut.
            NodeRefusedError: the node predates the trial-node API, or a message
                exceeded the size bound, or the reply's protocol is older than this
                engine's.
        """
        stub = self._connected_stub()
        request = trial_node_pb2.RunTrialRequest(
            trial_node_inputs_json=trial_node_inputs_json,
            client_protocol_version=NODE_PROTOCOL_VERSION,
        )
        try:
            response = stub.RunTrial(request)
        except grpc.RpcError as exc:
            raise self._call_failure("RunTrial", exc) from exc
        check_node_version(response.node_protocol_version, node_address=self.address)
        return NodeTrialOutcome(
            success=response.success,
            trial_result_json=response.trial_result_json,
            error_type=response.error_type,
            stage=response.stage,
            reason=response.reason,
            node_protocol_version=response.node_protocol_version,
        )

    def _connected_stub(self) -> trial_node_pb2_grpc.TrialNodeServiceStub:
        if self._stub is None:
            raise RuntimeError(
                f"TrialNodeClient for {self.address} is not connected; call connect()."
            )
        return self._stub

    def _await_health(
        self, timeout: float, retry_interval: float
    ) -> trial_node_pb2.HealthCheckResponse:
        stub = self._connected_stub()
        deadline = time.monotonic() + timeout
        while True:
            try:
                return stub.HealthCheck(
                    trial_node_pb2.HealthCheckRequest(), timeout=_HEALTH_PROBE_TIMEOUT_S
                )
            except grpc.RpcError as exc:
                failure = self._call_failure("HealthCheck", exc)
                if not isinstance(failure, NodeTransientError) or time.monotonic() >= deadline:
                    raise failure from exc
            time.sleep(retry_interval)

    def _call_failure(self, operation: str, exc: grpc.RpcError) -> Exception:
        """Classify a failed call as a skewed/refused node or a transient fault."""
        code = exc.code() if isinstance(exc, grpc.Call) else None
        detail = (exc.details() if isinstance(exc, grpc.Call) else None) or str(exc)
        if code is grpc.StatusCode.UNIMPLEMENTED:
            return NodeRefusedError(
                NodeErrorType.VERSION_SKEW,
                "protocol",
                f"node {self.address} does not serve {operation}; its image predates the "
                "trial-node API. Upgrade the node image to match the engine.",
            )
        if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
            return NodeRefusedError(
                NodeErrorType.MESSAGE_SIZE_SKEW,
                "transport",
                resource_exhausted_message(
                    f"{operation} on {self.address}", self.max_message_mb, detail
                ),
            )
        if code in _TRANSIENT_CODES:
            return NodeTransientError(
                f"node {self.address} is unavailable during {operation}: {code.name}: {detail}"
            )
        name = code.name if code is not None else "unknown"
        return NodeTransientError(f"{operation} on node {self.address} failed: {name}: {detail}")
