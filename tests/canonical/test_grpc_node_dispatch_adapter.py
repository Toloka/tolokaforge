"""Canonical lock for :class:`GrpcNodeDispatchAdapter`.

The adapter leases a node per trial and ships the whole trial to a client dialled
at the *leased* address, then translates every node failure into the
transport-neutral dispatch vocabulary. A recording node client stands in for the
gRPC transport, so no node, Docker daemon or LLM is involved. The adapter is a
pure :class:`DispatchAdapter` (``connect`` / ``close`` / ``run_trial_node``); it
is deliberately NOT a ``RuntimeBackend``.
"""

from __future__ import annotations

import pytest

from tests.canonical._factories import make_trial_spec
from tolokaforge.core.dispatch.adapter import ControllableDispatchAdapter, DispatchAdapter
from tolokaforge.core.dispatch.control import JobHandle, JobState
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
    DispatchUnsupported,
)
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.models import TaskConfig, TrialStatus
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.plugin_registry import (
    DispatchAdapterBuildContext,
    load_dispatch_adapter,
)
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.trial_node.protocol import NodeErrorType, NodeTransientError, NodeTrialOutcome

pytestmark = pytest.mark.canonical

_NODES = ["node-a:50061", "node-b:50061"]


class _RecordingNodeClient:
    def __init__(self, address: str, max_message_mb: int, outcome: NodeTrialOutcome) -> None:
        self.address = address
        self.max_message_mb = max_message_mb
        self.outcome = outcome
        self.calls: list[str] = []
        self.connect_calls: list[dict[str, float]] = []
        self.sent: list[str] = []
        self.closed = False

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        self.calls.append("connect")
        self.connect_calls.append({"timeout": timeout, "retry_interval": retry_interval})

    def run_trial(self, trial_node_inputs_json: str) -> NodeTrialOutcome:
        self.calls.append("run_trial")
        self.sent.append(trial_node_inputs_json)
        return self.outcome

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class _Factory:
    def __init__(self, outcome: NodeTrialOutcome | None = None) -> None:
        self.outcome = outcome or NodeTrialOutcome(success=True, trial_result_json=_result_json())
        self.clients: list[_RecordingNodeClient] = []

    def __call__(self, address: str, max_message_mb: int) -> _RecordingNodeClient:
        client = _RecordingNodeClient(address, max_message_mb, self.outcome)
        self.clients.append(client)
        return client


def _result_json(trial_id: str = "t:0") -> str:
    from tolokaforge.core.conductor import _default_success_trajectory

    task_id, index = trial_id.rsplit(":", 1)
    return TrialResult.from_trajectory(
        trial_id=trial_id, trajectory=_default_success_trajectory(task_id, int(index))
    ).model_dump_json()


def _inputs(trial_id: str = "t:0") -> TrialNodeInputs:
    task_id = trial_id.rsplit(":", 1)[0]
    spec = make_trial_spec(trial_id=trial_id, task_id=task_id)
    return TrialNodeInputs(
        task=TaskConfig(
            task_id=task_id,
            name=task_id,
            category="test",
            description="d",
            initial_user_message="hi",
        ),
        trial_spec=spec,
        grpc_max_message_mb=50,
        runtime="auto",
        conductor="in_process",
    )


def _adapter(
    factory: _Factory, *, nodes: list[str] = _NODES, max_mb: int = 7
) -> GrpcNodeDispatchAdapter:
    return GrpcNodeDispatchAdapter(
        registry=InMemoryNodeRegistry(nodes, lease_timeout_s=0.1),
        grpc_max_message_mb=max_mb,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        node_client_factory=factory,
    )


def test_is_a_dispatch_adapter_not_a_runtime_backend() -> None:
    adapter = _adapter(_Factory())

    assert isinstance(adapter, DispatchAdapter)
    # A pure dispatch adapter: none of the RuntimeBackend substrate surface.
    for gone in ("provision", "teardown", "endpoints", "register_trial", "grade_trial"):
        assert not hasattr(adapter, gone)


def test_run_trial_node_leases_dials_the_leased_address_and_ships_the_inputs() -> None:
    factory = _Factory()
    adapter = _adapter(factory, max_mb=7)
    inputs = _inputs("t:0")

    result = adapter.run_trial_node(inputs)

    (client,) = factory.clients
    assert client.address in _NODES
    assert client.max_message_mb == 7
    assert client.connect_calls == [{"timeout": 11.0, "retry_interval": 0.5}]
    assert client.calls == ["connect", "run_trial", "close"]
    assert TrialNodeInputs.model_validate_json(client.sent[0]) == inputs
    assert result.trial_id == "t:0"
    assert result.trajectory.status is TrialStatus.COMPLETED


def test_each_concurrent_trial_dials_its_own_node_and_times_out_when_all_leased() -> None:
    factory = _Factory()
    adapter = _adapter(factory)
    adapter.registry.lease("t:0")
    adapter.registry.lease("t:1")

    with pytest.raises(TimeoutError, match="no node became free"):
        adapter.run_trial_node(_inputs("t:2"))


def test_the_node_is_freed_after_the_trial_whatever_its_outcome() -> None:
    factory = _Factory(NodeTrialOutcome(success=False, error_type="Internal", reason="boom"))
    adapter = _adapter(factory, nodes=["only:1"])

    for _ in range(2):
        with pytest.raises(DispatchTransient):
            adapter.run_trial_node(_inputs("t:0"))

    assert [client.address for client in factory.clients] == ["only:1", "only:1"]
    assert all(client.closed for client in factory.clients)


