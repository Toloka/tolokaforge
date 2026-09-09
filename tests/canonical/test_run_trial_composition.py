"""Composition-equivalence lock for ``tolokaforge.runner.run_trial``.

Over a fixture task pack, ``run_trial`` wired to the ``in_memory`` runtime
backend + ``in_memory`` conductor must return a :class:`TrialResult` whose
trajectory + grade match what the :class:`Orchestrator` yields for the same
task wired to the same seams — proving ``run_trial`` threads the
registry-resolved seams and returns the seam's result without diverging from
the orchestrator's path.

Canonical tier: resolution goes through installed entry-point metadata for the
``in_memory`` names. ``InMemoryConductor`` returns a deterministic synthetic
trajectory and never touches the RPC surface, so this runs without Docker or an
LLM.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    Trajectory,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult
from tolokaforge.runner import run_trial

pytestmark = pytest.mark.canonical

_AGENT = {"provider": "openai", "name": "gpt-4"}


@pytest.fixture
def flat_pack(tmp_path: Path) -> Path:
    """A flat-layout, MCP-free pack. Returns the base dir the adapter globs."""
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "grading": "grading.yaml",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            },
            # The weight above needs the section it names: a pack weighting a component
            # it never configures cannot be graded as written, and the pre-run gate
            # refuses it before either path here builds a trajectory.
            "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
        },
    )
    return tmp_path


def _canonical(trajectory: Trajectory) -> dict:
    # start_ts / end_ts are per-run wall-clock; the plan excludes timestamps.
    return trajectory.model_dump(exclude={"start_ts", "end_ts"})


def _orchestrator_trajectory(base_dir: Path, task, output_dir: Path) -> Trajectory:
    adapter = NativeAdapter({"base_dir": str(base_dir), "tasks_glob": "tasks/**/task.yaml"})
    task_desc = adapter.to_task_description(task.task_id)

    config = RunConfig(
        models={
            "agent": ModelConfig(**_AGENT),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )
    orch.tasks = [task]
    adapter_stub = MagicMock()
    adapter_stub.to_task_description.side_effect = lambda _tid: task_desc
    adapter_stub.docker_stack_requirements.return_value = None
    adapter_stub.trial_grader_name = "runner_rpc"
    # The pre-run gate reads the pack off the adapter — its directory, and the layers
    # the real adapter answers for its own tasks. Left auto-mocked, the gate resolves a
    # mock as a path and reads whatever that opens.
    adapter_stub.get_task_dir.side_effect = adapter.get_task_dir
    adapter_stub.grading_hash_source_layer.side_effect = adapter.grading_hash_source_layer
    adapter_stub.grading_combine_layer.side_effect = adapter.grading_combine_layer
    adapter_stub.fingerprint.return_value = None
    orch.adapter = adapter_stub
    orch.run()
    (trajectory,) = orch.results
    return trajectory


def test_run_trial_matches_orchestrator_composition(flat_pack: Path, tmp_path: Path) -> None:
    adapter = NativeAdapter({"base_dir": str(flat_pack), "tasks_glob": "tasks/**/task.yaml"})
    task = adapter.get_task("flat")

    result = run_trial(
        task=task,
        models={
            "agent": _AGENT,
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        runtime="in_memory",
        conductor="in_memory",
        output_dir=None,
    )
    assert isinstance(result, TrialResult)

    expected = _orchestrator_trajectory(flat_pack, task, tmp_path / "orch_results")

    assert _canonical(result.trajectory) == _canonical(expected)
    assert result.trajectory.grade == expected.grade
