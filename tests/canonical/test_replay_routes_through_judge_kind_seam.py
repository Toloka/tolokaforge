"""Offline replay dispatches through the :class:`JudgeKind` seam.

Locks that :func:`tolokaforge.core.grading.replay.replay_trial` reads
the recorded ``judge_kind`` + ``kind_config`` off the bundle's
``task.yaml.grading_config.llm_judge`` and dispatches through
``load_judge_kind(inputs.judge_kind)()`` — a recorded ``chunked_rubric``
run replays through :class:`ChunkedRubricJudgeKind` (returned
``JudgeResult.chunk_boundaries`` is non-empty and matches the recorded
partition, and :func:`build_replay_grade` projects it onto
``Grade.judge_chunk_boundaries``), while a legacy trial artifact
without the two fields defaults to ``single_shot_rubric`` (the
byte-parity anchor). The composite-recompute populator lock lives at
``tests/canonical/test_composite_recompute_carries_chunk_boundaries.py``.
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
    args: dict = {"reasons": "chunk verdicts"}
    for cid in chunk_ids:
        args[cid] = True
        args[f"{cid}_justification"] = f"{cid}: VERDICT: MET"
    return [("submit_report", args)]


class TestChunkedRubricReplaysThroughKindSeam:
    """A recorded chunked_rubric trial replays through
    :class:`ChunkedRubricJudgeKind`, and :func:`build_replay_grade`
    projects the partition onto ``Grade.judge_chunk_boundaries``."""

    def test_chunked_replay_carries_recorded_chunk_boundaries(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trials" / "refund_task" / "0"
        _write_bundle(
            trial_dir,
            llm_judge_block={
                "judge_kind": "chunked_rubric",
                "kind_config": {"chunk_size": 2},
                "rubric": {
                    "reference": "Refund quotes $328.50.",
                    "criteria": [
                        {"id": "a", "description": "a", "kind": "binary"},
                        {"id": "b", "description": "b", "kind": "binary"},
                        {"id": "c", "description": "c", "kind": "binary"},
                    ],
                },
            },
        )

        inputs = read_replay_inputs(trial_dir)

        assert inputs.judge_kind == "chunked_rubric"
        assert inputs.kind_config == {"chunk_size": 2}
        assert inputs.provenance.judge_kind == "chunked_rubric"
        assert inputs.provenance.judge_kind_source is ProvenanceSource.RECORDED

        client = ScriptedClient(
            [
                _submit_report_step(("a", "b")),
                _submit_report_step(("c",)),
            ]
        )
        result = replay_trial(inputs, judge_client=client)

        assert result.status is JudgeRunStatus.COMPLETED
        assert result.chunk_boundaries == (("a", "b"), ("c",))
        grade = build_replay_grade(result)
        assert grade.judge_chunk_boundaries == [["a", "b"], ["c"]]


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
        assert result.chunk_boundaries == ()
        assert build_replay_grade(result).judge_chunk_boundaries is None
