"""Canonical lock for the three composable run steps.

An external orchestration layer drives a run as ``plan_trials`` →
``run_trial_from_inputs`` (per trial) → ``aggregate_results``, and reproduces
the four aggregate artifacts a single ``tolokaforge run`` writes. All offline:
the ``in_memory`` runtime backend and conductor, no Docker, no LLM.

Headline round-trip: the three-step pipeline's aggregates match what the
orchestrator's own ``_generate_reports`` writes over the same trajectories,
field for field, excluding only the disk-only non-reproducible stamps
(``tolokaforge_version``, ``captured_service_logs``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import (
    ComputeConfig,
    EvaluationConfig,
    Grade,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult
from tolokaforge.core.trial_node import run_trial_from_inputs
from tolokaforge.runner import aggregate_results, plan_trials
from tolokaforge.runner import run_trial_from_inputs as _rtfi

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_FIXED_TS = datetime(2020, 1, 1, tzinfo=UTC)

# aggregate.json fields the single-box run stamps on after the reproducible
# report is assembled; the library path does not reproduce them by design.
_NON_REPRODUCIBLE = ("tolokaforge_version", "captured_service_logs")


def _write_native_task(base_dir: Path, task_id: str) -> TaskConfig:
    task_dir = base_dir / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": task_id,
            "name": task_id,
            "category": "tool_use",
            "description": task_id,
            "metadata": {"complexity": "simple", "tags": ["domain:demo"]},
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
            "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
        },
    )
    return NativeAdapter({"base_dir": str(base_dir), "tasks_glob": "tasks/**/task.yaml"}).get_task(
        task_id
    )


def _run_config(output_dir: Path, repeats: int) -> RunConfig:
    return RunConfig(
        models={"agent": _AGENT, "user": _USER},
        orchestrator=OrchestratorConfig(workers=1, repeats=repeats, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )


def _library_results(config: RunConfig, task: TaskConfig) -> list[TrialResult]:
    """Drive the three-step library pipeline's first two steps to trial results."""
    planned = plan_trials(
        config,
        tasks=[task],
        runtime="in_memory",
        conductor="in_memory",
        grader="runner_rpc",
    )
    return [run_trial_from_inputs(inputs) for inputs in planned]


def _load_json_files(run_dir: Path) -> dict[str, Any]:
    return {
        name: json.loads((run_dir / name).read_text())
        for name in (
            "aggregate.json",
            "per_task_metrics.json",
            "metadata_slices.json",
            "failure_attribution.json",
        )
    }


def _trial_key(trial_id: str) -> tuple[str, int]:
    """Split a planned ``trial_id`` (``"<task_id>:<trial_index>"``) into its key."""
    task_id, trial_index = trial_id.rsplit(":", 1)
    return task_id, int(trial_index)


def test_three_steps_reproduce_the_run_report(tmp_path: Path) -> None:
    """plan → run → aggregate matches the orchestrator's own report assembly."""
    task = _write_native_task(tmp_path / "pack", "demo")
    config = _run_config(tmp_path / "results", repeats=3)

    # Steps 1 + 2 (library): plan the trials and run each to a graded result.
    results = _library_results(config, task)
    assert len(results) == 3
    assert all(r.trajectory.grade is not None for r in results)

    # Dir A — the orchestrator's own report assembly over the same trajectories.
    dir_a = tmp_path / "A"
    dir_a.mkdir()
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(runtime_backend=InMemoryRuntimeBackend()),
    )
    orch.tasks = [task]
    orch.results = [r.trajectory for r in results]
    orch._generate_reports(dir_a)

    # Planning parity: the (task_id, trial_index) keys plan_trials expands must
    # equal the orchestrator's own pending-trial build for this config — the
    # library planning step enqueues exactly the trial set a full run would,
    # not merely a set the two report-builders happen to agree on.
    planned = plan_trials(
        config, tasks=[task], runtime="in_memory", conductor="in_memory", grader="runner_rpc"
    )
    planned_keys = [_trial_key(inp.trial_spec.trial_id) for inp in planned]
    orchestrator_keys = orch._build_pending_trials([task], config.orchestrator.repeats)
    assert planned_keys == orchestrator_keys == [("demo", 0), ("demo", 1), ("demo", 2)]

    # Dir B — step 3 of the library pipeline over the same results.
    dir_b = tmp_path / "B"
    dir_b.mkdir()
    aggregate_results(results, run_config=config, tasks=[task], output_dir=dir_b)

    files_a = _load_json_files(dir_a)
    files_b = _load_json_files(dir_b)

    # aggregate.json matches once the disk-only stamps are dropped from both.
    agg_a = {k: v for k, v in files_a["aggregate.json"].items() if k not in _NON_REPRODUCIBLE}
    agg_b = {k: v for k, v in files_b["aggregate.json"].items() if k not in _NON_REPRODUCIBLE}
    assert agg_a == agg_b
    assert agg_b["total_trials"] == 3
    assert agg_b["schema_version"] == agg_a["schema_version"]

    # The other three artifacts are reproducible in full.
    assert files_a["per_task_metrics.json"] == files_b["per_task_metrics.json"]
    assert files_a["metadata_slices.json"] == files_b["metadata_slices.json"]
    assert files_a["failure_attribution.json"] == files_b["failure_attribution.json"]

    # Metadata threaded from the task config drives the slices.
    (per_task,) = files_b["per_task_metrics.json"]
    assert per_task["benchmark_type"] == "tool_use"
    assert per_task["complexity"] == "simple"
    assert per_task["tags"] == ["domain:demo"]
    assert "tool_use" in files_b["metadata_slices.json"]["by_benchmark_type"]


