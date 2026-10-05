"""Canonical writer-shape lock for ``trajectory.yaml``.

The set of top-level keys :meth:`FileArtifactWriter.write_trajectory`
emits is a compatibility surface: external tools that parse the file
depend on the enumeration. Any new field on :class:`Trajectory` must
either be added to the writer with a deliberate update to this lock,
or explicitly omitted (with a comment on the writer explaining why).
A silent drift fails this test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import Message, MessageRole, Trajectory, TrialStatus
from tolokaforge.core.models.trajectory import (
    FirstUserMessageSource,
    SnapshotStatus,
    TerminationReason,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.canonical


_EXPECTED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "task_id",
        "trial_index",
        "harness_entry",  # owning harnesses entry (None for a single-adapter run)
        "adapter_type",  # resolved adapter's registered type for this bundle
        "execution_mode",  # engine turn loop vs delegated harness (None pre-field)
        "attempt_id",  # the final attempt the trial ran as (live tracing, ADR-0047)
        "simulator_schema_version",
        "start_ts",
        "end_ts",
        "status",
        "termination_reason",
        "provision_stage",
        "grading_error",
        "grading_judge_usage",
        "grading_state_diff",
        "grading_comparison_view",
        "snapshot_status",
        "first_user_message_source",
        "messages",
        "user_reply_guard_events",
    }
)


def _full_trajectory() -> Trajectory:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trajectory(
        task_id="writer-shape-full",
        trial_index=7,
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.ERROR,
        termination_reason=TerminationReason.PROVISION_ERROR,
        provision_stage="provision",
        grading_error=None,
        snapshot_status=SnapshotStatus.stored(
            uri="bundle://local_disk/" + "f" * 64,
            bundle_size_bytes=2048,
        ),
        first_user_message_source=FirstUserMessageSource.PINNED,
        messages=[Message(role=MessageRole.USER, content="hello")],
    )


def test_write_trajectory_top_level_keys(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    FileArtifactWriter().write_trajectory(trial_dir, _full_trajectory())

    raw = yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    assert set(raw.keys()) == _EXPECTED_TOP_LEVEL_KEYS


def test_opt_in_simulation_counters_are_persisted_without_changing_legacy_shape(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "trial"
    trajectory = _full_trajectory().model_copy(
        update={"simulation_steps": 200, "environment_errors": 10}
    )
    FileArtifactWriter().write_trajectory(trial_dir, trajectory)

    raw = yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    assert raw["simulation_steps"] == 200
    assert raw["environment_errors"] == 10
