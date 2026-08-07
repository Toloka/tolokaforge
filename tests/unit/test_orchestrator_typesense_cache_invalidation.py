"""A description cached before the TypeSense Docker rewrite must not reach a trial.

The pre-run grading gate resolves every selected task through the run-scoped
description cache before the Docker stack exists, so each cached ``SearchConfig``
carries the host-side TypeSense address. After the stack starts,
``_connect_typesense_to_runner_network`` rewrites the adapter's TypeSense params
to the Docker-network alias — and inside the runner container the host-side
address points at the runner itself, so a trial served the stale description has
every ``search_policy`` call die with "Search service is not available" (#925).
The rewrite therefore drops the cache, and this file locks that.

The lock sits on the two private seams the regression lives between —
``_task_description`` (the cache) and ``_connect_typesense_to_runner_network``
(the rewrite) — because the end-to-end surface needs a Docker daemon the unit
lane does not have. The orchestrator is real; only the Docker SDK boundary and
the adapter are stand-ins.
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

HOST_SIDE = {"enabled": True, "mode": "local", "host": "127.0.0.1", "port": 8108, "api_key": "k"}


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
    """The started-server handle the rewrite reads the container from."""

    def __init__(self) -> None:
        container = types.SimpleNamespace(container_id="ts-container-id")
        self._stack = types.SimpleNamespace(_containers={"typesense": container})


class _ServiceStack:
    """The core stack handle the rewrite reads the runner network from."""

    def __init__(self) -> None:
        net = types.SimpleNamespace(network_id="net-id", name="runner-net")
        self._networks = {"runner-net": net}


class _DockerNetwork:
    def __init__(self) -> None:
        self.connected: list[tuple[str, tuple[str, ...]]] = []

    def connect(self, container: Any, aliases: list[str] | None = None) -> None:
        self.connected.append((container.name, tuple(aliases or ())))


def _docker_module(network: _DockerNetwork) -> types.SimpleNamespace:
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(
            get=lambda cid: types.SimpleNamespace(id=cid, name="tolokaforge-typesense")
        ),
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
