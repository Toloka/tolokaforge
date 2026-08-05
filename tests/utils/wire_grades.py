"""Drive a wire ``Grade`` through the real gRPC client lowering.

The host reads a graded trial in two production steps — ``GrpcRunnerClient``
lowers the proto ``Grade`` into a dict, then ``_parse_grade_result`` builds the
Pydantic ``Grade`` from it — and a component can survive one step and be dropped
by the other. Standing in a fake client would hide exactly that, so this supplies
only the gRPC stub and runs the real lowering.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2


def lower_wire_grade(proto_grade: runner_pb2.Grade) -> dict:
    """Return the ``grade`` dict the client lowers ``proto_grade`` into."""
    client = GrpcRunnerClient.__new__(GrpcRunnerClient)
    client.stub = MagicMock()
    client.stub.GradeTrial.return_value = runner_pb2.GradeTrialResponse(
        success=True, grade=proto_grade
    )
    result = client.grade_trial(trial_id="t:0")
    assert result["success"] is True
    return result["grade"]
