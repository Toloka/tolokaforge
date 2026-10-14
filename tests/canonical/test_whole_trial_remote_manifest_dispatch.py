"""A task-declared single-stack manifest runs on the whole-trial node.

The control plane serialises the manifest with compose paths that are absolute
on its own filesystem; the node, which mounts the same task pack elsewhere,
must rebase them before the manifest validates and materialise the stack from
its own copy. A registered test runtime builds the shared-stack backend over a
stub composer, so the node's real ``run_trial_from_inputs`` path runs without
Docker and the composer records what the node asked it to materialise.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import warnings
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints
from tests.canonical._fakes import RecordingRunnerClient
from tests.canonical._node_fakes import InProcessNode, InProcessNodeClientFactory
from tolokaforge.core.composition_runtime import ComposedEnvHandle, RunCtx, RunSubstrate, StackDecl
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TrialStatus,
)
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.plugin_registry import (
    RUNTIME_BACKENDS_GROUP,
    RuntimeBackendBuildContext,
    _clear_discovery_cache,
)
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_node import _resolve_runtime_name, run_trial_from_inputs

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_EXAMPLE = Path(__file__).parents[2] / "examples" / "native" / "multi_service" / "dataset"
_TASK_ID = "multi_service_example_01"
_NODES = ["node-a:50061"]
_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_STUB_RUNTIME = "stub-composer-shared"


class _StubComposer:
    """Records what the node asks the composer to materialise; starts nothing."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.torn_down = 0

    def materialise_run(self, plan: list[StackDecl], ctx: RunCtx) -> RunSubstrate:
        self.runs.append(
            {
                "plan": plan,
                "compose_files_exist": [stack.compose_file.is_file() for stack in plan],
                "manifest_compose_file": ctx.manifest.compose_file,
            }
        )
        return RunSubstrate(
            run_id=ctx.run_id,
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=RecordingRunnerClient("stub-runner:50051", ctx.grpc_max_message_mb),
            endpoints=make_env_endpoints(),
            seeds=ctx.seeds,
            mount_docker_socket=ctx.mount_docker_socket,
            expose_substrate=ctx.expose_substrate,
            log_capture=ctx.log_capture,
            events=ctx.events,
            grpc_max_message_mb=ctx.grpc_max_message_mb,
        )

    def provision_trial(
        self, plan: list[StackDecl], spec: TrialSpec, run_sub: RunSubstrate
    ) -> ComposedEnvHandle:
        return ComposedEnvHandle(
            trial_id=spec.trial_id,
            trial_stack_handles=(),
            trial_endpoints=None,
            trial_runner_client=None,
        )

    def endpoints_for(self, run_sub: RunSubstrate, env_handle: ComposedEnvHandle) -> EnvEndpoints:
        assert run_sub.endpoints is not None
        return run_sub.endpoints

    def teardown_trial(self, env_handle: ComposedEnvHandle) -> None:
        pass

    def teardown_run(self, run_sub: RunSubstrate) -> None:
        self.torn_down += 1


@pytest.fixture
def composer(monkeypatch: pytest.MonkeyPatch) -> _StubComposer:
    """Register ``stub-composer-shared``: the shared-stack backend over a stub composer."""
    stub = _StubComposer()

    def factory(ctx: RuntimeBackendBuildContext) -> SharedStackRuntimeBackend:
        return SharedStackRuntimeBackend(
            env_manifest=ctx.env_manifest, run_id=ctx.run_id, seeds=ctx.seeds, composer=stub
        )

    class _EntryPoint:
        name = _STUB_RUNTIME

        class dist:  # noqa: N801 — mimics ``EntryPoint.dist``
            name = "tests-fixture"

        @staticmethod
        def load() -> Any:
            return factory

    real = importlib.metadata.entry_points

    def entry_points(*, group: str) -> list[Any]:
        found = list(real(group=group))
        return [*found, _EntryPoint] if group == RUNTIME_BACKENDS_GROUP else found

    _clear_discovery_cache()
    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    _clear_discovery_cache()
    yield stub
    _clear_discovery_cache()


