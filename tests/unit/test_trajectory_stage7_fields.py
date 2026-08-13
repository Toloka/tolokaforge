"""Trajectory shape: prompts moved to ``prompts.yaml``, schema version stays.

Pre-PR-#88+1: ``Trajectory.system_prompt`` and ``Trajectory.user_system_prompt``
were embedded in the trajectory itself, bloating ``trajectory.yaml``. They now
live in a sibling ``prompts.yaml`` artifact written by
:class:`~tolokaforge.core.output.artifacts.FileArtifactWriter.write_prompts`.
``Trajectory`` no longer declares either field — accidental re-introduction
of the embedded shape will surface as a missing key in
:func:`~tolokaforge.core.output_writer.OutputWriter.write_trajectory`'s
output (covered by the canonical layout test).

``simulator_schema_version`` keeps its place on :class:`Trajectory` — it
describes the *shape* of the message trace, not the prompt itself.

Refs:
    plans/eval_tau_manufacturing_v2_post_fix_diagnosis.md (Stage 7 origin)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tolokaforge.core.models import Trajectory, TrialStatus

pytestmark = pytest.mark.unit


def _base_kwargs() -> dict:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "task_id": "stage7-trajectory-fields",
        "trial_index": 0,
        "start_ts": ts,
        "end_ts": ts,
        "status": TrialStatus.COMPLETED,
        "messages": [],
    }


def test_trajectory_does_not_have_system_prompt_field() -> None:
    """``system_prompt`` lives on the sibling ``prompts.yaml`` artifact,
    not on the trajectory itself."""
    traj = Trajectory(**_base_kwargs())
    assert not hasattr(traj, "system_prompt")


def test_trajectory_does_not_have_user_system_prompt_field() -> None:
    """``user_system_prompt`` lives on ``prompts.yaml`` too."""
    traj = Trajectory(**_base_kwargs())
    assert not hasattr(traj, "user_system_prompt")


def test_trajectory_model_fields_omit_legacy_prompts() -> None:
    """Both legacy keys are absent from ``Trajectory.model_fields``. Pydantic
    silently ignores unknown kwargs at construction time; an explicit
    ``model_fields`` check is the contract that catches a stale schema."""
    fields = set(Trajectory.model_fields.keys())
    assert "system_prompt" not in fields
    assert "user_system_prompt" not in fields


def test_trajectory_has_simulator_schema_version_field() -> None:
    """``simulator_schema_version`` stays — it tracks the shape of the
    message trace, which is what ``trajectory.yaml`` carries."""
    traj = Trajectory(**_base_kwargs())
    assert hasattr(traj, "simulator_schema_version")
    assert traj.simulator_schema_version == 3


def test_trajectory_roundtrip_preserves_simulator_schema_version() -> None:
    kwargs = _base_kwargs()
    kwargs["simulator_schema_version"] = 1
    original = Trajectory(**kwargs)

    dumped = original.model_dump(mode="json")
    assert dumped["simulator_schema_version"] == 1
    assert "system_prompt" not in dumped
    assert "user_system_prompt" not in dumped

    restored = Trajectory.model_validate(dumped)
    assert restored.simulator_schema_version == 1
