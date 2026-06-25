"""A proto Grade carrying criterion_results round-trips to the Pydantic Grade.

Pins the Stage-2 data-plane seam: the runner returns per-criterion rubric
results on the proto ``Grade``; ``RunnerClient.grade_trial`` lowers that proto
into the dict the orchestrator consumes; the orchestrator then builds the
Pydantic ``Grade`` from that dict. This test drives the real proto→dict path
(via a stubbed gRPC stub) and the real dict→Pydantic build, so the new
``criterion_results`` field survives the whole trip without loss.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.docker_runtime import RunnerClient
from tolokaforge.core.models import CriterionResult, Grade, GradeComponents
from tolokaforge.runner import runner_pb2

pytestmark = pytest.mark.unit


def _grade_from_dict(g: dict) -> Grade:
    """Mirror the orchestrator's dict→Pydantic Grade construction."""
    criterion_results = None
    raw = g.get("criterion_results")
    if raw:
        criterion_results = [CriterionResult(**cr) for cr in raw]
    return Grade(
        binary_pass=g["binary_pass"],
        score=g["score"],
        components=GradeComponents(
            state_checks=g["components"].get("state_checks", -1.0),
            transcript_rules=g["components"].get("transcript_rules", -1.0),
            llm_judge=g["components"].get("llm_judge", -1.0),
            custom_checks=g["components"].get("custom_checks", -1.0),
        ),
        reasons=g.get("reasons", ""),
        criterion_results=criterion_results,
    )


def test_proto_grade_criterion_results_round_trip():
    proto_grade = runner_pb2.Grade(
        binary_pass=True,
        score=0.85,
        components=runner_pb2.GradeComponents(
            state_checks=-1.0,
            transcript_rules=-1.0,
            llm_judge=0.85,
            custom_checks=-1.0,
        ),
        reasons="Judge: refund quoted correctly",
        criterion_results=[
            runner_pb2.CriterionResult(
                id="refund_amount",
                met=True,
                score=1.0,
                justification="Reply quotes $328.50",
            ),
            runner_pb2.CriterionResult(
                id="tone",
                met=False,
                score=0.4,
                justification="Slightly terse",
            ),
        ],
        judge_status=runner_pb2.JUDGE_STATUS_COMPLETED,
    )
    response = runner_pb2.GradeTrialResponse(success=True, grade=proto_grade)

    client = RunnerClient.__new__(RunnerClient)
    client.stub = MagicMock()
    client.stub.GradeTrial.return_value = response

    result = client.grade_trial(trial_id="t:0")

    assert result["success"] is True
    raw = result["grade"]["criterion_results"]
    assert [c["id"] for c in raw] == ["refund_amount", "tone"]
    assert result["grade"]["judge_status"] == runner_pb2.JUDGE_STATUS_COMPLETED

    grade = _grade_from_dict(result["grade"])
    assert grade.criterion_results is not None
    assert len(grade.criterion_results) == 2
    refund, tone = grade.criterion_results
    assert refund.id == "refund_amount" and refund.met is True and refund.score == 1.0
    assert tone.id == "tone" and tone.met is False and tone.score == pytest.approx(0.4)
    assert tone.justification == "Slightly terse"


def test_proto_grade_without_criterion_results_yields_none():
    """No rubric judge ⇒ empty proto repeated field ⇒ Pydantic None (not [])."""
    proto_grade = runner_pb2.Grade(binary_pass=False, score=0.0)
    response = runner_pb2.GradeTrialResponse(success=True, grade=proto_grade)

    client = RunnerClient.__new__(RunnerClient)
    client.stub = MagicMock()
    client.stub.GradeTrial.return_value = response

    result = client.grade_trial(trial_id="t:0")
    assert result["grade"]["criterion_results"] == []

    grade = _grade_from_dict(result["grade"])
    assert grade.criterion_results is None
