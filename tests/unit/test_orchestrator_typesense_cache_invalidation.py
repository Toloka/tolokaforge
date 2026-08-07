"""The TypeSense Docker bridge either completes or aborts the run.

The pre-run grading gate resolves every selected task through the run-scoped
description cache before the Docker stack exists, so each cached ``SearchConfig``
carries the host-side TypeSense address. After the stack starts,
``_connect_typesense_to_runner_network`` rewrites the adapter's TypeSense params
to the Docker-network alias — and inside the runner container the host-side
address points at the runner itself, so a trial served the stale description has
every ``search_policy`` call die with "Search service is not available" (#925).
The rewrite therefore drops the cache, and this file locks that.

It also locks the other half of the same contract: a bridge that cannot be built
raises rather than leaving that host-side address in place. There is no partial
outcome — no container, no ``runner-net``, an adapter that cannot carry the
rewritten params, or anything the Docker SDK raises all abort the run before the
config is touched (#926).

The lock sits on the two private seams the regression lives between —
``_task_description`` (the cache) and ``_connect_typesense_to_runner_network``
(the rewrite) — because the end-to-end surface needs a Docker daemon the unit
lane does not have. The orchestrator is real; only the Docker SDK boundary and
the adapter are stand-ins.

One contract this file assumes rather than proves: the mcp_core adapters read
``params["typesense"]`` at ``to_task_description`` time, not at construction
time (their construction-time snapshot serves host-side indexing only). No
in-repo adapter exercises that read, so an adapter that started snapshotting
would regress to #925 with these tests still green — that end of the contract
belongs to the external adapters' own suites.
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
# container port is always 8108, so a fixture that agrees with it cannot tell a
# rewritten address from an un-rewritten one.
HOST_SIDE = {"enabled": True, "mode": "local", "host": "127.0.0.1", "port": 8199, "api_key": "k"}


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


class _ParamlessAdapter:
    """An adapter with no ``params`` — nothing the rewrite could propagate into."""


class _TypesenseServer:
    """The started-server handle the rewrite reads the container from."""

    def __init__(self, *, containers: dict[str, Any] | None = None) -> None:
        container = types.SimpleNamespace(container_id="ts-container-id")
        self._stack = types.SimpleNamespace(
            _containers={"typesense": container} if containers is None else containers
        )


class _ServiceStack:
    """The core stack handle the rewrite reads the runner network from."""

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


def test_a_pre_stack_description_is_rebuilt_with_the_docker_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's cache warm-up must not pin the host-side address on the trial."""
    network = _DockerNetwork()
    monkeypatch.setitem(sys.modules, "docker", _docker_module(network))

    orchestrator = _orchestrator(tmp_path)
    orchestrator.adapter = _KbAdapter({"typesense": dict(HOST_SIDE)})
    orchestrator._typesense_server = _TypesenseServer()

    # The pre-run grading gate resolves the task first — stack not started yet.
    before = orchestrator._task_description("TASK-KB")
    assert before.search.host == "127.0.0.1"

    orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    # The rewrite really ran, against the real method's docker calls...
    assert network.connected == [("tolokaforge-typesense", ("typesense",))]

    # ...and the trial-facing resolve now rebuilds with the Docker alias
    # instead of serving the stale host-side copy back.
    after = orchestrator._task_description("TASK-KB")
    assert after.search.host == "typesense"
    assert after.search.port == 8108


def test_descriptions_cache_again_once_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rewrite costs one rebuild per task — the cache is dropped, not disabled."""
    monkeypatch.setitem(sys.modules, "docker", _docker_module(_DockerNetwork()))

    orchestrator = _orchestrator(tmp_path)
    adapter = _KbAdapter({"typesense": dict(HOST_SIDE)})
    orchestrator.adapter = adapter
    orchestrator._typesense_server = _TypesenseServer()

    orchestrator._task_description("TASK-KB")
    orchestrator._connect_typesense_to_runner_network(_ServiceStack())
    orchestrator._task_description("TASK-KB")
    orchestrator._task_description("TASK-KB")

    assert adapter.builds == 2


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


def test_a_missing_typesense_container_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that was rolled back leaves an empty stack — the run cannot continue on it."""
    orchestrator, _ = _bridge_inputs(tmp_path, monkeypatch, server=_TypesenseServer(containers={}))

    with pytest.raises(RuntimeError, match=r"no 'typesense' container"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert orchestrator.config.orchestrator.typesense.host == "127.0.0.1"


def test_a_bridge_with_no_typesense_config_to_rewrite_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge only runs after a server started, so the config block must still be there."""
    orchestrator, _ = _bridge_inputs(tmp_path, monkeypatch)
    orchestrator.config.orchestrator.typesense = None

    with pytest.raises(RuntimeError, match="no TypeSense configuration to rewrite"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())


def test_a_missing_runner_network_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without runner-net there is no network to make typesense:8108 resolve on."""
    orchestrator, _ = _bridge_inputs(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match=r"no 'runner-net' network"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack(with_runner_net=False))

    assert orchestrator.config.orchestrator.typesense.host == "127.0.0.1"


def test_a_docker_sdk_failure_reaches_the_caller_as_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No blanket handler stands between the Docker SDK and the run."""
    orchestrator, _ = _bridge_inputs(
        tmp_path, monkeypatch, docker_error=_DockerSdkError("daemon went away")
    )

    with pytest.raises(_DockerSdkError, match="daemon went away"):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert orchestrator.config.orchestrator.typesense.host == "127.0.0.1"


@pytest.mark.parametrize(
    ("adapter", "reason"),
    [
        (None, r"no adapter was created"),
        (_ParamlessAdapter(), r"exposes no 'params'"),
    ],
    ids=["no-adapter", "adapter-without-params"],
)
def test_an_adapter_that_cannot_carry_the_rewrite_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: Any, reason: str
) -> None:
    """The #925 corruption used to happen here with no log line at all."""
    orchestrator, network = _bridge_inputs(tmp_path, monkeypatch)
    orchestrator.adapter = adapter

    with pytest.raises(RuntimeError, match=reason):
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    # Refused before any of it happened: no container joined the network and the
    # config still describes the host side.
    assert network.connected == []
    assert orchestrator.config.orchestrator.typesense.host == "127.0.0.1"


def test_every_abort_names_the_host_side_address_trials_would_have_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator reads which address the run was about to ship to the trials."""
    orchestrator, _ = _bridge_inputs(tmp_path, monkeypatch, server=_TypesenseServer(containers={}))

    with pytest.raises(RuntimeError) as raised:
        orchestrator._connect_typesense_to_runner_network(_ServiceStack())

    assert "127.0.0.1:8199" in str(raised.value)
