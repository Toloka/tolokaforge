"""Wire contract of the standalone grader service.

Round-trips a :class:`Grade` through the actual gRPC stack: an in-process
:class:`GraderServiceImpl` receives the request, dispatches to an injected
judge callable, and returns a wire :class:`grader_pb2.Grade` that the
:class:`GrpcGraderClient` decodes back to the same dict shape
``RunnerRPCTrialGrader`` already consumes. If this test fails, the two
plug-in impls have diverged and a downstream conductor swapping graders
will see different Grades.

The service is exercised in-process rather than over a real socket so the
canonical tier remains keyless. The socket path is covered in the
integration tier.
"""

from __future__ import annotations

from concurrent import futures
from contextlib import contextmanager
from unittest.mock import MagicMock

import grpc
import pytest

from tolokaforge.core.models import Grade, GradeComponents
from tolokaforge.grader import grader_pb2, grader_pb2_grpc
from tolokaforge.grader.client import GrpcGraderClient
from tolokaforge.grader.service import GraderServiceImpl

pytestmark = pytest.mark.canonical


@contextmanager
def _running_service(judge_fn):
    """Spin up an in-process gRPC server hosting :class:`GraderServiceImpl`.

    Uses a real (localhost) listener rather than an in-memory channel so the
    grpc-py serialization layer is exercised — that is the thing this test
    exists to lock down.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    grader_pb2_grpc.add_GraderServiceServicer_to_server(
        GraderServiceImpl(judge_fn=judge_fn, logger=MagicMock()), server
    )
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        client = GrpcGraderClient(grader_address=f"localhost:{port}")
        client.connect()
        try:
            yield client
        finally:
            client.close()
    finally:
        server.stop(grace=None)


def test_health_check_reports_serving() -> None:
    def _judge_fn(*_args, **_kwargs):
        raise AssertionError("HealthCheck must not dispatch the judge")

    with _running_service(_judge_fn) as client:
        assert client.health_check() is True


def test_grade_round_trip_matches_judge_verdict() -> None:
    verdict = Grade(
        binary_pass=True,
        score=0.77,
        components=GradeComponents(llm_judge=0.77),
        reasons="rubric met per stub",
    )

    def _judge_fn(dispatch):  # noqa: ARG001
        return verdict

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task:0",
            llm_messages_json='[{"role":"user","content":"hi"}]',
            termination_reason="agent_done",
        )

    assert result["success"] is True
    assert result["grade"]["binary_pass"] is True
    assert result["grade"]["score"] == pytest.approx(0.77)
    assert result["grade"]["reasons"] == "rubric met per stub"
    assert result["grade"]["components"]["llm_judge"] == pytest.approx(0.77)


def test_grade_failure_returns_success_false_with_error() -> None:
    def _judge_fn(*_args, **_kwargs):
        raise RuntimeError("judge blew up")

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task:0",
            llm_messages_json="[]",
            termination_reason="",
        )

    assert result["success"] is False
    assert result["grade"] is None
    assert "judge blew up" in (result["error"] or "")


def test_full_grade_payload_round_trips_through_the_wire() -> None:
    """Every field a live :class:`Grade` may carry survives encode + decode.

    The earlier grader.proto encoder populated only a subset (binary_pass /
    score / reasons / components / custom_checks / criterion_results /
    judge_status). A judge that returned a Grade with trace-check results
    or judge audit fields would have silently lost them on the wire. This
    test constructs a fully-populated Grade, round-trips it through the
    real gRPC stack, and asserts equality of every field the caller reads.
    """
    from tolokaforge.core.models import (
        CriterionResult,
        CustomCheckDetail,
        JudgeInputs,
        JudgeKbGating,
        JudgeStatus,
        JudgeUsage,
        TraceChecksSummary,
        TraceConstraintResult,
        TracePathResult,
    )

    verdict = Grade(
        binary_pass=False,
        score=0.42,
        components=GradeComponents(
            state_checks=0.8,
            transcript_rules=1.0,
            llm_judge=0.3,
            custom_checks=None,
            trace_checks=0.5,
        ),
        reasons="rubric partially met | state hash mismatch",
        state_diff={"orders": {"added": ["a"], "removed": []}},
        custom_checks_details=[
            CustomCheckDetail(
                check_name="c1",
                status="passed",
                score=1.0,
                message="ok",
                details={"n": 3},
            )
        ],
        criterion_results=[
            CriterionResult(id="crit-a", met=False, score=0.4, justification="missing X")
        ],
        judge_status=JudgeStatus.COMPLETED,
        judge_usage=JudgeUsage(
            calls=2,
            prompt_tokens=100,
            completion_tokens=50,
            reasoning_tokens=10,
            cost_usd=0.0123,
            tool_calls=1,
            consistency_rejections=1,
        ),
        judge_transcript=[{"role": "user", "content": "hi"}],
        judge_kb_gating=JudgeKbGating(
            knowledge_search_disabled=True,
            offered=["search_kb"],
            withheld=["search_policy"],
        ),
        judge_inputs=JudgeInputs(
            state_diff_text="orders[1]: open -> shipped",
            read_tools_offered=["read_file", "get_db_state"],
        ),
        judge_custom_prompt=True,
        judge_agent_prompt_included=False,
        trace_check_results=[
            TraceConstraintResult(
                id="t1",
                kind="present",
                passed=False,
                weight=0.5,
                message="anchor missing",
                matched_positions=[3, 7],
                severity="gate",
                undecided=False,
            )
        ],
        trace_checks_summary=TraceChecksSummary(
            winning_path="alt-A",
            gate_failed=True,
            failed_gate_ids=["g1"],
            paths=[TracePathResult(id="alt-A", score=0.6, gate_failed=True)],
        ),
    )

    def _judge_fn(*_args, **_kwargs):
        return verdict

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task:0",
            llm_messages_json="[]",
            termination_reason="",
        )

    assert result["success"] is True
    assert result["no_verdict"] is False
    grade_dict = result["grade"]
    assert grade_dict is not None

    # Every field the wire carries must survive.
    assert grade_dict["state_diff_json"] is not None  # state_diff round-tripped
    assert grade_dict["components"]["trace_checks"] == pytest.approx(0.5)
    assert grade_dict["custom_checks"][0]["check_name"] == "c1"
    assert grade_dict["criterion_results"][0]["id"] == "crit-a"

    # Full judge_report round-trip.
    report = grade_dict["judge_report"]
    assert report["calls"] == 2
    assert report["cost_usd"] == pytest.approx(0.0123)
    assert report["knowledge_search_disabled"] is True
    assert report["kb_tools_withheld"] == ["search_policy"]
    assert report["state_diff_text"] == "orders[1]: open -> shipped"
    assert report["custom_system_prompt"] is True
    assert report["include_agent_system_prompt"] is False
    assert "transcript_json" in report

    # Trace-check round-trip — the sub-messages the earlier encoder dropped.
    assert grade_dict["trace_checks"][0]["severity"] == "gate"
    assert grade_dict["trace_checks"][0]["undecided"] is False
    assert grade_dict["trace_checks_summary"]["winning_path"] == "alt-A"
    assert grade_dict["trace_checks_summary"]["failed_gate_ids"] == ["g1"]
    assert grade_dict["trace_checks_summary"]["paths"][0]["id"] == "alt-A"


def test_none_from_judge_surfaces_as_no_verdict_wire_flag() -> None:
    """The wire distinguishes "nothing to grade" from a grading failure.

    ``TrialGrader.grade`` returns ``None`` for the former and raises
    ``GradingFailedError`` for the latter; the transport must preserve the
    distinction so the seam consumer produces the same top-level outcome
    regardless of dispatch shape."""

    def _judge_fn(*_args, **_kwargs):
        return None

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task:0",
            llm_messages_json="[]",
            termination_reason="",
        )

    assert result["success"] is True
    assert result["no_verdict"] is True
    assert result["grade"] is None
    assert result["error"] is None


def test_wire_message_types_carry_the_full_grade_shape() -> None:
    """Guards against a runner.proto ↔ grader.proto drift on the Grade payload.

    Recurses into every sub-message on ``Grade`` (``GradeComponents``,
    ``JudgeReport``, ``CriterionResult``, ``CustomCheckResult``,
    ``TraceConstraintResult``, ``TraceChecksSummary``, ``TracePathResult``)
    so a nested-field addition on runner.proto fails the guard until the
    grader mirror is updated — keeping the two RPCs interchangeable at
    the plug-in-seam layer, per ADR-0035.
    """
    from tolokaforge.runner import runner_pb2

    def _fields(descriptor) -> set[str]:  # type: ignore[no-untyped-def]
        return {f.name for f in descriptor.fields}

    grader_grade_fields = _fields(grader_pb2.Grade.DESCRIPTOR)
    runner_grade_fields = _fields(runner_pb2.Grade.DESCRIPTOR)
    missing_top = runner_grade_fields - grader_grade_fields
    assert missing_top == set(), (
        "runner.Grade carries fields the grader payload does not mirror: "
        f"{sorted(missing_top)}. Update grader.proto so the wire stays coherent."
    )

    # Match runner sub-messages to grader sub-messages by top-level Grade field
    # name (identical numbering across the two protos). ``JudgeStatus`` is an
    # enum, not a message, so it is field-typed comparably but has no nested
    # descriptor — skip it below.
    grade_message_fields = [
        "components",
        "judge_report",
        "criterion_results",
        "custom_checks",
        "trace_checks",
        "trace_checks_summary",
    ]
    for field_name in grade_message_fields:
        grader_field = grader_pb2.Grade.DESCRIPTOR.fields_by_name[field_name]
        runner_field = runner_pb2.Grade.DESCRIPTOR.fields_by_name[field_name]
        grader_sub_fields = _fields(grader_field.message_type)
        runner_sub_fields = _fields(runner_field.message_type)
        missing_nested = runner_sub_fields - grader_sub_fields
        assert missing_nested == set(), (
            f"grader.proto's {field_name!r} sub-message drops fields present in "
            f"runner.proto: {sorted(missing_nested)}. Mirror the runner shape so "
            "the wire stays coherent — see ADR-0035 § wire alignment."
        )

    # ``trace_checks_summary.paths`` is a nested repeated sub-message — check
    # it too so a runner-side ``TracePathResult`` field addition surfaces here.
    grader_paths_field = grader_pb2.TraceChecksSummary.DESCRIPTOR.fields_by_name[
        "paths"
    ].message_type
    runner_paths_field = runner_pb2.TraceChecksSummary.DESCRIPTOR.fields_by_name[
        "paths"
    ].message_type
    missing_paths = _fields(runner_paths_field) - _fields(grader_paths_field)
    assert missing_paths == set(), (
        "grader.proto's TracePathResult drops fields present in runner.proto: "
        f"{sorted(missing_paths)}. Mirror the runner shape."
    )
