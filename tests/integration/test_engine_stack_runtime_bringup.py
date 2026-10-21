"""End-to-end: the library route brings up the engine stack for one trial.

``run_trial_with_stack`` starts the real runner (+ db-service) in a fresh
environment, points ``EXECUTOR_ADDRESS`` at it, runs one trial to a graded
result, and tears the stack down. This is the path a sandbox-per-trial backend
takes instead of copying the orchestrator's choice of services.

Linux/CI only: the bring-up builds and runs Docker containers, which a
Docker-in-Docker host (CI) supports but a macOS Docker Desktop host cannot drive
from inside a nested context. The ``requires_docker`` marker + the daemon
skipif keep it out of the unit/canonical lanes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.runner import plan_trials, run_trial_with_stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.requires_api,
]


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


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker daemon not available")
def test_run_trial_with_stack_grades_one_trial_on_a_real_runner(tmp_path: Path) -> None:
    task = _write_native_task(tmp_path / "pack", "demo")
    config = RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4o-mini"),
            "user": ModelConfig(provider="openai", name="gpt-4o-mini"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results")),
    )

    (inputs,) = plan_trials(config, tasks=[task], grader="runner_rpc")

    result = run_trial_with_stack(inputs, config)

    # The stack came up, the trial ran against the real runner, and it was
    # graded — the state_checks pass because the no-tool agent leaves the
    # notes list untouched.
    assert result.trajectory.grade is not None
    assert result.trajectory.grade.binary_pass is True
