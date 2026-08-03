"""Real-user-scenario locks for the ``tolokaforge.core.models`` package split.

The models package is a re-export shim over per-concern submodules
(:mod:`.grade`, :mod:`.grade_components`, :mod:`.trajectory`,
:mod:`.model_config`, :mod:`.run_config`, :mod:`.task_config`). This
module locks the split against **the user paths that actually
instantiate the wire types**, not synthetic Pydantic round-trips:

- **Trial author** — loads ``project.yaml`` + a run config the same
  way ``tolokaforge run`` does, through
  :func:`~tolokaforge.core.project_loader.load_project_config` and
  :class:`~tolokaforge.core.models.RunConfig`.
- **CI operator** — invokes ``tolokaforge validate --tasks ...`` on a
  bundled example via ``CliRunner``; the exit code must be 0.
- **Output-bundle consumer** — reads a real golden trial's
  ``grade.yaml``, ``trajectory.yaml``, ``task.yaml``, and
  ``metrics.yaml`` via the split :class:`Grade`, :class:`Trajectory`,
  :class:`TaskConfig`, and :class:`Metrics` models and JSON-round-trips
  each.
- **JSON-Lines subprocess consumer** — parses
  ``tests/data/run_trial_capstone_golden.jsonl``'s ``result`` envelope
  (:class:`~tolokaforge.core.trial.TrialResult`) and re-serialises it
  bit-for-bit.

Each assertion below fails loud on any Pydantic drift (missing field,
new default, changed serialiser, changed extras policy) that could
survive the mechanical split.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    ProjectConfig,
    RunConfig,
    TaskConfig,
    Trajectory,
)
from tolokaforge.core.models import grade as grade_mod
from tolokaforge.core.models import grade_components as grade_components_mod
from tolokaforge.core.models import model_config as model_config_mod
from tolokaforge.core.models import run_config as run_config_mod
from tolokaforge.core.models import task_config as task_config_mod
from tolokaforge.core.models import trajectory as trajectory_mod
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.core.trial import TrialResult
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_PROJECT_ROOT = _REPO_ROOT / "examples" / "native" / "tool_use"
_EXAMPLE_RUN_CONFIG = _EXAMPLE_PROJECT_ROOT / "run_configs" / "dev.yaml"
_EXAMPLE_TASK_GLOB = str(_EXAMPLE_PROJECT_ROOT / "dataset" / "tasks" / "**" / "task.yaml")
_GOLDEN_TRIAL_ROOT = (
    _REPO_ROOT
    / "tests"
    / "data"
    / "projects"
    / "food_delivery_2"
    / "output"
    / "trials"
    / "051fa6cb-a29e-4a0d-9ccf-e0f95802eee5"
    / "0"
)
_RUN_TRIAL_JSONL = _REPO_ROOT / "tests" / "data" / "run_trial_capstone_golden.jsonl"


# ────────────────────────────────────────────────────────────────
# Persona 0 — the split respects the documented module partition
# ────────────────────────────────────────────────────────────────

_EXPECTED_MODULE_HOMES = {
    GradeComponents: grade_components_mod,
    Grade: grade_mod,
    Trajectory: trajectory_mod,
    Metrics: trajectory_mod,
    ModelConfig: model_config_mod,
    RunConfig: run_config_mod,
    TaskConfig: task_config_mod,
    ProjectConfig: task_config_mod,
}


@pytest.mark.parametrize("cls,expected_module", list(_EXPECTED_MODULE_HOMES.items()))
def test_class_lives_in_its_declared_submodule(cls, expected_module):
    """Each split class is defined in the submodule its concern owns.

    Guards against a future maintainer accidentally moving a class
    across submodules and breaking the ADR-0025 partition without a
    doc update. ``__module__`` is the source-of-truth marker Pydantic
    stamps at class creation.
    """
    assert cls.__module__ == expected_module.__name__


def test_shim_and_submodule_reexport_same_object():
    """Importing ``Grade`` from the shim vs the submodule must be the
    same object — the re-export must not re-declare."""
    from tolokaforge.core.models import Grade as ShimGrade
    from tolokaforge.core.models.grade import Grade as SubGrade

    assert ShimGrade is SubGrade


# ────────────────────────────────────────────────────────────────
# Persona 1 — Trial author: real project.yaml + run_config.yaml load
# ────────────────────────────────────────────────────────────────


def test_bundled_example_project_yaml_loads_via_split_models():
    """``tolokaforge run`` loads ``project.yaml`` via
    :func:`load_project_config`. The bundled ``tool_use`` example is
    non-trivial (task defaults + actors + task discovery + models
    map) and must parse cleanly through the split
    :class:`ProjectConfig` → :class:`TaskDefaults` → :class:`ActorSpec`
    chain.
    """
    project = load_project_config(_EXAMPLE_PROJECT_ROOT / "project.yaml")

    assert isinstance(project, ProjectConfig)
    # The split ProjectConfig's task_defaults must resolve to the split
    # TaskDefaults instance (not a stray legacy class).
    assert project.task_defaults.__class__.__module__ == task_config_mod.__name__
    assert project.name == "tool-use"

    # JSON round-trip — the wire-lock. If the split dropped a field
    # (or changed a serialiser), the re-parsed model would either fail
    # or differ from the original.
    dumped = project.model_dump(mode="json")
    reconstructed = ProjectConfig(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


def test_bundled_example_run_config_yaml_loads_via_split_models():
    """``tolokaforge run --config dev.yaml`` parses the same run_config
    the CLI would. The split :class:`RunConfig` must accept the file
    unchanged and its nested :class:`ModelConfig` / :class:`OrchestratorConfig`
    /  :class:`EvaluationConfig` chain must round-trip.

    Round-trip uses ``exclude_defaults=True``: the run config's
    parse-time alias lift (``orchestrator.workers`` → ``compute.workers``)
    would otherwise collide with default-filled fields on re-parse —
    a pre-existing property of the alias-lift path, unrelated to this
    split. ``exclude_defaults`` is what the output writer emits.
    """
    raw = yaml.safe_load(_EXAMPLE_RUN_CONFIG.read_text())
    run_config = RunConfig(**raw)

    assert isinstance(run_config, RunConfig)
    assert set(run_config.models.keys()) == {"agent", "user"}
    for role, model in run_config.models.items():
        assert isinstance(model, ModelConfig), role
        assert model.__class__.__module__ == model_config_mod.__name__

    dumped = run_config.model_dump(mode="json", exclude_defaults=True)
    reconstructed = RunConfig(**dumped)
    assert reconstructed.model_dump(mode="json", exclude_defaults=True) == dumped


def test_bundled_example_task_loads_and_round_trips():
    """A trial-author's task.yaml load path is
    :func:`adapters._task_loader.load_task_yaml` → :class:`TaskConfig`.
    The split must round-trip every task field including nested
    :class:`InitialStateConfig`, :class:`ToolsConfig`, :class:`ActorSpec`.
    """
    task_yaml = (
        _EXAMPLE_PROJECT_ROOT
        / "dataset"
        / "tasks"
        / "tool_use"
        / "tool_use_public_example_01"
        / "task.yaml"
    )
    task, task_dir = load_task_yaml(task_yaml)
    assert isinstance(task, TaskConfig)
    assert task.source_dir == task_dir

    # Two round-trips: model_dump(mode="json") must be idempotent
    # under reconstruction — the shape the output writer emits.
    dumped = task.model_dump(mode="json")
    reconstructed = TaskConfig(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


# ────────────────────────────────────────────────────────────────
# Persona 2 — CI operator: ``tolokaforge validate`` on the pack
# ────────────────────────────────────────────────────────────────


def test_tolokaforge_validate_command_accepts_bundled_example():
    """``tolokaforge validate --tasks 'examples/native/tool_use/dataset/tasks/**/task.yaml'``
    is the CI operator's entry point. It parses every ``task.yaml`` via
    :class:`TaskConfig` and its ``grading.yaml`` via
    :class:`GradingConfig`. Exit 0 means the split survived every
    field a real task pack authors.

    The CLI writes its Rich-console output to ``stderr`` (Rich's
    default when ``mix_stderr=False``), so the ✓ / ✗ markers and the
    Summary line appear there rather than ``stdout``. Both markers
    must show up — a silent glob miss (no tasks matched) would produce
    a Summary of ``0 valid, 0 invalid`` and pass an exit-code-only
    check.
    """
    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli, ["validate", "--tasks", _EXAMPLE_TASK_GLOB])
    assert result.exit_code == 0, (result.stdout or "") + "\n" + (result.stderr or "")
    combined = (result.stdout or "") + (result.stderr or "")
    assert combined.count("Summary:") == 1
    assert " 0 invalid" in combined
    assert " 0 valid" not in combined


# ────────────────────────────────────────────────────────────────
# Persona 3 — Output-bundle consumer: golden trial YAML → split models
# ────────────────────────────────────────────────────────────────


def test_golden_grade_yaml_parses_via_split_grade_model():
    """A downstream reader loading ``grade.yaml`` from a completed run
    goes through :class:`Grade`. The golden fixture exercises
    :class:`GradeComponents` (all four tiers), a diff-rich ``reasons``
    string, and the ``state_diff`` sub-block."""
    with (_GOLDEN_TRIAL_ROOT / "grade.yaml").open() as f:
        payload = yaml.safe_load(f)

    grade = Grade(**payload)
    assert isinstance(grade, Grade)
    assert isinstance(grade.components, GradeComponents)
    # Wire-lock: reserialise and re-parse; parsing must be idempotent.
    dumped = grade.model_dump(mode="json")
    reconstructed = Grade(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


def test_golden_trajectory_yaml_parses_via_split_trajectory_model():
    """A ``trajectory.yaml`` read is the primary consumer surface for
    the trial's message trace + :class:`Metrics`. The golden fixture
    carries a non-empty message list and populated metrics, so parsing
    exercises :class:`Message` + :class:`ToolCall` + :class:`Metrics`
    at once.
    """
    with (_GOLDEN_TRIAL_ROOT / "trajectory.yaml").open() as f:
        payload = yaml.safe_load(f)

    trajectory = Trajectory(**payload)
    assert isinstance(trajectory, Trajectory)
    assert isinstance(trajectory.metrics, Metrics)
    dumped = trajectory.model_dump(mode="json")
    reconstructed = Trajectory(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


def test_golden_task_yaml_parses_via_split_task_model():
    """The recorded ``task.yaml`` snapshot inside an output bundle is
    consumed by post-run analyzers. It must load via the split
    :class:`TaskConfig`."""
    with (_GOLDEN_TRIAL_ROOT / "task.yaml").open() as f:
        payload = yaml.safe_load(f)

    task = TaskConfig(**payload)
    assert isinstance(task, TaskConfig)
    dumped = task.model_dump(mode="json")
    reconstructed = TaskConfig(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


def test_current_wire_metrics_parses_via_split_metrics_model():
    """The trial ``metrics.yaml`` a live run emits today is consumed
    by the aggregate reporter and third-party analysis. Must parse via
    the split :class:`Metrics` and round-trip clean.

    Sources the payload from the current-schema wire golden
    (:data:`_RUN_TRIAL_JSONL`) rather than from
    ``tests/data/projects/food_delivery_2/output/…/metrics.yaml``,
    which is a legacy fixture predating the ``usage.*`` /
    ``tool_usage.tool_name`` schema flip and does not represent what
    the runner emits today. That fixture drift is a pre-existing
    stale-golden condition, not a regression of this split — filed as
    a follow-up.
    """
    line = _RUN_TRIAL_JSONL.read_text().splitlines()[0]
    envelope = json.loads(line)
    metrics_payload = envelope["result"]["trajectory"]["metrics"]

    metrics = Metrics(**metrics_payload)
    assert isinstance(metrics, Metrics)
    dumped = metrics.model_dump(mode="json")
    reconstructed = Metrics(**dumped)
    assert reconstructed.model_dump(mode="json") == dumped


# ────────────────────────────────────────────────────────────────
# Persona 4 — JSON-Lines subprocess consumer: run-trial wire
# ────────────────────────────────────────────────────────────────


def test_run_trial_jsonl_result_envelope_round_trips_via_split_models():
    """The ``tolokaforge run-trial`` JSON-Lines wire is what external
    harnesses read. The ``result`` envelope carries a
    :class:`~tolokaforge.core.trial.TrialResult` whose transitive
    fields exercise :class:`Trajectory`, :class:`Grade`,
    :class:`GradeComponents`, :class:`Metrics`, and every enum.

    The golden line is the shape emitted by the runner today; parsing
    it back into :class:`TrialResult` and re-serialising must be
    bit-for-bit identical, otherwise the wire has drifted.
    """
    line = _RUN_TRIAL_JSONL.read_text().splitlines()[0]
    envelope = json.loads(line)
    assert envelope["type"] == "result"

    result = TrialResult(**envelope["result"])
    assert isinstance(result, TrialResult)
    assert isinstance(result.trajectory, Trajectory)
    assert isinstance(result.trajectory.grade, Grade)
    assert isinstance(result.trajectory.grade.components, GradeComponents)
    assert isinstance(result.trajectory.metrics, Metrics)

    # Re-emit and re-parse. Both the JSON body AND the wrapper envelope
    # must be idempotent under the split.
    reemitted = result.model_dump(mode="json")
    reconstructed = TrialResult(**reemitted)
    assert reconstructed.model_dump(mode="json") == reemitted
    # And the exact wire line the runner would have produced.
    assert json.loads(json.dumps(reemitted)) == reemitted
