"""External-harness capstone: the four runtime-independence surfaces together.

A single downstream plug-in package (``tests/fixtures/tolokaforge_plugin_fixture``,
installed only into an isolated ``--target`` — never the dev venv) registers a
runtime backend, a trial grader, and a conductor. Both tolokaforge entries are
driven over that package, in fresh subprocesses, Docker-free and keyless:

* ``test_run_trial_over_downstream_plugins`` — the library entry
  ``tolokaforge.run_trial`` resolves all three fixture seams from installed
  metadata and returns the fixture conductor's synthetic trajectory carrying the
  fixture grader's sentinel grade; its trajectory is byte-equal (no mask) to what
  an ``Orchestrator`` wired to the same seams produces.
* ``test_run_one_over_downstream_plugins`` — the ``tolokaforge run-one``
  subprocess over the ADR wire produces a ``result`` that equals the library
  result and is byte-equal to a checked-in golden transcript. The fixture
  trajectory carries no volatile field (``messages=[]``, pinned
  ``start_ts``/``end_ts``, default ``Metrics``), so the golden needs no mask.

Load-bearing matrix (issue #540 AC): remove #536 (entry-point registries) → both
tests fail at seam resolution; remove #537 (``tolokaforge.run_trial``) →
``test_run_trial_over_downstream_plugins`` fails; remove #538 (the ``run-one``
subcommand) → ``test_run_one_over_downstream_plugins`` fails.

Regenerate the golden after an intentional wire/fixture change:
``TOLOKAFORGE_REGENERATE_GOLDEN=1 scripts/with_env.sh uv run pytest \
tests/integration/test_external_harness_e2e.py::test_run_one_over_downstream_plugins``.
A diff across two regenerations means a volatile field leaked — pin it in the
fixture, do not add a mask.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.trial import TrialResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available"),
]

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "tolokaforge_plugin_fixture"
_GOLDEN = Path(__file__).parent.parent / "data" / "run_one_capstone_golden.jsonl"
_RUN_ONE_CMD = [sys.executable, "-m", "tolokaforge.cli.main", "run-one"]
_AGENT_MODEL = {"provider": "openai", "name": "gpt-4"}
_TASK_ID = "capstone"

# Runs run_trial over the three fixture seams, builds an Orchestrator baseline
# wired to the same seams, and writes both serialisations to ``out_file``.
# The baseline injects the fixture grader through the conductor_factory closure:
# OrchestratorDeps has no grader seam and _build_conductor hardcodes
# load_trial_grader("runner_rpc") into ctx.trial_grader (which would fire a real
# gRPC), so the closure ignores ctx.trial_grader and passes FixtureGrader
# explicitly — both paths then run FixtureConductor + FixtureGrader, Docker-free.
# The payload is written to a file because run_trial's StructuredLogger writes to
# stdout, which must not be parsed as the payload.
_RUN_TRIAL_PROBE = """
import json
import sys
from unittest.mock import MagicMock
from pathlib import Path

from tolokaforge import run_trial
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import EvaluationConfig, ModelConfig, OrchestratorConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.trial import TrialResult
from tolokaforge_plugin_fixture import FixtureConductor, FixtureGrader, FixtureRuntimeBackend

base_dir, out_file, orch_out = sys.argv[1], sys.argv[2], sys.argv[3]
agent = {"provider": "openai", "name": "gpt-4"}

adapter = NativeAdapter({"base_dir": base_dir, "tasks_glob": "tasks/**/task.yaml"})
task = adapter.get_task("capstone")

result = run_trial(
    task=task,
    models={"agent": agent},
    runtime="fixture_backend",
    grader="fixture_grader",
    conductor="fixture_conductor",
    output_dir=None,
)
assert isinstance(result, TrialResult), type(result)

task_desc = adapter.to_task_description("capstone")
config = RunConfig(
    models={"agent": ModelConfig(**agent)},
    orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
    evaluation=EvaluationConfig(output_dir=orch_out),
)
orch = Orchestrator(
    config,
    deps=OrchestratorDeps(
        runtime_backend=FixtureRuntimeBackend(),
        conductor_factory=lambda _ctx: FixtureConductor(grader=FixtureGrader()),
    ),
)
orch.tasks = [task]
adapter_stub = MagicMock()
adapter_stub.to_task_description.side_effect = lambda _tid: task_desc
adapter_stub.docker_stack_requirements.return_value = None
orch.adapter = adapter_stub
orch.run()
(orch_trajectory,) = orch.results

