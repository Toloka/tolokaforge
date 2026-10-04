"""The run-level output-format switch is plumbed into the artifact-write phase.

``InProcessConductor._write_artifacts`` reads
``RunConfig.effective_output_format()`` and persists the tolokaforge trial
bundle. Every format persists that bundle: it is the whole output under
``tolokaforge`` (the default) and the engine-side half under ``native`` /
``both``. Native-artifact preservation attaches to the latter two separately,
so today ``native`` and ``both`` write the same per-trial file set as the
default — this test locks that equivalence and the default-unchanged contract.

The write path is driven end-to-end: a real ``FileArtifactWriter``, the
production ``_write_artifacts`` phase, and a real ``RunConfig`` carrying each
``output.format`` value — no mock of the code under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    runner_stub,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    OutputConfig,
    OutputFormat,
    RunConfig,
    Trajectory,
    TrialStatus,
)

pytestmark = pytest.mark.canonical


_EXPECTED_TRIAL_FILES = sorted(
    [
        "env.yaml",
        "grade.yaml",
        "logs.yaml",
        "metrics.yaml",
        "prompts.yaml",
        "task.yaml",
        "tool_log.yaml",
        "tools_schemas.yaml",
        "trajectory.yaml",
    ]
)


def _run_config_for(tmp_path: Path, output: OutputConfig | None) -> RunConfig:
    base = make_run_config(tmp_path / "results")
    if output is None:
        return base
    return base.model_copy(update={"output": output})


def _write_bundle(tmp_path: Path, output: OutputConfig | None) -> Path:
    """Drive ``_write_artifacts`` for one synthetic trial under *output* and
    return the trial directory the bundle landed in."""
    conductor = make_conductor(_run_config_for(tmp_path, output), tmp_path, MagicMock())

    task = make_task_config("task_with_output_switch")
    setup = make_setup(tmp_path, task.task_id, 0)
    now = datetime.now(UTC)
    trajectory = Trajectory(
        task_id=task.task_id,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        metrics=Metrics(),
        grade=Grade(binary_pass=True, score=1.0, components=GradeComponents()),
    )

    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{task.task_id}:0", task_id=task.task_id),
        task,
        setup,
        trajectory,
        runner_stub(),
    )
    return setup.trial_dir


def _trial_file_names(trial_dir: Path) -> list[str]:
    return sorted(p.name for p in trial_dir.iterdir() if p.is_file())


def test_default_run_writes_the_tolokaforge_trial_bundle(tmp_path: Path) -> None:
    """A run with no ``output`` block resolves to ``tolokaforge`` and writes the
    full per-trial bundle — the file set the engine has always written."""
    trial_dir = _write_bundle(tmp_path, output=None)
    assert _trial_file_names(trial_dir) == _EXPECTED_TRIAL_FILES


@pytest.mark.parametrize(
    "fmt",
    [OutputFormat.TOLOKAFORGE, OutputFormat.NATIVE, OutputFormat.BOTH],
)
def test_every_format_currently_writes_the_same_trial_bundle(
    tmp_path: Path, fmt: OutputFormat
) -> None:
    """``tolokaforge``, ``native`` and ``both`` all persist the tolokaforge
    bundle today — native-artifact preservation for ``native`` / ``both`` is a
    later step. Each format writes into its own directory, and the file set is
    the default bundle's."""
    trial_dir = _write_bundle(tmp_path / fmt.value, output=OutputConfig(format=fmt))
    assert _trial_file_names(trial_dir) == _EXPECTED_TRIAL_FILES
