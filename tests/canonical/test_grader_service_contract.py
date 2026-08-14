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


def test_none_from_judge_surfaces_as_no_verdict() -> None:
    def _judge_fn(*_args, **_kwargs):
        return None

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task:0",
            llm_messages_json="[]",
            termination_reason="",
        )

    assert result["success"] is False
    assert result["error"] == "no verdict"


def test_wire_message_types_carry_the_full_grade_shape() -> None:
    """Guards against a runner.proto ↔ grader.proto drift on the Grade payload.

    If a field is added to runner.Grade for a downstream consumer, this test
    fails until the mirror field is added to grader.Grade — keeping the two
    RPCs interchangeable at the plug-in-seam layer, per ADR-0035.
    """
    from tolokaforge.runner import runner_pb2

    grader_fields = {f.name for f in grader_pb2.Grade.DESCRIPTOR.fields}
    runner_fields = {f.name for f in runner_pb2.Grade.DESCRIPTOR.fields}
    # The grader payload must be a superset of every field runner.Grade carries
    # (grader may add its own; runner must not carry a field the grader loses).
    missing = runner_fields - grader_fields
    assert missing == set(), (
        "runner.Grade carries fields the grader payload does not mirror: "
        f"{sorted(missing)}. Update grader.proto so the wire stays coherent."
    )
