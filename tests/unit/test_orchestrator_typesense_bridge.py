"""The TypeSense Docker bridge either completes or aborts the run.

The runner container is created knowing the alias and the container port, and
that address resolves only while TypeSense is a member of ``runner-net``.
Building that membership is the whole job: the run config keeps the host-side
address the orchestrator and the adapter index against, the adapter keeps the
connection details it was created with, and descriptions resolved before the
stack existed stay cached. Every one of those is a thing the bridge used to
rewrite, and the rewrite is what #925 regressed on.

The other half of the same contract: a bridge that cannot be built raises
rather than letting the run reach a trial. There is no partial outcome — no
container, no ``runner-net``, a missing ``orchestrator.typesense`` block, or
anything the Docker SDK raises all abort before any container joins a network.

The lock sits on ``_connect_typesense_to_runner_network`` and on
``_task_description`` (the cache it no longer drops), because the end-to-end
surface needs a Docker daemon the unit lane does not have. The orchestrator is
real; only the Docker SDK boundary and the adapter are stand-ins.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TypeSenseConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.runner.models import SearchConfig, TaskDescription

pytestmark = pytest.mark.unit

# The host-mapped port is deliberately not 8108: inside a Docker network the
# container port is always 8108, so a fixture that agrees with it cannot tell an
# untouched address from one the bridge overwrote with the alias.
HOST_SIDE = {"enabled": True, "mode": "local", "host": "127.0.0.1", "port": 8199, "api_key": "k"}
HOST_SIDE_ADDRESS = "127.0.0.1:8199"
INJECTED_ADDRESS = "typesense:8108"


class _DockerSdkError(Exception):
    """What the Docker SDK raises — it must reach the caller as itself."""


class _KbAdapter:
    """An adapter that answers with whatever TypeSense params it currently holds.

    The frozen mcp_core adapter reads ``params["typesense"]`` into each
    description's ``SearchConfig`` the same way; what matters here is only that
    the address in the description is the address in the params at build time.
    """

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        self.builds = 0

    def to_task_description(self, task_id: str) -> TaskDescription:
        self.builds += 1
        ts = self.params.get("typesense") or {}
        port = ts.get("port")
        return TaskDescription(
            task_id=task_id,
            name=task_id,
            category="unit",
            description="",
            adapter_type="native",
            system_prompt="",
            search=SearchConfig(
                enabled=False,
                domain_name="unit_domain",
                host=ts.get("host"),
                port=port if isinstance(port, int) else None,
                api_key=ts.get("api_key"),
            ),
        )


class _TypesenseServer:
    """The started-server handle the bridge reads the container from."""

    def __init__(self, *, containers: dict[str, Any] | None = None) -> None:
        container = types.SimpleNamespace(container_id="ts-container-id")
        self._stack = types.SimpleNamespace(
            _containers={"typesense": container} if containers is None else containers
        )


class _ServiceStack:
    """The core stack handle the bridge reads the runner network from."""

    def __init__(self, *, with_runner_net: bool = True) -> None:
        net = types.SimpleNamespace(network_id="net-id", name="runner-net")
        self._networks = {"runner-net": net} if with_runner_net else {}


class _DockerNetwork:
    def __init__(self) -> None:
        self.connected: list[tuple[str, tuple[str, ...]]] = []

    def connect(self, container: Any, aliases: list[str] | None = None) -> None:
        self.connected.append((container.name, tuple(aliases or ())))


def _docker_module(
    network: _DockerNetwork, *, containers_get_raises: Exception | None = None
) -> types.SimpleNamespace:
    def _get_container(container_id: str) -> Any:
        if containers_get_raises is not None:
            raise containers_get_raises
        return types.SimpleNamespace(id=container_id, name="tolokaforge-typesense")

    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=_get_container),
        networks=types.SimpleNamespace(get=lambda _nid: network),
    )
    return types.SimpleNamespace(from_env=lambda: client)


def _orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                auto_start_services=False,
                shuffle_trials=False,
                typesense=TypeSenseConfig(**HOST_SIDE),
            ),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "results"), projects=[str(tmp_path)]
            ),
        ),
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )


def _bridge_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server: _TypesenseServer | None = None,
    docker_error: Exception | None = None,
) -> tuple[Orchestrator, _DockerNetwork]:
    """An orchestrator whose bridge inputs are all sound unless a test spoils one."""
    network = _DockerNetwork()
    monkeypatch.setitem(
        sys.modules, "docker", _docker_module(network, containers_get_raises=docker_error)
    )
    orchestrator = _orchestrator(tmp_path)
    orchestrator.adapter = _KbAdapter({"typesense": dict(HOST_SIDE)})
    orchestrator._typesense_server = server if server is not None else _TypesenseServer()
    return orchestrator, network


def test_a_completed_bridge_leaves_the_run_config_the_adapter_and_the_cache_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Joining the network is the whole job — the address travels a different way.

    The runner already holds the alias, injected at container creation. The
    host-side address is what the orchestrator and the adapter index against, so
    a bridge that rewrote it into the adapter's params would leave host-side
    indexing pointed at a name only the Docker network resolves.
    """
    orchestrator, network = _bridge_inputs(tmp_path, monkeypatch)
    adapter = orchestrator.adapter
    before = orchestrator._task_description("TASK-KB")

    orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    # The bridge really ran, against the real method's Docker calls.
    assert network.connected == [("tolokaforge-typesense", ("typesense",))]

    assert adapter.params["typesense"] == HOST_SIDE
    configured = orchestrator.config.orchestrator.typesense
    assert (configured.host, configured.port) == ("127.0.0.1", 8199)

    # The cached description is served back, not rebuilt: one build, one object.
    assert orchestrator._task_description("TASK-KB") is before
    assert adapter.builds == 1
    assert before.search.host == "127.0.0.1"


