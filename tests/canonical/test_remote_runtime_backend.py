"""Canonical lock for :class:`RemoteRuntimeBackend`.

The backend leases a runner node per trial and dispatches the trial's RPCs to a
client dialled at the *leased* address. A recording runner client stands in for
the gRPC transport, so no node, Docker daemon or LLM is involved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tolokaforge.core.trial_node as trial_node
from tests.canonical._factories import make_trial_spec
from tests.canonical._fakes import RecordingClientFactory
from tolokaforge.core.models import GraderConfig, ModelConfig
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    TrialGraderContext,
    load_runtime_backend,
)
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
from tolokaforge.core.runtime import IsolationMode, RuntimeBackend
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader
from tolokaforge.core.trial_node import TrialExecutionContext, build_trial_execution_unit

pytestmark = pytest.mark.canonical

_NODES = ["node-a:50051", "node-b:50051"]


def _backend(
    factory: RecordingClientFactory, *, nodes: list[str] = _NODES, max_mb: int = 7
) -> RemoteRuntimeBackend:
    return RemoteRuntimeBackend(
        registry=InMemoryNodeRegistry(nodes, lease_timeout_s=0.1),
        grpc_max_message_mb=max_mb,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        runner_client_factory=factory,
    )


def test_satisfies_the_runtime_backend_protocol() -> None:
    backend = _backend(RecordingClientFactory())

    assert isinstance(backend, RuntimeBackend)
    assert backend.isolation_mode is IsolationMode.SHARED_STACK
    assert backend.advertised_capabilities == frozenset()


def test_provision_leases_and_dials_the_leased_address() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory, max_mb=7)

    backend.provision(make_trial_spec(trial_id="t:0"))
    backend.provision(make_trial_spec(trial_id="t:1"))

    assert sorted(c.runner_address for c in factory.clients) == sorted(_NODES)
    assert {c.max_message_mb for c in factory.clients} == {7}


def test_per_trial_rpcs_reach_that_trials_leased_client() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory)
    backend.provision(make_trial_spec(trial_id="t:0"))
    backend.provision(make_trial_spec(trial_id="t:1"))
    first, second = factory.clients

    backend.register_trial("t:0", "{}")
    backend.execute_tool("t:0", "echo", {"x": 1}, call_id="c1")
    backend.grade_trial("t:1", llm_messages_json="[]", termination_reason="agent_done")
    backend.get_state("t:1")
    backend.reset_trial("t:1")

    assert first.names() == ["register_trial", "execute_tool"]
    assert first.calls[1][1] == {
        "trial_id": "t:0",
        "tool_name": "echo",
        "arguments": {"x": 1},
        "executor": "agent",
        "call_id": "c1",
    }
    assert second.names() == ["grade_trial", "get_state", "reset_trial"]


def test_connect_is_deferred_to_the_first_rpc_and_not_repeated() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory)
    backend.connect()
    backend.provision(make_trial_spec(trial_id="t:0"))
    (client,) = factory.clients
    assert client.connect_calls == []

    backend.register_trial("t:0", "{}")
    backend.execute_tool("t:0", "echo", {}, call_id="c1")
    backend.grade_trial("t:0")

    assert client.connect_calls == [{"timeout": 11.0, "retry_interval": 0.5}]


def test_an_rpc_before_provision_fails_loud() -> None:
    backend = _backend(RecordingClientFactory())

    with pytest.raises(RuntimeError, match="holds no leased runner"):
        backend.execute_tool("ghost:0", "echo", {}, call_id="c1")


def test_endpoints_name_the_leased_runner() -> None:
    backend = _backend(RecordingClientFactory(), nodes=["node-a:50051"])
    handle = backend.provision(make_trial_spec(trial_id="t:0"))

    endpoints = backend.endpoints(handle)

    assert endpoints.runner_url == "http://node-a:50051"
    assert endpoints.db_url is None
    assert endpoints.rag_url is None


def test_teardown_forgets_the_trial_closes_the_client_and_frees_the_node() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory, nodes=["only:1"])
    handle = backend.provision(make_trial_spec(trial_id="t:0"))
    backend.register_trial("t:0", "{}")
    (client,) = factory.clients

    backend.teardown(handle)
    backend.teardown(handle)

    assert client.closed
    assert client.names() == ["register_trial", "cleanup_trial"]
    backend.provision(make_trial_spec(trial_id="t:1"))
    assert factory.clients[1].runner_address == "only:1"


def test_a_never_connected_trial_is_released_without_an_rpc() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory, nodes=["only:1"])
    handle = backend.provision(make_trial_spec(trial_id="t:0"))

    backend.teardown(handle)

    assert factory.clients[0].calls == []
    assert factory.clients[0].closed


def test_cleanup_trial_without_a_lease_succeeds() -> None:
    backend = _backend(RecordingClientFactory())

    assert backend.cleanup_trial("never-provisioned:0") == {"success": True, "error": None}


def test_close_closes_every_client_and_frees_every_node() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory)
    backend.provision(make_trial_spec(trial_id="t:0"))
    backend.provision(make_trial_spec(trial_id="t:1"))

    backend.close()

    assert all(c.closed for c in factory.clients)
    backend.provision(make_trial_spec(trial_id="t:2"))
    backend.provision(make_trial_spec(trial_id="t:3"))


def test_provision_with_every_node_leased_times_out() -> None:
    backend = _backend(RecordingClientFactory(), nodes=["only:1"])
    backend.provision(make_trial_spec(trial_id="t:0"))

    with pytest.raises(TimeoutError, match="no node became free"):
        backend.provision(make_trial_spec(trial_id="t:1"))


def test_a_failed_client_build_releases_the_lease() -> None:
    attempts: list[str] = []

    def failing_factory(address: str, max_mb: int):
        attempts.append(address)
        if len(attempts) == 1:
            raise OSError("cannot build a channel")
        return RecordingClientFactory()(address, max_mb)

    backend = _backend(RecordingClientFactory(), nodes=["only:1"])
    backend.runner_client_factory = failing_factory

    with pytest.raises(OSError, match="cannot build a channel"):
        backend.provision(make_trial_spec(trial_id="t:0"))

    backend.provision(make_trial_spec(trial_id="t:1"))


def test_health_check_needs_nodes_and_healthy_connected_runners() -> None:
    factory = RecordingClientFactory()
    backend = _backend(factory)
    assert backend.health_check()
    backend.provision(make_trial_spec(trial_id="t:0"))
    backend.register_trial("t:0", "{}")
    assert backend.health_check()

    factory.clients[0].closed = True

    assert not backend.health_check()
    assert not _backend(RecordingClientFactory(), nodes=[]).health_check()


def test_observability_and_snapshot_surfaces_are_inert() -> None:
    backend = _backend(RecordingClientFactory())
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


def test_exposes_no_static_runner_address_so_the_grader_shim_is_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(RecordingClientFactory())
    assert not hasattr(backend, "runner_address")
    seen: list[TrialGraderContext] = []

    def grader_factory(ctx: TrialGraderContext) -> RunnerRPCTrialGrader:
        seen.append(ctx)
        return RunnerRPCTrialGrader(
            runner_address="", logger=ctx.logger, runtime_backend=ctx.runtime_backend
        )

    monkeypatch.setattr(trial_node, "load_trial_grader", lambda name: grader_factory)

    unit = build_trial_execution_unit(_unit_context(backend, tmp_path))

    assert seen[0].runtime_backend is backend
    assert seen[0].runner_address is None
    unit.close()


def _unit_context(backend: RemoteRuntimeBackend, tmp_path: Path) -> TrialExecutionContext:
    adapter = MagicMock()
    adapter.trial_grader_name = "runner_rpc"
    return TrialExecutionContext(
        adapter=adapter,
        artifact_writer=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        agent_client=MagicMock(config=ModelConfig(provider="openai", name="gpt-4")),
        runtime_backend=backend,
        output_dir=tmp_path,
        request_limiter=None,
        grader_config=GraderConfig(name="runner_rpc"),
        grpc_max_message_mb=7,
    )
