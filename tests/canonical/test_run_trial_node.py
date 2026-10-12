"""Canonical lock for the standalone trial entry ``run_trial_from_inputs``.

One native and one delegated (terminal-bench) fixture run to a graded
:class:`TrialResult` from serialized :class:`TrialNodeInputs` — wired to the
``in_memory`` runtime backend and conductor, so no Docker or LLM is needed —
with no ``Orchestrator`` anywhere in the call graph. The import closure of the
entry is locked separately: loading it must not load the orchestrator module.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.canonical._factories import make_env_endpoints, write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import GraderConfig, ModelConfig, TaskConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_node import (
    build_single_task_adapter,
    compute_task_pack_hash,
    run_trial_from_inputs,
)

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


@pytest.fixture(autouse=True)
def _no_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_trial_from_inputs must not construct an Orchestrator")

    monkeypatch.setattr(Orchestrator, "__init__", refuse)


def _inputs_for(task: TaskConfig) -> TrialNodeInputs:
    """Serialize-and-reload inputs for *task*, as a node would receive them."""
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
        runtime="in_memory",
        conductor="in_memory",
        task_pack_dir=pack_dir,
        task_pack_hash=compute_task_pack_hash(pack_dir),
    )
    return TrialNodeInputs.model_validate_json(inputs.model_dump_json())


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


@pytest.fixture
def delegated_task(
    test_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TaskConfig:
    """A terminal-bench task: the adapter stages its environment under the temp dir."""
    from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    adapter = TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(test_data_dir / "terminal_bench_tasks"),
            "staging_root": str(tmp_path / "staging"),
        }
    )
    return adapter.get_task("echo-hello")


def test_native_task_runs_to_a_graded_result(native_task: TaskConfig) -> None:
    result = run_trial_from_inputs(_inputs_for(native_task))

    assert isinstance(result, TrialResult)
    assert result.trial_id == "flat:0"
    assert result.trajectory.grade is not None


def test_delegated_task_runs_to_a_graded_result(delegated_task: TaskConfig) -> None:
    assert delegated_task.adapter_type == "terminal_bench"

    result = run_trial_from_inputs(_inputs_for(delegated_task))

    assert isinstance(result, TrialResult)
    assert result.trial_id == "echo-hello:0"
    assert result.trajectory.grade is not None


def test_delegated_adapter_resolves_the_preloaded_task(delegated_task: TaskConfig) -> None:
    adapter = build_single_task_adapter(delegated_task)

    assert adapter.get_task_ids() == ["echo-hello"]
    assert adapter.get_task_dir("echo-hello") == delegated_task.source_dir


def test_delegated_adapter_refuses_a_directory_that_is_not_the_task(
    delegated_task: TaskConfig,
) -> None:
    adapter = build_single_task_adapter(delegated_task)

    with pytest.raises(ValueError, match="does not match the mounted task directory"):
        adapter.register_preloaded_task(delegated_task, Path("/not-the-task"))


@pytest.mark.parametrize(
    "entry",
    [
        "from tolokaforge.runner import run_trial_from_inputs",
        "import tolokaforge.core.trial_node",
        "from tolokaforge.runner import run_trial",
    ],
)
def test_entry_import_closure_excludes_the_orchestrator(entry: str) -> None:
    """Loading the standalone entry must not pull in the batch orchestrator."""
    program = (
        f"{entry}\n"
        "import sys\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('tolokaforge.core.orchestrator'))\n"
        "assert not loaded, loaded\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", program], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