def test_a_missing_typesense_container_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that was rolled back leaves an empty stack — the run cannot continue on it."""
    orchestrator, network = _bridge_inputs(
        tmp_path, monkeypatch, server=_TypesenseServer(containers={})
    )

    with pytest.raises(RuntimeError, match=r"no 'typesense' container"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert network.connected == []


def test_a_bridge_with_no_typesense_configuration_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge only runs after a server started, so the config block must still be there.

    Without it the two aborts below have no host-side address to name, and an
    operator reading them cannot tell which server failed to be bridged.
    """
    orchestrator, network = _bridge_inputs(tmp_path, monkeypatch)
    orchestrator.config.orchestrator.typesense = None

    with pytest.raises(RuntimeError, match="ran with no TypeSense configuration"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert network.connected == []


def test_a_missing_runner_network_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without runner-net there is no network for the runner's alias to resolve on."""
    orchestrator, network = _bridge_inputs(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match=r"no 'runner-net' network"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack(with_runner_net=False))

    assert network.connected == []


def test_a_docker_sdk_failure_reaches_the_caller_as_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No blanket handler stands between the Docker SDK and the run."""
    orchestrator, network = _bridge_inputs(
        tmp_path, monkeypatch, docker_error=_DockerSdkError("daemon went away")
    )

    with pytest.raises(_DockerSdkError, match="daemon went away"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert network.connected == []


@pytest.mark.parametrize(
    ("spoiled", "server", "service_stack"),
    [
        ("no-typesense-container", _TypesenseServer(containers={}), _ServiceStack()),
        ("no-runner-net", None, _ServiceStack(with_runner_net=False)),
    ],
)
def test_a_failed_bridge_names_both_addresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoiled: str,
    server: _TypesenseServer | None,
    service_stack: _ServiceStack,
) -> None:
    """The operator reads the alias the runner was handed and where the server is.

    Neither address alone identifies the fault: the trials are configured with
    the alias whatever happens here, and the alias resolving to nothing is
    indistinguishable from a wrong host-side address until both are on the page.
    """
    orchestrator, _ = _bridge_inputs(tmp_path, monkeypatch, server=server)

    with pytest.raises(RuntimeError) as raised:
        orchestrator._connect_typesense_to_runner_network(service_stack)

    message = str(raised.value)
    assert INJECTED_ADDRESS in message
    assert HOST_SIDE_ADDRESS in message
