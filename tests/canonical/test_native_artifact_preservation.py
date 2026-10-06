"""``native`` / ``both`` preserve a harness's own artifacts under ``native/``.

The artifact-write phase reads the native bytes the runner staged off the trial
container and writes them under ``trials/.../native/``, keeping the harness's own
subtree (``native/logs/verifier/reward.txt``). The normalised tolokaforge bundle
is written exactly as before, so:

- ``both`` = the full normalised bundle PLUS ``native/``,
- ``native`` = the full normalised bundle PLUS ``native/`` (the reduced skeleton
  is deferred — the full bundle is safe for resume / observer),
- ``tolokaforge`` writes NO ``native/`` even when bytes are staged,
- a harness / engine-loop trial that staged nothing creates NO empty ``native/``.

The write path is driven end-to-end: a real ``FileArtifactWriter``, the
production ``_write_artifacts`` phase, a real ``RunConfig`` carrying each
``output.format``, and the native bytes staged on the runner the way
``run_harness`` stages them.
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


_NATIVE_BYTES = {
    "logs/verifier/reward.txt": b"1.0\n",
    "logs/agent/session.log": b"agent did a thing\n",
}

_EXPECTED_BUNDLE_FILES = sorted(
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


def _run_config_for(tmp_path: Path, fmt: OutputFormat) -> RunConfig:
    return make_run_config(tmp_path / "results").model_copy(
        update={"output": OutputConfig(format=fmt)}
    )


def _write_bundle(tmp_path: Path, fmt: OutputFormat, *, native: dict[str, bytes] | None) -> Path:
    """Drive ``_write_artifacts`` for one trial under *fmt* with *native* bytes
    staged on the runner; return the trial directory the bundle landed in."""
    conductor = make_conductor(_run_config_for(tmp_path, fmt), tmp_path, MagicMock())

    task = make_task_config("task_with_native_artifacts")
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
        runner_stub(harness_native_artifacts=native),
    )
    return setup.trial_dir


def _bundle_file_names(trial_dir: Path) -> list[str]:
    return sorted(p.name for p in trial_dir.iterdir() if p.is_file())


@pytest.mark.parametrize("fmt", [OutputFormat.NATIVE, OutputFormat.BOTH])
def test_native_and_both_write_the_bundle_plus_native_subtree(
    tmp_path: Path, fmt: OutputFormat
) -> None:
    """Staged native bytes land under ``native/`` keeping the harness subtree,
    and the full normalised bundle is written alongside."""
    trial_dir = _write_bundle(tmp_path / fmt.value, fmt, native=_NATIVE_BYTES)

    # Full normalised bundle, unchanged.
    assert _bundle_file_names(trial_dir) == _EXPECTED_BUNDLE_FILES

    # Harness subtree preserved verbatim under native/.
    native_root = trial_dir / "native"
    assert (native_root / "logs/verifier/reward.txt").read_bytes() == b"1.0\n"
    assert (native_root / "logs/agent/session.log").read_bytes() == b"agent did a thing\n"


def test_tolokaforge_writes_no_native_dir_even_when_bytes_are_staged(
    tmp_path: Path,
) -> None:
    """The default format never writes ``native/``; the gate is the format, not
    whether bytes happen to be staged on the runner."""
    trial_dir = _write_bundle(tmp_path, OutputFormat.TOLOKAFORGE, native=_NATIVE_BYTES)

    assert _bundle_file_names(trial_dir) == _EXPECTED_BUNDLE_FILES
    assert not (trial_dir / "native").exists()


@pytest.mark.parametrize("fmt", [OutputFormat.NATIVE, OutputFormat.BOTH])
def test_no_staged_bytes_creates_no_empty_native_dir(tmp_path: Path, fmt: OutputFormat) -> None:
    """An engine-loop / no-artifact trial under ``native`` / ``both`` collapses
    to the full bundle with no empty ``native/`` directory."""
    trial_dir = _write_bundle(tmp_path / fmt.value, fmt, native=None)

    assert _bundle_file_names(trial_dir) == _EXPECTED_BUNDLE_FILES
    assert not (trial_dir / "native").exists()
