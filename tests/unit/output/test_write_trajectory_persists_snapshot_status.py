"""``FileArtifactWriter.write_trajectory`` round-trips ``snapshot_status``.

Locks the writer's on-disk shape for the trial-end grade-bundle producer
outcome: every :class:`SnapshotOutcome` value, and the ``None`` case,
persist through YAML into a :class:`Trajectory` that compares equal to
the original on ``snapshot_status``. ``SnapshotStatus.model_config``
sets ``extra="forbid"`` so any typo the writer might introduce fails
loud at load. The ``None`` case asserts the key is present with a
``null`` value — the writer keeps the shape self-documenting, so a
consumer that reads raw YAML sees the field either way.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.models.trajectory import SnapshotOutcome, SnapshotStatus
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.unit


def _build_trajectory(snapshot_status: SnapshotStatus | None) -> Trajectory:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trajectory(
        task_id="snapshot-status-roundtrip",
        trial_index=0,
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.COMPLETED,
        messages=[],
        snapshot_status=snapshot_status,
    )


_STATUS_CASES: list[SnapshotStatus | None] = [
    SnapshotStatus.stored(
        uri="bundle://local_disk/" + "0" * 64,
        bundle_size_bytes=12_345,
    ),
    SnapshotStatus.oversize(bundle_size_bytes=40_000_000, cap_bytes=33_554_432),
    SnapshotStatus.produce_failed("store construction: PermissionError('/nope')"),
    SnapshotStatus.ungraded(),
    None,
]


@pytest.mark.parametrize("snapshot_status", _STATUS_CASES)
def test_write_trajectory_round_trips_snapshot_status(
    tmp_path: Path,
    snapshot_status: SnapshotStatus | None,
) -> None:
    trajectory = _build_trajectory(snapshot_status)
    trial_dir = tmp_path / "trial"

    FileArtifactWriter().write_trajectory(trial_dir, trajectory)

    reread = Trajectory.model_validate(
        yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    )
    assert reread.snapshot_status == snapshot_status


def test_write_trajectory_emits_snapshot_status_null_key_when_absent(tmp_path: Path) -> None:
    trajectory = _build_trajectory(snapshot_status=None)
    trial_dir = tmp_path / "trial"

    FileArtifactWriter().write_trajectory(trial_dir, trajectory)

    raw = yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    assert "snapshot_status" in raw
    assert raw["snapshot_status"] is None


def test_write_trajectory_emits_snapshot_status_outcome_value(tmp_path: Path) -> None:
    trajectory = _build_trajectory(
        SnapshotStatus.stored(
            uri="bundle://local_disk/" + "a" * 64,
            bundle_size_bytes=100,
        )
    )
    trial_dir = tmp_path / "trial"

    FileArtifactWriter().write_trajectory(trial_dir, trajectory)

    raw = yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    assert raw["snapshot_status"]["outcome"] == SnapshotOutcome.STORED.value
    assert raw["snapshot_status"]["uri"] == "bundle://local_disk/" + "a" * 64
    assert raw["snapshot_status"]["bundle_size_bytes"] == 100
