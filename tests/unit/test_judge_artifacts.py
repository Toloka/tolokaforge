"""Stage 5 — the rubric judge's usage + transcript reach the trial bundle.

Pins the output-plumbing behaviour: a ``Grade`` carrying ``criterion_results``,
``judge_status``, ``judge_usage`` and ``judge_transcript`` is written by the
real :class:`FileArtifactWriter` into a tmp dir such that

* ``grade.yaml`` carries the per-criterion breakdown + judge_status + judge
  usage, and does NOT inline the (large) transcript; and
* ``judge_trajectory.yaml`` carries the judge's message transcript sidecar.

Uses the real writer + a tmp path (no over-mocking), and round-trips the data
back off disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import (
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeStatus,
    JudgeUsage,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter

pytestmark = pytest.mark.unit


def _judge_grade() -> Grade:
    return Grade(
        binary_pass=True,
        score=0.8,
        components=GradeComponents(llm_judge=0.8),
        reasons="Judge: refund quoted correctly | tone slightly terse",
        criterion_results=[
            CriterionResult(
                id="refund_amount",
                met=True,
                score=1.0,
                justification="Reply quotes the correct $328.50 refund.",
            ),
            CriterionResult(
                id="tone",
                met=False,
                score=0.4,
                justification="Polite but terse.",
            ),
        ],
        judge_status=JudgeStatus.COMPLETED,
        judge_usage=JudgeUsage(
            calls=3,
            prompt_tokens=4120,
            completion_tokens=318,
            reasoning_tokens=0,
            cost_usd=0.0142,
            tool_calls=4,
            consistency_rejections=2,
        ),
        judge_transcript=[
            {"role": "system", "content": "You are a grading judge."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "get_db_state", "arguments": {"tables": ["orders"]}}
                ],
            },
            {"role": "tool", "content": "{...}", "tool_call_id": "c1"},
        ],
    )


def test_write_grade_emits_breakdown_usage_and_transcript_sidecar(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_grade(trial_dir, _judge_grade())

    grade_path = trial_dir / "grade.yaml"
    transcript_path = trial_dir / "judge_trajectory.yaml"
    assert grade_path.exists()
    assert transcript_path.exists()

    grade = yaml.safe_load(grade_path.read_text())

    # Per-criterion breakdown round-trips.
    ids = [c["id"] for c in grade["criterion_results"]]
    assert ids == ["refund_amount", "tone"]
    refund, tone = grade["criterion_results"]
    assert refund["met"] is True and refund["score"] == 1.0
    assert tone["met"] is False and tone["score"] == pytest.approx(0.4)
    assert "terse" in tone["justification"]

    # Judge status + the judge's own usage land in grade.yaml.
    assert grade["judge_status"] == "completed"
    assert grade["judge_usage"]["calls"] == 3
    assert grade["judge_usage"]["prompt_tokens"] == 4120
    assert grade["judge_usage"]["cost_usd"] == pytest.approx(0.0142)
    assert grade["judge_usage"]["tool_calls"] == 4
    assert grade["judge_usage"]["consistency_rejections"] == 2

    # The transcript is NOT inlined into grade.yaml — it lives in the sidecar.
    assert "judge_transcript" not in grade

    transcript = yaml.safe_load(transcript_path.read_text())
    msgs = transcript["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["tool_calls"][0]["name"] == "get_db_state"
    assert msgs[2]["tool_call_id"] == "c1"


def test_write_grade_without_judge_writes_no_transcript_sidecar(tmp_path: Path) -> None:
    """No judge ⇒ grade.yaml only, no judge_trajectory.yaml sidecar."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_b" / "0"

    writer.write_grade(
        trial_dir,
        Grade(binary_pass=True, score=1.0, components=GradeComponents(state_checks=1.0)),
    )

    assert (trial_dir / "grade.yaml").exists()
    assert not (trial_dir / "judge_trajectory.yaml").exists()


def test_errored_judge_usage_and_partial_transcript_persist(tmp_path: Path) -> None:
    """An ERRORED judge still records its usage + partial transcript (fail loud)."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_c" / "0"

    grade = Grade(
        binary_pass=False,
        score=0.0,
        components=GradeComponents(state_checks=0.5),  # llm_judge excluded (unscored)
        reasons="JUDGE ERRORED: did not call submit_report",
        judge_status=JudgeStatus.ERRORED,
        judge_usage=JudgeUsage(
            calls=2,
            prompt_tokens=900,
            completion_tokens=50,
            tool_calls=1,
            consistency_rejections=3,
        ),
        judge_transcript=[{"role": "system", "content": "judge prompt"}],
    )
    writer.write_grade(trial_dir, grade)

    loaded = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    assert loaded["judge_status"] == "errored"
    assert loaded["judge_usage"]["calls"] == 2
    # An ERRORED judge still persists the consistency counter (fail loud).
    assert loaded["judge_usage"]["consistency_rejections"] == 3
    # llm_judge stays None/-1 sentinel territory — no 0.0 fabricated for the judge.
    assert loaded["components"]["llm_judge"] is None

    sidecar = yaml.safe_load((trial_dir / "judge_trajectory.yaml").read_text())
    assert sidecar["messages"][0]["content"] == "judge prompt"


def test_in_memory_writer_carries_judge_fields(tmp_path: Path) -> None:
    """The in-memory writer stores the full Grade incl. judge usage / transcript."""
    writer = InMemoryArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_d" / "0"
    grade = _judge_grade()

    writer.write_grade(trial_dir, grade)

    stored = writer.trials[trial_dir].grade
    assert stored is grade
    assert stored.judge_usage is not None and stored.judge_usage.calls == 3
    assert stored.judge_transcript is not None and len(stored.judge_transcript) == 3
