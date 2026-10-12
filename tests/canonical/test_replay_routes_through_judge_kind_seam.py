"""Offline replay dispatches through the :class:`JudgeKind` seam.

Locks that :func:`tolokaforge.core.grading.replay.replay_trial` reads
the recorded ``judge_kind`` + ``kind_config`` off the bundle's
``task.yaml.grading_config.llm_judge`` and dispatches through
``load_judge_kind(inputs.judge_kind)()``; a legacy trial artifact
without the two fields defaults to ``single_shot_rubric`` (the
byte-parity anchor).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.grading.test_judge import ScriptedClient
from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    ProvenanceSource,
    build_replay_grade,
    read_replay_inputs,
    replay_trial,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeStatus,
    Message,
    MessageRole,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.canonical


_AGENT_PROMPT = "You are the refund agent."
_JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}


def _trajectory() -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="Refund order O-1."),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c1", name="get_order", arguments={"id": "O-1"})],
            ),
            Message(role=MessageRole.TOOL, content='{"total": 328.5}', tool_call_id="c1"),
            Message(role=MessageRole.ASSISTANT, content="Refund of $328.50 issued."),
        ],
    )


def _write_bundle(
    trial_dir: Path,
    *,
    llm_judge_block: dict,
) -> None:
    """Write a full replay-eligible bundle whose ``grading_config.llm_judge``
    is ``llm_judge_block`` verbatim — the test controls whether the block
    carries ``judge_kind`` / ``kind_config`` or leaves them absent (legacy
    shape)."""
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, _trajectory())
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt")
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": {"llm_judge": llm_judge_block},
            "model_config": {"judge": _JUDGE_MODEL},
        },
    )
    writer.write_grade(
        trial_dir,
        Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=JudgeStatus.COMPLETED,
            judge_inputs=JudgeInputs(read_tools_offered=[]),
        ),
    )


def _submit_report_step(chunk_ids: tuple[str, ...]) -> list[tuple[str, dict]]:
    """Build one scripted judge turn that emits ``submit_report`` marking
    every criterion in ``chunk_ids`` as MET."""
    args: dict = {"reasons": "verdicts"}
    for cid in chunk_ids:
        args[cid] = True
        args[f"{cid}_justification"] = f"{cid}: VERDICT: MET"
    return [("submit_report", args)]


class TestLegacyArtifactDefaultsToSingleShot:
    """A trial artifact whose ``grading_config.llm_judge`` lacks both
    ``judge_kind`` and ``kind_config``; the resolver defaults
    them to ``("single_shot_rubric", None)`` with
    ``ProvenanceSource.RECORDED`` and the reference kind grades the trial."""

    def test_legacy_llm_judge_block_routes_through_single_shot(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trials" / "refund_task" / "0"
        _write_bundle(
            trial_dir,
            llm_judge_block={
                "rubric": {
                    "reference": "Refund quotes $328.50.",
                    "criteria": [
                        {"id": "refund", "description": "Refund quotes $328.50", "kind": "binary"},
                    ],
                },
            },
        )

        inputs = read_replay_inputs(trial_dir)

        assert inputs.judge_kind == "single_shot_rubric"
        assert inputs.kind_config is None
        assert inputs.provenance.judge_kind == "single_shot_rubric"
        assert inputs.provenance.judge_kind_source is ProvenanceSource.RECORDED

        client = ScriptedClient([_submit_report_step(("refund",))])
        result = replay_trial(inputs, judge_client=client)

        assert result.status is JudgeRunStatus.COMPLETED
        grade = build_replay_grade(result)
        assert grade.judge_status is JudgeStatus.COMPLETED
        assert grade.criterion_results is not None
        assert [c.id for c in grade.criterion_results] == ["refund"]
        assert grade.criterion_results[0].met is True
