"""A proto Grade round-trips to the Pydantic Grade the orchestrator records.

Pins the data-plane seam: the runner answers ``GradeTrial`` with a proto
``Grade``; ``RunnerClient.grade_trial`` lowers that proto into a dict; and
``_parse_grade_result`` builds the Pydantic ``Grade`` from that dict. Both
production halves run here — a stubbed gRPC stub supplies the wire message and
nothing else is stood in for, so a field that survives one half and is dropped
by the other fails.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tests.utils.wire_grades import lower_wire_grade
from tolokaforge.core.models import JudgeStatus
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.trial_grader import GradingFailedError, _parse_grade_result
from tolokaforge.runner import runner_pb2

pytestmark = pytest.mark.unit


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
    lowered = lower_wire_grade(proto_grade)

    assert [c["id"] for c in lowered["criterion_results"]] == ["refund_amount", "tone"]
    assert lowered["judge_status"] == runner_pb2.JUDGE_STATUS_COMPLETED

    grade = _parse_grade_result(lowered)
    assert grade.criterion_results is not None
    assert len(grade.criterion_results) == 2
    refund, tone = grade.criterion_results
    assert refund.id == "refund_amount" and refund.met is True and refund.score == 1.0
    assert tone.id == "tone" and tone.met is False and tone.score == pytest.approx(0.4)
    assert tone.justification == "Slightly terse"


def test_proto_grade_without_criterion_results_yields_none():
    """No rubric judge ⇒ empty proto repeated field ⇒ Pydantic None (not [])."""
    lowered = lower_wire_grade(runner_pb2.Grade(binary_pass=False, score=0.0))
    assert lowered["criterion_results"] == []

    grade = _parse_grade_result(lowered)
    assert grade.criterion_results is None
    # No judge_report set on the proto ⇒ None all the way through.
    assert lowered["judge_report"] is None
    assert grade.judge_usage is None
    assert grade.judge_transcript is None


def test_proto_grade_judge_report_round_trip():
    """The judge's usage + transcript (JudgeReport) survive proto → Pydantic."""
    transcript = [
        {"role": "system", "content": "You are a grading judge."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "get_db_state", "arguments": {"tables": ["o"]}}],
        },
        {"role": "tool", "content": "{...}", "tool_call_id": "c1"},
    ]
    proto_grade = runner_pb2.Grade(
        binary_pass=True,
        score=0.85,
        components=runner_pb2.GradeComponents(llm_judge=0.85),
        reasons="Judge: ok",
        criterion_results=[
            runner_pb2.CriterionResult(id="c", met=True, score=1.0, justification="ok"),
        ],
        judge_status=runner_pb2.JUDGE_STATUS_COMPLETED,
        judge_report=runner_pb2.JudgeReport(
            calls=3,
            prompt_tokens=4120,
            completion_tokens=318,
            reasoning_tokens=0,
            cost_usd=0.0142,
            tool_calls=4,
            consistency_rejections=2,
            transcript_json=json.dumps(transcript),
        ),
    )
    lowered = lower_wire_grade(proto_grade)

    report = lowered["judge_report"]
    assert report["calls"] == 3
    assert report["prompt_tokens"] == 4120
    assert report["cost_usd"] == pytest.approx(0.0142)
    assert report["tool_calls"] == 4
    assert report["consistency_rejections"] == 2

    grade = _parse_grade_result(lowered)
    assert grade.judge_status is JudgeStatus.COMPLETED
    assert grade.judge_usage is not None
    assert grade.judge_usage.calls == 3
    assert grade.judge_usage.prompt_tokens == 4120
    assert grade.judge_usage.cost_usd == pytest.approx(0.0142)
    assert grade.judge_usage.tool_calls == 4
    assert grade.judge_usage.consistency_rejections == 2
    assert grade.judge_transcript is not None
    assert len(grade.judge_transcript) == 3
    assert grade.judge_transcript[1]["tool_calls"][0]["name"] == "get_db_state"
    assert grade.judge_transcript[2]["tool_call_id"] == "c1"


