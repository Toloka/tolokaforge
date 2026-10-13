"""Canonical lock for :class:`RemoteRuntimeBackend`.

The backend leases a node per trial and ships the whole trial to a client dialled
at the *leased* address. A recording node client stands in for the gRPC
transport, so no node, Docker daemon or LLM is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_trial_spec
from tolokaforge.core.models import TaskConfig, TrialStatus
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    load_runtime_backend,
)
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
from tolokaforge.core.runtime import IsolationMode, ProvisionError, RuntimeBackend
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_executor import TrialOwningBackend
from tolokaforge.trial_node.protocol import (
    NodeErrorType,
    NodeRefusedError,
    NodeTransientError,
    NodeTrialOutcome,
)

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


def _backend(
    factory: _Factory, *, nodes: list[str] = _NODES, max_mb: int = 7
) -> RemoteRuntimeBackend:
    return RemoteRuntimeBackend(
        registry=InMemoryNodeRegistry(nodes, lease_timeout_s=0.1),
        grpc_max_message_mb=max_mb,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        node_client_factory=factory,
    )


def test_satisfies_both_the_runtime_backend_and_the_trial_owning_protocols() -> None:
    backend = _backend(_Factory())

    assert isinstance(backend, RuntimeBackend)
    assert isinstance(backend, TrialOwningBackend)
    assert backend.isolation_mode is IsolationMode.SHARED_STACK
    assert backend.advertised_capabilities == frozenset()


def test_run_trial_node_leases_dials_the_leased_address_and_ships_the_inputs() -> None:
    factory = _Factory()
    backend = _backend(factory, max_mb=7)
    inputs = _inputs("t:0")

    result = backend.run_trial_node(inputs)

    (client,) = factory.clients
    assert client.address in _NODES
    assert client.max_message_mb == 7
    assert client.connect_calls == [{"timeout": 11.0, "retry_interval": 0.5}]
    assert client.calls == ["connect", "run_trial", "close"]
    assert TrialNodeInputs.model_validate_json(client.sent[0]) == inputs
    assert result.trial_id == "t:0"
    assert result.trajectory.status is TrialStatus.COMPLETED


def test_each_concurrent_trial_dials_its_own_node() -> None:
    factory = _Factory()
    backend = _backend(factory)
    first, second = (
        backend.provision(make_trial_spec(trial_id="t:0")),
        backend.provision(make_trial_spec(trial_id="t:1")),
    )

    assert backend.endpoints(first).runner_url != backend.endpoints(second).runner_url
    with pytest.raises(TimeoutError, match="no node became free"):
        backend.run_trial_node(_inputs("t:2"))


def test_the_node_is_freed_after_the_trial_whatever_its_outcome() -> None:
    factory = _Factory(NodeTrialOutcome(success=False, error_type="Internal", reason="boom"))
    backend = _backend(factory, nodes=["only:1"])

    for _ in range(2):
        with pytest.raises(NodeTransientError):
            backend.run_trial_node(_inputs("t:0"))

    assert [client.address for client in factory.clients] == ["only:1", "only:1"]
    assert all(client.closed for client in factory.clients)


def test_a_connect_failure_closes_the_client_and_frees_the_node() -> None:
    factory = _Factory()
    backend = _backend(factory, nodes=["only:1"])

    def failing_connect(timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        raise NodeTransientError("node only:1 is unavailable during HealthCheck")

    def failing_factory(address: str, max_mb: int):
        client = factory(address, max_mb)
        client.connect = failing_connect  # type: ignore[method-assign]
        return client

    backend.node_client_factory = failing_factory
    with pytest.raises(NodeTransientError, match="unavailable"):
        backend.run_trial_node(_inputs("t:0"))

    assert factory.clients[0].closed
    backend.node_client_factory = factory
    backend.run_trial_node(_inputs("t:1"))


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (NodeErrorType.NODE_BUSY, NodeTransientError),
        (NodeErrorType.INTERNAL, NodeTransientError),
        (NodeErrorType.SECRET_PREFLIGHT, NodeRefusedError),
        (NodeErrorType.TASK_PACK_DRIFT, NodeRefusedError),
        (NodeErrorType.NOT_IMPLEMENTED, NodeRefusedError),
        (NodeErrorType.VERSION_SKEW, NodeRefusedError),
        (NodeErrorType.INVALID_CONFIG, NodeRefusedError),
        ("SomethingNewer", NodeRefusedError),
    ],
)
def test_each_node_error_type_maps_to_its_exception(error_type: str, expected: type) -> None:
    factory = _Factory(
        NodeTrialOutcome(success=False, error_type=error_type, stage="s", reason="why")
    )

    with pytest.raises(expected) as raised:
        _backend(factory).run_trial_node(_inputs())

    if expected is NodeRefusedError:
        assert (raised.value.error_type, raised.value.stage, raised.value.reason) == (
            error_type,
            "s",
            "why",
        )


def test_a_node_provision_error_is_reraised_as_a_provision_error() -> None:
    factory = _Factory(
        NodeTrialOutcome(
            success=False,
            error_type=NodeErrorType.PROVISION_ERROR,
            stage="await_ready",
            reason="db never healthy",
        )
    )

    with pytest.raises(ProvisionError) as raised:
        _backend(factory).run_trial_node(_inputs("t:0"))

    assert (raised.value.trial_id, raised.value.stage, raised.value.reason) == (
        "t:0",
        "await_ready",
        "db never healthy",
    )


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("register_trial", ("t:0", "{}")),
        ("execute_tool", ("t:0", "echo", {})),
        ("grade_trial", ("t:0",)),
        ("get_state", ("t:0",)),
        ("reset_trial", ("t:0",)),
    ],
)
def test_the_per_trial_rpcs_refuse_because_the_node_owns_the_trial(
    operation: str, args: tuple[Any, ...]
) -> None:
    backend = _backend(_Factory())
    call = getattr(backend, operation)
    kwargs = {"call_id": "c1"} if operation == "execute_tool" else {}

    with pytest.raises(NotImplementedError, match="run_trial_node"):
        call(*args, **kwargs)


def test_endpoints_name_the_leased_node() -> None:
    backend = _backend(_Factory(), nodes=["node-a:50061"])
    handle = backend.provision(make_trial_spec(trial_id="t:0"))

    endpoints = backend.endpoints(handle)

    assert endpoints.runner_url == "http://node-a:50061"
    assert endpoints.db_url is None
    assert endpoints.rag_url is None


def test_endpoints_before_provision_fail_loud() -> None:
    backend = _backend(_Factory())

    with pytest.raises(RuntimeError, match="holds no leased node"):
        backend.endpoints(type("H", (), {"trial_id": "ghost:0"})())


def test_teardown_frees_the_node_and_is_idempotent() -> None:
    backend = _backend(_Factory(), nodes=["only:1"])
    handle = backend.provision(make_trial_spec(trial_id="t:0"))

    backend.teardown(handle)
    backend.teardown(handle)

    backend.provision(make_trial_spec(trial_id="t:1"))


def test_cleanup_trial_frees_the_node_and_succeeds_without_a_lease() -> None:
    backend = _backend(_Factory(), nodes=["only:1"])
    backend.provision(make_trial_spec(trial_id="t:0"))

    assert backend.cleanup_trial("t:0") == {"success": True, "error": None}
    assert backend.cleanup_trial("never-provisioned:0") == {"success": True, "error": None}
    backend.provision(make_trial_spec(trial_id="t:1"))


def test_close_frees_every_node() -> None:
    backend = _backend(_Factory())
    backend.provision(make_trial_spec(trial_id="t:0"))
    backend.provision(make_trial_spec(trial_id="t:1"))

    backend.close()

    backend.provision(make_trial_spec(trial_id="t:2"))
    backend.provision(make_trial_spec(trial_id="t:3"))


def test_provision_with_every_node_leased_times_out() -> None:
    backend = _backend(_Factory(), nodes=["only:1"])
    backend.provision(make_trial_spec(trial_id="t:0"))

    with pytest.raises(TimeoutError, match="no node became free"):
        backend.provision(make_trial_spec(trial_id="t:1"))


def test_health_check_needs_nodes() -> None:
    assert _backend(_Factory()).health_check()
    assert not _backend(_Factory(), nodes=[]).health_check()


def test_observability_and_snapshot_surfaces_are_inert() -> None:
    backend = _backend(_Factory())
    handle = backend.provision(make_trial_spec(trial_id="t:0"))

    assert backend.capture_service_logs(handle, capture_worthy=True) == {}
    assert backend.get_infrastructure_snapshot(handle) == []
    with pytest.raises(NotImplementedError, match="grade bundles"):
        backend.build_grade_bundle("t:0", out_dir=Path("unused"))


def test_factory_requires_a_node_registry() -> None:
    ctx = RuntimeBackendBuildContext(
        runner_address="", env_manifest=None, run_id="r", seeds={}, log_capture=None
    )

    with pytest.raises(ValueError, match="node registry"):
        load_runtime_backend("remote")(ctx)


def test_factory_threads_the_context_budget_and_registry() -> None:
    registry = InMemoryNodeRegistry(["a:1"])
    ctx = RuntimeBackendBuildContext(
        runner_address="",
        env_manifest=None,
        run_id="r",
        seeds={},
        log_capture=None,
        connect_timeout_s=9.0,
        connect_retry_interval_s=0.25,
        grpc_max_message_mb=13,
        node_registry=registry,
    )

    backend = load_runtime_backend("remote")(ctx)

    assert isinstance(backend, RemoteRuntimeBackend)
    assert backend.registry is registry
    assert (backend.connect_timeout_s, backend.retry_interval_s) == (9.0, 0.25)
    assert backend.grpc_max_message_mb == 13
