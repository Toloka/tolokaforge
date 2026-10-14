"""``TrialNodeInputs`` rebases every compose path onto the node's task-pack mount.

A manifest's compose paths are absolute on the control plane. Deserialising the
inputs on a node that mounts the pack elsewhere must rewrite each one, and only
when the node says where its packs live.
"""

from __future__ import annotations

import copy
import shutil
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.trial import TrialNodeInputs, TrialSpec

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_EXAMPLE = Path(__file__).parents[2] / "examples" / "native" / "multi_service" / "dataset"
_TASK_ID = "multi_service_example_01"
_MODEL = ModelConfig(provider="openai", name="gpt-4")


def _compose_file_paths(node: Any, where: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(json path, value)`` for every ``compose_file`` key in a JSON-shaped value."""
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{where}.{key}"
            if key == "compose_file":
                yield path, value
            yield from _compose_file_paths(value, path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _compose_file_paths(value, f"{where}[{index}]")


@pytest.fixture
def control_pack_dir(tmp_path: Path) -> Path:
    control = tmp_path / "control" / "dataset"
    shutil.copytree(_EXAMPLE, control)
    return next(control.rglob(_TASK_ID))


@pytest.fixture
def wire(control_pack_dir: Path, tmp_path: Path) -> dict[str, Any]:
    """The control plane's serialised inputs for the manifest task."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models={"agent": _MODEL, "user": _MODEL},
            orchestrator=OrchestratorConfig(
                workers=1, repeats=1, runtime="remote", remote={"nodes": ["node-a:50061"]}
            ),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "out"), projects=[str(control_pack_dir.parents[2])]
            ),
        )
    orch = Orchestrator(config)
    orch.load_tasks()
    (task,) = orch.tasks
    spec = TrialSpec(
        trial_id=f"{_TASK_ID}:0",
        run_id="r",
        task=orch._task_description(_TASK_ID),
        agent_model_config=_MODEL,
        user_model_config=_MODEL,
        env_endpoints=make_env_endpoints(),
    )
    inputs = TrialNodeInputs(
        task=task,
        trial_spec=spec,
        grpc_max_message_mb=9,
        runtime="auto",
        conductor="in_process",
        task_pack_dir=task.source_dir,
        task_pack_hash="pack-hash",
    )
    return inputs.model_dump(mode="json")


@pytest.fixture
def node_root(tmp_path: Path, control_pack_dir: Path) -> Path:
    root = tmp_path / "node-packs"
    shutil.copytree(control_pack_dir, root / _TASK_ID)
    return root


def _validate(wire: dict[str, Any], node_root: Path | None) -> TrialNodeInputs:
    context = {"task_pack_root": str(node_root)} if node_root is not None else None
    return TrialNodeInputs.model_validate(wire, context=context)


def test_a_control_plane_path_under_the_pack_becomes_the_node_path(
    wire: dict[str, Any], node_root: Path
) -> None:
    inputs = _validate(wire, node_root)

    manifest = inputs.trial_spec.task.environment_manifest
    assert manifest is not None
    node_compose = node_root / _TASK_ID / "environment.compose.yaml"
    assert manifest.compose_file == node_compose
    assert [stack.compose_file for stack in manifest.stacks] == [node_compose]


def test_every_compose_file_site_in_the_wire_is_rebased(
    wire: dict[str, Any], node_root: Path
) -> None:
    before = dict(_compose_file_paths(wire))
    after = dict(_compose_file_paths(_validate(wire, node_root).model_dump(mode="json")))

    assert set(before) == set(after)
    assert {".trial_spec.task.environment_manifest.compose_file"} <= set(before)
    assert any(path.endswith(".stacks[0].compose_file") for path in before)
    assert all(
        after[path].startswith(str(node_root)) and after[path] != before[path] for path in before
    )


def test_the_callers_data_is_not_mutated(wire: dict[str, Any], node_root: Path) -> None:
    snapshot = copy.deepcopy(wire)

    _validate(wire, node_root)

    assert wire == snapshot


def test_a_compose_file_outside_the_pack_is_refused(
    wire: dict[str, Any], node_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere" / "compose.yaml"
    wire["trial_spec"]["task"]["environment_manifest"]["compose_file"] = str(outside)

    with pytest.raises(ValueError, match="is outside the task pack"):
        _validate(wire, node_root)


def test_a_manifest_without_a_pack_hash_is_refused(wire: dict[str, Any], node_root: Path) -> None:
    wire["task_pack_dir"] = None
    wire["task_pack_hash"] = None

    with pytest.raises(ValueError, match="no task_pack_dir / task_pack_hash"):
        _validate(wire, node_root)


def test_without_a_pack_root_the_inputs_are_validated_as_sent(wire: dict[str, Any]) -> None:
    inputs = _validate(wire, None)

    manifest = inputs.trial_spec.task.environment_manifest
    assert manifest is not None
    assert (
        str(manifest.compose_file)
        == wire["trial_spec"]["task"]["environment_manifest"]["compose_file"]
    )