def test_runner_graded_trace_checks_score_reaches_the_host_grade():
    """A component the runner scored arrives as that score, not as the -1.0 default.

    ``_parse_grade_result`` defaults a missing component key to ``-1.0``, so a
    component the wire lowering forgets is recorded as a score no grader can
    produce rather than as the runner's answer.
    """
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=False,
            score=0.6,
            components=runner_pb2.GradeComponents(
                state_checks=-1.0,
                transcript_rules=-1.0,
                llm_judge=-1.0,
                custom_checks=-1.0,
                trace_checks=0.6,
            ),
        )
    )

    # The parsed grade first: a key the lowering dropped surfaces here as the -1.0
    # default, which is the silent form of the failure and the one worth naming.
    assert _parse_grade_result(lowered).components.trace_checks == pytest.approx(0.6)
    assert lowered["components"]["trace_checks"] == pytest.approx(0.6)


def test_runner_without_the_trace_checks_field_reads_as_not_evaluated():
    """An older runner omits field 5 entirely; proto3 would decode that as a scored 0.0."""
    older_runner_grade = runner_pb2.Grade(
        binary_pass=True,
        score=1.0,
        components=runner_pb2.GradeComponents(
            state_checks=1.0, transcript_rules=-1.0, llm_judge=-1.0, custom_checks=-1.0
        ),
    )
    assert older_runner_grade.components.HasField("trace_checks") is False

    lowered = lower_wire_grade(older_runner_grade)

    assert lowered["components"]["trace_checks"] is None
    assert _parse_grade_result(lowered).components.trace_checks is None


def test_a_runner_scored_zero_is_not_confused_with_an_absent_field():
    """The counterpart: present-and-0.0 is a real failing score and must survive."""
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=False,
            score=0.0,
            components=runner_pb2.GradeComponents(
                state_checks=-1.0, transcript_rules=-1.0, llm_judge=-1.0, custom_checks=-1.0
            ),
        )
    )
    assert lowered["components"]["trace_checks"] is None

    scored_zero = runner_pb2.GradeComponents(
        state_checks=-1.0, transcript_rules=-1.0, llm_judge=-1.0, custom_checks=-1.0
    )
    scored_zero.trace_checks = 0.0
    lowered_zero = lower_wire_grade(
        runner_pb2.Grade(binary_pass=False, score=0.0, components=scored_zero)
    )
    assert lowered_zero["components"]["trace_checks"] == 0.0
    assert _parse_grade_result(lowered_zero).components.trace_checks == 0.0