def _fabricated_result(task_id: str, *, passed: bool) -> TrialResult:
    """A graded single-trial result with ``trial_index=0`` and no live execution."""
    trajectory = Trajectory(
        task_id=task_id,
        trial_index=0,
        start_ts=_FIXED_TS,
        end_ts=_FIXED_TS,
        status=TrialStatus.COMPLETED,
        messages=[],
        grade=Grade(binary_pass=passed, score=1.0 if passed else 0.0, reasons="fabricated"),
    )
    return TrialResult.from_trajectory(trial_id=f"{task_id}:0", trajectory=trajectory)


def test_renumbers_independent_single_trial_results(tmp_path: Path) -> None:
    """N results for one task, all trial_index=0, count as repeats=N (not one)."""
    results = [_fabricated_result("task_a", passed=True) for _ in range(4)]

    report = aggregate_results(results, output_dir=tmp_path / "out")

    (per_task,) = report.per_task_metrics
    assert per_task["task_id"] == "task_a"
    assert per_task["total_trials"] == 4
    assert per_task["measured_trials"] == 4
    assert per_task["successful_trials"] == 4
    assert per_task["success_rate"] == 1.0
    assert per_task["pass@1"] == 1.0
    assert report.aggregate["total_trials"] == 4
    assert report.aggregate["total_tasks"] == 1


def test_aggregate_folds_trajectories_and_trial_results_identically(tmp_path: Path) -> None:
    """A raw Trajectory and the TrialResult wrapping it fold to the same report."""
    results = [_fabricated_result("t", passed=True), _fabricated_result("t", passed=False)]
    trajectories = [r.trajectory for r in results]

    from_results = aggregate_results(results)
    from_trajectories = aggregate_results(trajectories)

    assert from_results.per_task_metrics == from_trajectories.per_task_metrics
    assert from_results.aggregate == from_trajectories.aggregate


def test_plan_trials_carries_harness_shape_as_data(tmp_path: Path) -> None:
    """A planned native trial carries adapter_type and the agent harness as data."""
    task = _write_native_task(tmp_path / "pack", "demo")
    config = _run_config(tmp_path / "results", repeats=2)

    planned = plan_trials(config, tasks=[task], runtime="in_memory", conductor="in_memory")

    assert len(planned) == 2
    first = planned[0]
    # Adapter identity and the agent model (with its harness slot) travel as
    # plain data on the spec — no adapter or conductor object is resolved here.
    assert first.trial_spec.task.adapter_type == "native"
    assert first.conductor == "in_memory"
    assert first.runtime == "in_memory"
    assert first.trial_spec.agent_model_config.harness == config.models["agent"].harness
    assert [inp.trial_spec.trial_id for inp in planned] == ["demo:0", "demo:1"]


def test_plan_trials_stamps_the_configs_grpc_message_bound(tmp_path: Path) -> None:
    """A non-default compute.grpc_max_message_mb reaches every planned trial."""
    task = _write_native_task(tmp_path / "pack", "demo")
    config = RunConfig(
        models={"agent": _AGENT, "user": _USER},
        orchestrator=OrchestratorConfig(workers=1, repeats=2, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results")),
        compute=ComputeConfig(grpc_max_message_mb=123),
    )

    planned = plan_trials(config, tasks=[task], runtime="in_memory", conductor="in_memory")

    assert planned
    assert all(inp.grpc_max_message_mb == 123 for inp in planned)


def test_delegated_task_folds_like_native(
    test_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegated (terminal_bench) result aggregates through the same surface."""
    import tempfile

    from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    adapter = TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(test_data_dir / "terminal_bench_tasks"),
            "staging_root": str(tmp_path / "staging"),
        }
    )
    task = adapter.get_task("echo-hello")
    assert task.adapter_type == "terminal_bench"

    config = _run_config(tmp_path / "results", repeats=1)

    # A planned delegated trial carries the delegating adapter as data.
    planned = plan_trials(config, tasks=[task], runtime="in_memory", conductor="in_memory")
    assert planned[0].trial_spec.task.adapter_type == "terminal_bench"

    results = [run_trial_from_inputs(inputs) for inputs in planned]
    report = aggregate_results(results, tasks=[task], output_dir=tmp_path / "out")

    (per_task,) = report.per_task_metrics
    assert per_task["task_id"] == "echo-hello"
    assert per_task["total_trials"] == 1
    assert report.aggregate["total_tasks"] == 1


def test_public_api_surface_imports() -> None:
    """The three steps and their types import from the public runner surface."""
    from tolokaforge.core.trial import TrialNodeInputs
    from tolokaforge.core.trial import TrialResult as TR
    from tolokaforge.runner import (
        RunReport,
    )
    from tolokaforge.runner import (
        aggregate_results as agg,
    )
    from tolokaforge.runner import (
        plan_trials as plan,
    )
    from tolokaforge.runner import (
        run_trial_from_inputs as run,
    )

    assert callable(plan)
    assert callable(run)
    assert callable(agg)
    assert run is _rtfi
    assert TrialNodeInputs is not None
    assert TR is not None
    assert RunReport is not None