def _run_on_stub_runtime(inputs: TrialNodeInputs) -> TrialResult:
    return run_trial_from_inputs(
        inputs.model_copy(update={"runtime": _STUB_RUNTIME, "conductor": "in_memory"})
    )


def _remote_orchestrator(
    control_dataset: Path, out: Path, node: InProcessNode
) -> tuple[Orchestrator, InProcessNodeClientFactory]:
    factory = InProcessNodeClientFactory(node)
    backend = RemoteRuntimeBackend(
        registry=InMemoryNodeRegistry(_NODES, lease_timeout_s=5),
        grpc_max_message_mb=9,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        node_client_factory=factory,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models={"agent": _AGENT, "user": _USER, "judge": _USER},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                shuffle_trials=False,
                runtime="remote",
                remote={"nodes": _NODES, "lease_timeout_s": 5},
                auto_start_services=False,
            ),
            evaluation=EvaluationConfig(output_dir=str(out), projects=[str(control_dataset)]),
            compute={"grpc_max_message_mb": 9},
        )
    return Orchestrator(config, deps=OrchestratorDeps(runtime_backend=backend)), factory


@pytest.fixture
def packs(tmp_path: Path) -> tuple[Path, Path]:
    """The example project on the control plane, and the same task pack mounted on the node."""
    control = tmp_path / "control" / "dataset"
    shutil.copytree(_EXAMPLE, control)
    task_dir = next(control.rglob(_TASK_ID))
    node_root = tmp_path / "node-packs"
    shutil.copytree(task_dir, node_root / _TASK_ID)
    return control, node_root


def test_auto_resolves_an_all_shared_single_stack_manifest_to_the_shared_backend(
    packs: tuple[Path, Path], tmp_path: Path
) -> None:
    control, node_root = packs
    node = InProcessNode(tmp_path / "node-out", task_pack_root=node_root)
    orch, _ = _remote_orchestrator(control, tmp_path / "out", node)
    orch.load_tasks()
    description = orch._task_description(_TASK_ID)
    assert description.environment_manifest is not None

    spec = TrialSpec(
        trial_id=f"{_TASK_ID}:0",
        run_id="r",
        task=description,
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )

    assert _resolve_runtime_name("auto", spec) == "shared"


def test_the_node_materialises_the_single_stack_from_its_own_pack_copy(
    packs: tuple[Path, Path], tmp_path: Path, composer: _StubComposer
) -> None:
    control, node_root = packs
    node = InProcessNode(
        tmp_path / "node-out", run_trial=_run_on_stub_runtime, task_pack_root=node_root
    )
    orch, factory = _remote_orchestrator(control, tmp_path / "out", node)

    orch.run(run_id="remote-run", output_dir=tmp_path / "run")

    assert [t.status for t in orch.results] == [TrialStatus.COMPLETED]
    (inputs,) = node.received
    node_compose = node_root / _TASK_ID / "environment.compose.yaml"
    assert inputs.trial_spec.task.environment_manifest.compose_file == node_compose
    assert inputs.task.task_id == _TASK_ID
    (run,) = composer.runs
    assert [stack.compose_file for stack in run["plan"]] == [node_compose]
    assert run["manifest_compose_file"] == node_compose
    assert run["compose_files_exist"] == [True]
    assert composer.torn_down == 1
    (client,) = factory.clients
    assert str(control) in client.requests[0].trial_node_inputs_json
    assert str(node_root) not in client.requests[0].trial_node_inputs_json


def test_a_node_without_a_pack_root_leaves_the_control_plane_paths_untouched(
    packs: tuple[Path, Path], tmp_path: Path
) -> None:
    control, _ = packs
    node = InProcessNode(tmp_path / "node-out")
    orch, _ = _remote_orchestrator(control, tmp_path / "out", node)

    orch.run(run_id="remote-run", output_dir=tmp_path / "run")

    (inputs,) = node.received
    manifest = inputs.trial_spec.task.environment_manifest
    assert manifest is not None
    assert str(manifest.compose_file).startswith(str(control.resolve()))
