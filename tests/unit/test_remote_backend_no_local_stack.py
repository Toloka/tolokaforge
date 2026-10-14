"""``--runtime remote`` through a real :class:`Orchestrator`: no local stack, loud refusals.

The backend-only tests cannot see the orchestrator's auto-start block, which
builds an ``EngineStack`` whenever ``auto_start_services`` is on (its default).
These runs drive ``Orchestrator.run`` over a real on-disk pack and patch the
stack builders to raise, so a regression that starts a local stack fails here.
The trial itself runs on an in-process node.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._node_fakes import InProcessNode, InProcessNodeClientFactory
from tolokaforge.core.models import (
    EvaluationConfig,
    GraderConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]

_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"
_NODES = ["node-a:50061", "node-b:50061"]


@pytest.fixture(autouse=True)
def _no_local_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the remote runtime must not build a local EngineStack")

    monkeypatch.setattr("tolokaforge.docker.stacks.core_stack", refuse)
    monkeypatch.setattr("tolokaforge.docker.stacks.full_stack", refuse)


def _write_pack(root: Path, *, manifest: bool = False) -> None:
    task_dir = root / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    task: dict[str, Any] = {
        "task_id": "flat",
        "description": "flat",
        "initial_state": {"json_db": "initial_state.json"},
        "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
        "grading": "grading.yaml",
    }
    if manifest:
        task["environment_manifest"] = {
            "stack": {"compose_file": str(_FIXTURES / "safe_one_service.yaml")}
        }
    (task_dir / "task.yaml").write_text(yaml.safe_dump(task))
    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {"weights": {"state_checks": 1.0}},
                "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
            }
        )
    )


def _orchestrator(
    root: Path,
    out: Path,
    *,
    orchestrator: dict[str, Any],
    grader: GraderConfig | None = None,
) -> tuple[Orchestrator, InProcessNode, InProcessNodeClientFactory]:
    node = InProcessNode(out / "node-out")
    factory = InProcessNodeClientFactory(node)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models={
                "agent": ModelConfig(provider="openai", name="gpt-4"),
                "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
                "judge": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
            },
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=2,
                shuffle_trials=False,
                runtime="remote",
                remote={"nodes": _NODES, "lease_timeout_s": 5},
                **orchestrator,
            ),
            evaluation=EvaluationConfig(output_dir=str(out), projects=[str(root)]),
            grader=grader,
        )
    backend = RemoteRuntimeBackend(
        registry=InMemoryNodeRegistry(_NODES, lease_timeout_s=5), node_client_factory=factory
    )
    return Orchestrator(config, deps=OrchestratorDeps(runtime_backend=backend)), node, factory


def test_default_auto_start_builds_no_stack_and_each_trial_gets_a_leased_node(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path / "pack")
    orch, node, factory = _orchestrator(tmp_path / "pack", tmp_path / "out", orchestrator={})
    assert orch.config.orchestrator.auto_start_services is True

    orch.run(run_id="r", output_dir=tmp_path / "run")

    assert len(node.received) == 2
    assert {client.address for client in factory.clients} <= set(_NODES)
    assert all(client.closed for client in factory.clients)


def test_the_orchestrator_builds_the_remote_backend_from_the_configured_nodes(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path / "pack")
    orch, _, _ = _orchestrator(tmp_path / "pack", tmp_path / "out", orchestrator={})

    backend = orch._construct_runtime_backend("unused:1")

    assert isinstance(backend, RemoteRuntimeBackend)
    assert backend.registry.lease("t:0").runner_address == _NODES[0]
    assert backend.registry.lease("t:1").runner_address == _NODES[1]


def test_explicit_auto_start_services_with_remote_fails_loud(tmp_path: Path) -> None:
    _write_pack(tmp_path / "pack")
    orch, node, _ = _orchestrator(
        tmp_path / "pack", tmp_path / "out", orchestrator={"auto_start_services": True}
    )

    with pytest.raises(ValueError, match="contradicts orchestrator.auto_start_services"):
        orch.run(run_id="r", output_dir=tmp_path / "run")

    assert node.received == []


def test_explicit_auto_start_false_with_remote_runs(tmp_path: Path) -> None:
    _write_pack(tmp_path / "pack")
    orch, node, _ = _orchestrator(
        tmp_path / "pack", tmp_path / "out", orchestrator={"auto_start_services": False}
    )

    orch.run(run_id="r", output_dir=tmp_path / "run")

    assert len(node.received) == 2


def test_a_task_declaring_an_out_of_pack_compose_file_fails_loud_before_any_trial(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path / "pack", manifest=True)
    orch, node, _ = _orchestrator(tmp_path / "pack", tmp_path / "out", orchestrator={})

    with pytest.raises(ValueError, match="outside its own directory"):
        orch.run(run_id="r", output_dir=tmp_path / "run")

    assert node.received == []


@pytest.mark.parametrize("grader_name", ["grader_rpc", "queue"])
def test_a_grader_needing_a_static_runner_address_fails_loud(
    tmp_path: Path, grader_name: str
) -> None:
    _write_pack(tmp_path / "pack")
    orch, node, _ = _orchestrator(
        tmp_path / "pack",
        tmp_path / "out",
        orchestrator={},
        grader=GraderConfig(name=grader_name),
    )

    with pytest.raises(ValueError, match="static runner address"):
        orch.run(run_id="r", output_dir=tmp_path / "run")

    assert node.received == []


def test_worker_mode_under_the_remote_runtime_fails_loud(tmp_path: Path) -> None:
    _write_pack(tmp_path / "pack")
    orch, node, _ = _orchestrator(tmp_path / "pack", tmp_path / "out", orchestrator={})

    with pytest.raises(ValueError, match="not supported in worker mode"):
        orch.run_worker(tmp_path / "out")

    assert node.received == []