def test_a_connect_failure_closes_the_client_and_frees_the_node() -> None:
    factory = _Factory()
    adapter = _adapter(factory, nodes=["only:1"])

    def failing_connect(timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        raise NodeTransientError("node only:1 is unavailable during HealthCheck")

    def failing_factory(address: str, max_mb: int):
        client = factory(address, max_mb)
        client.connect = failing_connect  # type: ignore[method-assign]
        return client

    adapter.node_client_factory = failing_factory
    with pytest.raises(DispatchTransient, match="unavailable"):
        adapter.run_trial_node(_inputs("t:0"))

    assert factory.clients[0].closed
    adapter.node_client_factory = factory
    adapter.run_trial_node(_inputs("t:1"))


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (NodeErrorType.NODE_BUSY, DispatchTransient),
        (NodeErrorType.INTERNAL, DispatchTransient),
        (NodeErrorType.SECRET_PREFLIGHT, DispatchRefused),
        (NodeErrorType.TASK_PACK_DRIFT, DispatchRefused),
        (NodeErrorType.NOT_IMPLEMENTED, DispatchRefused),
        (NodeErrorType.VERSION_SKEW, DispatchRefused),
        (NodeErrorType.INVALID_CONFIG, DispatchRefused),
        ("SomethingNewer", DispatchRefused),
    ],
)
def test_each_node_error_type_maps_to_its_neutral_exception(
    error_type: str, expected: type
) -> None:
    factory = _Factory(
        NodeTrialOutcome(success=False, error_type=error_type, stage="s", reason="why")
    )

    with pytest.raises(expected) as raised:
        _adapter(factory).run_trial_node(_inputs())

    assert (raised.value.error_type, raised.value.stage, raised.value.reason) == (
        error_type,
        "s",
        "why",
    )


def test_a_node_provision_error_is_a_dispatch_provision_error() -> None:
    factory = _Factory(
        NodeTrialOutcome(
            success=False,
            error_type=NodeErrorType.PROVISION_ERROR,
            stage="await_ready",
            reason="db never healthy",
        )
    )

    with pytest.raises(DispatchProvisionError) as raised:
        _adapter(factory).run_trial_node(_inputs("t:0"))

    assert (raised.value.error_type, raised.value.stage, raised.value.reason) == (
        NodeErrorType.PROVISION_ERROR,
        "await_ready",
        "db never healthy",
    )


def test_close_frees_every_lease_still_held() -> None:
    adapter = _adapter(_Factory(), nodes=["only:1"])
    adapter.registry.lease("t:0")
    adapter._leases["t:0"] = adapter.registry._leases["t:0"]  # type: ignore[attr-defined]

    adapter.close()

    # Freed: a fresh lease on the single node succeeds immediately.
    assert adapter.registry.lease("t:1").runner_address == "only:1"


def test_factory_from_the_build_context_threads_the_budget_registry_and_message_bound() -> None:
    ctx = DispatchAdapterBuildContext(
        connect_timeout_s=9.0,
        connect_retry_interval_s=0.25,
        config={"nodes": ["a:1", "b:2"], "lease_timeout_s": 42.0, "grpc_max_message_mb": 13},
    )

    adapter = load_dispatch_adapter("grpc_node")(ctx)

    assert isinstance(adapter, GrpcNodeDispatchAdapter)
    assert isinstance(adapter.registry, InMemoryNodeRegistry)
    assert adapter.registry.lease("t:0").runner_address == "a:1"
    assert (adapter.connect_timeout_s, adapter.retry_interval_s) == (9.0, 0.25)
    assert adapter.grpc_max_message_mb == 13


def test_is_a_controllable_dispatch_adapter() -> None:
    assert isinstance(_adapter(_Factory()), ControllableDispatchAdapter)


def test_cancel_is_unsupported_honestly() -> None:
    with pytest.raises(DispatchUnsupported, match="cannot cancel"):
        _adapter(_Factory()).cancel(JobHandle(trial_id="t:0"))


def test_fetch_result_is_unsupported_honestly() -> None:
    with pytest.raises(DispatchUnsupported, match="collect a result"):
        _adapter(_Factory()).fetch_result(JobHandle(trial_id="t:0"))


def test_status_is_running_while_a_lease_is_held_else_unknown() -> None:
    adapter = _adapter(_Factory())
    handle = JobHandle(trial_id="t:0")

    assert adapter.status(handle).state is JobState.UNKNOWN

    adapter._leases["t:0"] = adapter.registry.lease("t:0")
    status = adapter.status(handle)
    assert status.state is JobState.RUNNING
    assert "lease" in status.detail


def test_has_active_lease_is_the_public_lease_probe() -> None:
    adapter = _adapter(_Factory())

    # The public accessor other adapters read instead of touching ``_leases``.
    assert adapter.has_active_lease("t:0") is False
    adapter._leases["t:0"] = adapter.registry.lease("t:0")
    assert adapter.has_active_lease("t:0") is True


def test_factory_requires_a_nodes_list() -> None:
    with pytest.raises(ValueError, match="grpc_node"):
        load_dispatch_adapter("grpc_node")(DispatchAdapterBuildContext(config={}))


def test_factory_rejects_an_unknown_config_key() -> None:
    with pytest.raises(ValueError, match="grpc_node"):
        load_dispatch_adapter("grpc_node")(
            DispatchAdapterBuildContext(config={"nodes": ["a:1"], "bogus": 1})
        )
