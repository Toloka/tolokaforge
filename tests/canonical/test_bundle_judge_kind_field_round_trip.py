"""Grade bundle's ``grading_config.json`` round-trips
``LLMJudgeConfig.judge_kind`` + ``kind_config``.

Locks that the two new ``LLMJudgeConfig`` fields survive the
grade-bundle producer → reader path with no producer / reader / manifest
/ schema-version edits: they ride the existing ``grading_config.json``
part via ``TaskDescription.grading.model_dump(mode="json")``, and the
snapshot regrade path reconstructs ``LLMJudgeConfig`` from that dict
with byte-identity on both fields.

If a future producer edit accidentally drops either field, or a schema
change reshapes ``grading_config.json`` in a way the model refuses,
this test surfaces the regression at the persistence boundary rather
than at a downstream dispatch site.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.bundle import load_grade_bundle
from tolokaforge.core.grading.bundle_producer import serialize_bundle_from_substrate
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    TaskDescription,
)

pytestmark = pytest.mark.canonical


def _task_description() -> TaskDescription:
    """A minimal ``TaskDescription`` whose ``grading.llm_judge`` carries
    both new fields explicitly (default kind + non-empty ``kind_config``)."""
    rubric = Rubric(
        criteria=[
            Criterion(
                id="answered",
                description="Agent answered the question",
                kind="binary",
            )
        ]
    )
    return TaskDescription(
        task_id="bundle-round-trip-task",
        name="bundle round trip task",
        category="test",
        description="fixture task with judge_kind + kind_config",
        adapter_type="native",
        system_prompt="you are the agent",
        grading=RunnerGradingConfig(
            weights={"llm_judge": 1.0},
            llm_judge=LLMJudgeConfig(
                rubric=rubric,
                judge_kind="single_shot_rubric",
                kind_config={"foo": "bar", "nested": {"n": 1}},
            ),
        ),
    )


class _StubTrajectory:
    """Structural stand-in for a ``Trajectory`` — the bundle producer calls
    ``model_dump(mode="json")`` and treats the return as the trajectory
    payload."""

    def model_dump(self, *, mode: str) -> dict:
        del mode
        return {"messages": []}


def test_grading_config_json_round_trips_judge_kind_and_kind_config(tmp_path: Path) -> None:
    """A grade bundle produced from a task whose ``grading.llm_judge``
    carries ``judge_kind=single_shot_rubric`` + ``kind_config={...}`` is
    re-parsed cleanly, and the reconstructed ``LLMJudgeConfig`` is
    byte-identical to the source on both fields."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "artifact.txt").write_bytes(b"agent-produced output\n")

    task_description = _task_description()

    substrate = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=workspace,
        initial_state={},
        final_state={},
    )
    bundle_dir = tmp_path / "bundle"
    serialize_bundle_from_substrate(
        substrate=substrate,
        trial_id="bundle-round-trip-task:0",
        out_dir=bundle_dir,
        trajectory=_StubTrajectory(),
        task_description=task_description,
    )

    view = load_grade_bundle(bundle_dir)
    grading_config = json.loads(view.open_part("grading_config.json"))

    assert grading_config["llm_judge"]["judge_kind"] == "single_shot_rubric"
    assert grading_config["llm_judge"]["kind_config"] == {"foo": "bar", "nested": {"n": 1}}

    reconstructed = LLMJudgeConfig.model_validate(grading_config["llm_judge"])
    assert reconstructed.judge_kind == "single_shot_rubric"
    assert reconstructed.kind_config == {"foo": "bar", "nested": {"n": 1}}
