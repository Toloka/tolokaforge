"""One-shot ``tolokaforge run-trial --inputs`` over a serialized ``TrialNodeInputs``.

A native, Docker-free, LLM-free trial (``in_memory`` runtime + conductor) is
serialized to a file, run through the one-shot entry, and asserted end to end:
exit ``0`` with ``trial_result.json`` written on success, the deterministic exit
code on a don't-retry failure, and the transient exit code on an unexpected one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_env_endpoints, write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import GraderConfig, ModelConfig, TaskConfig
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_node import build_single_task_adapter, compute_task_pack_hash
from tolokaforge.dx.cli.run_trial_command import (
    ONESHOT_EXIT_DETERMINISTIC,
    ONESHOT_EXIT_TRANSIENT,
    TRIAL_RESULT_FILENAME,
    _run_oneshot,
)

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


@pytest.fixture
def native_task(tmp_path: Path) -> TaskConfig:
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
            "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}).get_task(
        "flat"
    )


def _inputs_for(task: TaskConfig, *, runtime: str = "in_memory") -> TrialNodeInputs:
    pack_dir = task.source_dir
    assert pack_dir is not None
    task_desc = build_single_task_adapter(task).to_task_description(task.task_id)
    spec = TrialSpec(
        trial_id=f"{task.task_id}:0",
        run_id="node-run",
        task=task_desc,
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )
    inputs = TrialNodeInputs(
        task=task,
        trial_spec=spec,
        grader_config=GraderConfig(name="runner_rpc"),
        grpc_max_message_mb=50,
        runtime=runtime,
        conductor="in_memory",
        task_pack_dir=pack_dir,
        task_pack_hash=compute_task_pack_hash(pack_dir),
    )
    return TrialNodeInputs.model_validate_json(inputs.model_dump_json())


def _write_inputs(task: TaskConfig, tmp_path: Path, *, runtime: str = "in_memory") -> Path:
    inputs_path = tmp_path / "trial.json"
    inputs_path.write_text(_inputs_for(task, runtime=runtime).model_dump_json(), encoding="utf-8")
    return inputs_path


def test_inputs_round_trip_runs_and_writes_the_result(
    native_task: TaskConfig, tmp_path: Path
) -> None:
    inputs_path = _write_inputs(native_task, tmp_path)
    output_dir = tmp_path / "out"

    code = _run_oneshot(inputs_path, task_pack_root=None, output_dir=output_dir)

    assert code == 0
    result_file = output_dir / TRIAL_RESULT_FILENAME
    assert result_file.exists()
    result = TrialResult.model_validate_json(result_file.read_text(encoding="utf-8"))
    assert result.trial_id == "flat:0"
    assert result.trajectory.grade is not None


def test_output_dir_flag_overrides_the_inputs_placement(
    native_task: TaskConfig, tmp_path: Path
) -> None:
    inputs_path = _write_inputs(native_task, tmp_path)
    flagged = tmp_path / "flagged-out"

    assert _run_oneshot(inputs_path, task_pack_root=None, output_dir=flagged) == 0
    assert (flagged / TRIAL_RESULT_FILENAME).exists()


def test_unknown_runtime_is_a_deterministic_failure(
    native_task: TaskConfig, tmp_path: Path
) -> None:
    inputs_path = _write_inputs(native_task, tmp_path, runtime="bogus")
    output_dir = tmp_path / "out"

    code = _run_oneshot(inputs_path, task_pack_root=None, output_dir=output_dir)

    assert code == ONESHOT_EXIT_DETERMINISTIC
    assert not (output_dir / TRIAL_RESULT_FILENAME).exists()


def test_unexpected_internal_fault_is_a_transient_failure(
    native_task: TaskConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs_path = _write_inputs(native_task, tmp_path)

    def _boom(_inputs: TrialNodeInputs) -> TrialResult:
        raise RuntimeError("something unexpected deep in the trial")

    # The one-shot entry imports the symbol at call time, so the patch is seen.
    monkeypatch.setattr("tolokaforge.core.trial_node.run_trial_from_inputs", _boom)

    code = _run_oneshot(inputs_path, task_pack_root=None, output_dir=tmp_path / "out")

    assert code == ONESHOT_EXIT_TRANSIENT