Path(out_file).write_text(
    json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "orch_trajectory": orch_trajectory.model_dump(mode="json"),
        }
    )
)
"""


@pytest.fixture(scope="module")
def isolated_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Install the fixture package into an isolated target and expose it on
    ``PYTHONPATH`` for child processes. Installed once, shared by both tests."""
    target = tmp_path_factory.mktemp("site")
    install = subprocess.run(
        ["uv", "pip", "install", "--target", str(target), str(_FIXTURE_DIR)],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, f"fixture install failed:\n{install.stderr}"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(target), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


@pytest.fixture(scope="module")
def flat_pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A flat-layout, MCP-free pack the adapter can glob. Returns the base dir."""
    base = tmp_path_factory.mktemp("pack")
    task_dir = base / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": _TASK_ID,
            "name": _TASK_ID,
            "category": "tool_use",
            "description": _TASK_ID,
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
            }
        },
    )
    return base


@pytest.fixture(scope="module")
def run_trial_payload(
    isolated_env: dict[str, str],
    flat_pack: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    """Run the library probe once; return ``{"result", "orch_trajectory"}``."""
    out = tmp_path_factory.mktemp("rt") / "payload.json"
    orch_out = tmp_path_factory.mktemp("orch")
    proc = subprocess.run(
        [sys.executable, "-c", _RUN_TRIAL_PROBE, str(flat_pack), str(out), str(orch_out)],
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"run_trial probe failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    return json.loads(out.read_text())


def test_run_trial_over_downstream_plugins(run_trial_payload: dict) -> None:
    result = run_trial_payload["result"]
    TrialResult.model_validate(result)

    trajectory = result["trajectory"]
    # Byte-equal, no mask: the fixture trajectory is fully pinned, so run_trial's
    # registry-resolved seams and the orchestrator's injected seams must agree.
    assert trajectory == run_trial_payload["orch_trajectory"]

    grade = trajectory["grade"]
    assert grade["score"] == 0.42
    assert "fixture-grader sentinel" in grade["reasons"]

    assert result["trial_id"] == f"{_TASK_ID}:0"
    assert trajectory["messages"] == []
    assert trajectory["status"] == "completed"
    assert trajectory["start_ts"] == trajectory["end_ts"]


def test_run_one_over_downstream_plugins(
    isolated_env: dict[str, str],
    flat_pack: Path,
    run_trial_payload: dict,
) -> None:
    task = NativeAdapter({"base_dir": str(flat_pack), "tasks_glob": "tasks/**/task.yaml"}).get_task(
        _TASK_ID
    )
    task_dir = flat_pack / "tasks" / "flat"

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": {"agent": _AGENT_MODEL},
        "runtime": "fixture_backend",
        "grader": "fixture_grader",
        "conductor": "fixture_conductor",
    }
    # A wire task carries no source_dir, so assets resolve against cwd — spawn at
    # the task-pack root, matching the ADR wire contract.
    proc = subprocess.run(
        _RUN_ONE_CMD,
        input=json.dumps(start) + "\n",
        cwd=str(task_dir),
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"run-one subprocess failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one stdout line, got: {proc.stdout!r}"
    wire_line = lines[0]

    envelope = json.loads(wire_line)
    assert envelope["type"] == "result", envelope
    assert envelope["result"] == run_trial_payload["result"]

    if os.environ.get("TOLOKAFORGE_REGENERATE_GOLDEN") == "1":
        assert not os.environ.get("CI"), (
            "TOLOKAFORGE_REGENERATE_GOLDEN=1 is set in a CI environment. "
            "Regeneration is a dev-only tool; the frozen golden lock must not be silently rewritten. "
            "Set the env var only when regenerating locally."
        )
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(wire_line + "\n")
        return

    assert _GOLDEN.exists(), (
        f"golden missing: {_GOLDEN}. Regenerate with "
        "TOLOKAFORGE_REGENERATE_GOLDEN=1 (see module docstring)."
    )
    assert wire_line == _GOLDEN.read_text().strip()
