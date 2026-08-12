"""A trial bundle's ``task.yaml`` names the user actor and the pinned opener.

The snapshot answers, from the bundle alone, which user was simulated and what
the authored opener was — the resolution the run used, not what the pack typed,
and ``user_actor: null`` under ``agent_only`` where the run resolved no
simulator at all.

The production artifact-write phase is driven directly:
``InProcessConductor.run()`` is not drivable without a real environment, so
``_write_artifacts`` is called with the arguments the builders in
:mod:`tests.utils.conductor_phases` assemble, and every assertion reads a real
file off disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._factories import make_trial_spec
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    make_task_config,
    runner_stub,
)
from tolokaforge.core.models import ActorSpec, Metrics, TaskConfig, Trajectory, TrialStatus

pytestmark = pytest.mark.unit


def _written_snapshot(tmp_path: Path, task: TaskConfig) -> dict[str, Any]:
    """Drive the production artifact-write phase; return the ``task.yaml`` it wrote."""
    # The artifact-write phase never grades, so the grader seam stays a double.
    conductor = make_conductor(make_run_config(tmp_path / "results"), tmp_path, MagicMock())
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
    )

    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{task.task_id}:0", task_id=task.task_id),
        task,
        setup,
        trajectory,
        runner_stub(),
    )

    return yaml.safe_load((setup.trial_dir / "task.yaml").read_text(encoding="utf-8"))


def test_a_declared_user_actor_is_recorded_whole(tmp_path: Path) -> None:
    """All four ``UserSimulatorConfig`` fields, scripted flow included — a trial
    whose user turns were scripted has no other record of what was said."""
    flow = [{"role": "user", "content": "Hi, I need to replace my season pass."}]
    task = make_task_config(
        "conversational_declared",
        actors={
            "user": ActorSpec(
                mode="scripted",
                persona="frustrated commuter",
                backstory="I lost my season pass last week.",
                scripted_flow=flow,
            )
        },
    )

    snapshot = _written_snapshot(tmp_path, task)

    assert snapshot["interaction_mode"] == "conversational"
    assert snapshot["user_actor"] == {
        "mode": "scripted",
        "persona": "frustrated commuter",
        "backstory": "I lost my season pass last week.",
        "scripted_flow": flow,
    }
    # The three keys are written where the bundle's shape documents them, and
    # the writer hands the whole snapshot through in that order.
    assert list(snapshot) == [
        "task_id",
        "trial_index",
        "category",
        "description",
        "interaction_mode",
        "initial_user_message",
        "user_actor",
        "grading_config",
        "tools",
        "policies",
        "model_config",
    ]


def test_a_task_declaring_no_user_actor_records_the_resolution_the_run_used(
    tmp_path: Path,
) -> None:
    """The defaults the conductor resolved, spelled out — not an absent key the
    reader has to know the defaults to interpret."""
    snapshot = _written_snapshot(tmp_path, make_task_config("conversational_default", actors=None))

    assert snapshot["user_actor"] == {
        "mode": "llm",
        "persona": "cooperative",
        "backstory": None,
        "scripted_flow": None,
    }


def test_an_agent_only_trial_records_a_null_actor_its_mode_attributes(tmp_path: Path) -> None:
    """``agent_only`` resolves no simulator, so there is no configuration to
    record. The task still declares ``actors.user``, so the ``null`` is
    attributable to the turn-loop shape rather than to an undeclared actor —
    which is what ``interaction_mode`` beside it says."""
    snapshot = _written_snapshot(
        tmp_path, make_task_config("agent_only_task", interaction_mode="agent_only")
    )

    assert snapshot["interaction_mode"] == "agent_only"
    assert snapshot["user_actor"] is None


@pytest.mark.parametrize(
    "opener",
    [
        "  Hi, I need to replace my pass.  ",
        "Hi, I need to replace my pass.\n\n",
        "\tHi, I need to replace my pass.\t",
        "Hi, I need to replace my pass.",
        None,
    ],
    ids=["surrounding-spaces", "trailing-newlines", "tabs", "no-whitespace", "unset"],
)
def test_the_pinned_opener_is_recorded_verbatim(tmp_path: Path, opener: str | None) -> None:
    """The text the agent really received, whitespace and all — the opener is
    delivered verbatim, so a bundle that trims it misreports the first turn."""
    snapshot = _written_snapshot(
        tmp_path, make_task_config("pinned_opener", initial_user_message=opener)
    )

    assert snapshot["initial_user_message"] == opener
