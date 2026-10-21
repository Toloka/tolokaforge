"""Canonical lock: a caller job id scopes a trial's output, logs and trace.

Offline — the ``in_memory`` runtime backend and conductor with the
``runner_rpc`` grader, no Docker and no LLM. Pins:

- ``plan_trials`` leaves ``job_id`` unset — only the caller knows it;
- a job id nests the trial's on-disk bundle under ``<output_dir>/<job_id>`` and
  binds to the trial's logs, while the graded result is unchanged;
- with no job id the bundle lands at today's path and the result is identical,
  so the field is behaviour-preserving when absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tolokaforge.core.trial_node as trial_node
from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.trial_node import run_trial_from_inputs
from tolokaforge.runner import plan_trials

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


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


def _run_config(output_dir: Path) -> RunConfig:
    return RunConfig(
        models={"agent": _AGENT, "user": _USER},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )


def _plan_one(config: RunConfig, task: TaskConfig):
    (planned,) = plan_trials(
        config,
        tasks=[task],
        runtime="in_memory",
        conductor="in_memory",
        grader="runner_rpc",
    )
    return planned


def _capture_execution_output_dir(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | None]:
    """Record the output dir that reaches the trial-execution context.

    The ``in_memory`` conductor runs no trial and writes no bundle, so the
    on-disk layout is unobservable offline. The output dir the composition hands
    the conductor is where a writing conductor would root the bundle, so the
    recorded value is the effective output path for this trial.
    """
    captured: dict[str, Path | None] = {}
    real = trial_node.build_trial_execution_unit

    def spy(ctx: trial_node.TrialExecutionContext):
        captured["output_dir"] = ctx.output_dir
        return real(ctx)

    monkeypatch.setattr(trial_node, "build_trial_execution_unit", spy)
    return captured


def test_plan_trials_leaves_job_id_unset(tmp_path: Path) -> None:
    task = _write_native_task(tmp_path / "pack", "demo")
    planned = plan_trials(
        _run_config(tmp_path / "jobid_plan_results"),
        tasks=[task],
        runtime="in_memory",
        conductor="in_memory",
        grader="runner_rpc",
    )
    assert planned
    assert all(inputs.job_id is None for inputs in planned)


def test_job_id_scopes_the_output_path_and_binds_to_the_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _write_native_task(tmp_path / "pack", "demo")
    # A distinct run-id basename keeps this trial's (process-global) logger clear
    # of any other run's records.
    config = _run_config(tmp_path / "jobid_nest_results")
    run_dir = tmp_path / "out"
    job_id = "wf-run-7-demo-0"
    inputs = _plan_one(config, task).model_copy(update={"output_dir": run_dir, "job_id": job_id})
    captured = _capture_execution_output_dir(monkeypatch)

    result = run_trial_from_inputs(inputs)

    assert result.trajectory.grade is not None
    # The trial's whole output is scoped under the job id, not the bare run dir.
    assert captured["output_dir"] == run_dir / job_id
    # The trial's logs carry the job id as structured context.
    records = get_logger(inputs.trial_spec.run_id).get_logs()
    assert any(record.get("context", {}).get("job_id") == job_id for record in records)


def test_absent_job_id_keeps_todays_path_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _write_native_task(tmp_path / "pack", "demo")
    # Distinct run-id basenames so the two trials use separate process-global
    # loggers and the absent-id trial's log stream is examined in isolation.
    with_job = _plan_one(_run_config(tmp_path / "jobid_with_results"), task).model_copy(
        update={"output_dir": tmp_path / "with", "job_id": "job-x"}
    )
    without_job = _plan_one(_run_config(tmp_path / "jobid_without_results"), task).model_copy(
        update={"output_dir": tmp_path / "without"}
    )

    graded_with = run_trial_from_inputs(with_job)
    captured = _capture_execution_output_dir(monkeypatch)
    graded_without = run_trial_from_inputs(without_job)

    # Absent: the output path is today's un-nested run dir.
    assert captured["output_dir"] == tmp_path / "without"
    # Behaviour-preserving: the graded outcome is the same with or without the id.
    assert graded_without.trajectory.grade == graded_with.trajectory.grade
    assert graded_without.trajectory.status == graded_with.trajectory.status
    # No job-id binding record leaks onto an absent-id trial's logs.
    records = get_logger(without_job.trial_spec.run_id).get_logs()
    assert all("job_id" not in record.get("context", {}) for record in records)