def test_per_constraint_trace_check_verdicts_reach_grade_yaml(tmp_path):
    """The verdicts the runner reached are what a reviewer reads off the bundle.

    Carried one step past the wire seam the rest of this module pins, because the
    component score alone says a trace check failed without saying which: the
    payload is asserted whole, so a field the wire lowering or the writer drops —
    ``matched_positions`` above all, the only pointer back into
    ``trajectory.yaml`` — fails rather than thinning the record. The summary rides
    beside it: which route the score came from, and which gate shut.
    """
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=False,
            score=0.5,
            components=runner_pb2.GradeComponents(
                state_checks=-1.0,
                transcript_rules=-1.0,
                llm_judge=-1.0,
                custom_checks=-1.0,
                trace_checks=0.5,
            ),
            trace_checks=[
                runner_pb2.TraceConstraintResult(
                    id="lookup_before_denial",
                    kind="before",
                    passed=False,
                    weight=2.0,
                    message="before: no match is ordered before the other side",
                    matched_positions=[2, 4],
                    severity="scored",
                ),
                runner_pb2.TraceConstraintResult(
                    id="no_prefill", kind="absent_before", passed=True, weight=1.0
                ),
                runner_pb2.TraceConstraintResult(
                    id="order_untouched",
                    kind="absent",
                    passed=False,
                    weight=1.0,
                    message="absent: a forbidden call was made",
                    severity="gate",
                ),
            ],
            trace_checks_summary=runner_pb2.TraceChecksSummary(
                winning_path="served_vs_source",
                gate_failed=True,
                failed_gate_ids=["order_untouched"],
                paths=[
                    runner_pb2.TracePathResult(id="served_vs_source", score=0.5, gate_failed=True),
                    runner_pb2.TracePathResult(id="cache_inspector", score=0.25, gate_failed=False),
                ],
            ),
        )
    )

    FileArtifactWriter().write_grade(tmp_path, _parse_grade_result(lowered))

    written = yaml.safe_load((tmp_path / "grade.yaml").read_text())
    assert written["trace_check_results"] == [
        {
            "id": "lookup_before_denial",
            "kind": "before",
            "passed": False,
            "weight": 2.0,
            "severity": "scored",
            "message": "before: no match is ordered before the other side",
            "matched_positions": [2, 4],
        },
        {
            "id": "no_prefill",
            "kind": "absent_before",
            "passed": True,
            "weight": 1.0,
            "severity": "scored",
            "message": "",
            "matched_positions": [],
        },
        {
            "id": "order_untouched",
            "kind": "absent",
            "passed": False,
            "weight": 1.0,
            "severity": "gate",
            "message": "absent: a forbidden call was made",
            "matched_positions": [],
        },
    ]
    assert written["trace_checks_summary"] == {
        "winning_path": "served_vs_source",
        "gate_failed": True,
        "failed_gate_ids": ["order_untouched"],
        "paths": [
            {"id": "served_vs_source", "score": 0.5, "gate_failed": True},
            {"id": "cache_inspector", "score": 0.25, "gate_failed": False},
        ],
    }


def test_a_runner_without_the_trace_checks_summary_leaves_it_absent():
    """Absent stays absent, so a gate cannot open by omission.

    A proto3 message field carries presence where its four scalars would not: a
    runner predating the field sends nothing, and filling that in with an empty
    summary would read as a gate that was evaluated and held. Such a runner rejects
    a pack declaring ``severity`` at ``RegisterTrial``, so an unset severity on a
    verdict is likewise the scored default rather than a value the host invented.
    """
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=True,
            score=1.0,
            trace_checks=[
                runner_pb2.TraceConstraintResult(
                    id="lookup_before_denial", kind="before", passed=True, weight=1.0
                )
            ],
        )
    )

    assert lowered["trace_checks_summary"] is None
    grade = _parse_grade_result(lowered)
    assert grade.trace_checks_summary is None
    assert [item.severity for item in grade.trace_check_results] == ["scored"]


def test_a_trace_checks_summary_the_host_cannot_read_fails_the_grade():
    """The gate decides whether the trial passed, so an unreadable summary rejects.

    Same reasoning as the per-constraint payload beside it, and the same shape of
    skew — a newer runner naming something this engine does not have. Injected onto
    the lowered dict rather than the proto, because the field this engine cannot
    read is by definition one its own stubs cannot encode.
    """
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=False,
            score=0.0,
            trace_checks_summary=runner_pb2.TraceChecksSummary(gate_failed=True),
        )
    )
    lowered["trace_checks_summary"]["gate_severity"] = "fatal"

    with pytest.raises(GradingFailedError, match="Grade.trace_checks_summary"):
        _parse_grade_result(lowered)


def test_a_trace_check_verdict_the_host_cannot_read_fails_the_grade():
    """Version skew rejects rather than degrading into a grade with unknown sub-checks.

    A ``kind`` outside this engine's vocabulary is what a newer runner's payload
    looks like here. The three JSON decode sites beside this one drop what they
    cannot read (#759), which is right where "absent" is a state the harness
    itself produces; for a deterministic per-constraint verdict it is not.
    """
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=False,
            score=0.0,
            trace_checks=[
                runner_pb2.TraceConstraintResult(id="eventually", kind="eventually", weight=1.0)
            ],
        )
    )

    with pytest.raises(GradingFailedError, match="Grade.trace_checks"):
        _parse_grade_result(lowered)
