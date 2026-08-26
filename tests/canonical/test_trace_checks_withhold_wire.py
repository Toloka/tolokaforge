"""``TraceConstraintResult.withheld`` on the runner and grader wires.

Three claims, over two independent proto shapes:

1. ``runner_pb2.TraceConstraintResult(withheld=True)`` serialises and reads back
   with ``withheld == True``. Locks that the runner-side wire actually carries
   the field an author's ``on_missing: withhold`` opts into.
2. ``grader_pb2.TraceConstraintResult(withheld=True)`` does the same. Locks the
   grader mirror per ADR-0038, so a grade crossing the grader substrate
   preserves the withheld verdict rather than defaulting it.
3. A byte stream missing the ``withheld`` field decodes with ``withheld ==
   False`` on both shapes. This is the silent-default response-direction the
   docs/GRADING.md version-lock row describes — an old runner emits no field,
   a new decoder reads the proto3 scalar default, and a pack that never
   declared ``on_missing: withhold`` sees the pre-fix behaviour.
"""

from __future__ import annotations

import pytest

from tolokaforge.grader import grader_pb2
from tolokaforge.runner import runner_pb2

pytestmark = pytest.mark.canonical


def test_the_runner_wire_round_trips_withheld_true():
    message = runner_pb2.TraceConstraintResult(
        id="kb_before_reply",
        kind="before",
        passed=False,
        weight=1.0,
        withheld=True,
    )
    payload = message.SerializeToString()

    decoded = runner_pb2.TraceConstraintResult.FromString(payload)

    assert decoded.withheld is True
    assert decoded.passed is False


def test_the_grader_wire_round_trips_withheld_true():
    message = grader_pb2.TraceConstraintResult(
        id="kb_before_reply",
        kind="before",
        passed=False,
        weight=1.0,
        withheld=True,
    )
    payload = message.SerializeToString()

    decoded = grader_pb2.TraceConstraintResult.FromString(payload)

    assert decoded.withheld is True
    assert decoded.passed is False


def test_a_message_lacking_the_field_reads_back_withheld_false_on_both_shapes():
    """The silent-default the version-lock row locks.

    A new decoder reading a message an old runner emitted — one that never
    wrote the field — reads ``withheld == False`` (the proto3 scalar default).
    This is the correct pre-fix behaviour for any pack that never declared
    ``on_missing: withhold`` and is what the docs version-lock row names as
    the silent response-direction skew.
    """
    for shape in (runner_pb2, grader_pb2):
        old = shape.TraceConstraintResult(
            id="pre_withhold_result",
            kind="present",
            passed=True,
            weight=1.0,
        )
        payload = old.SerializeToString()

        decoded = shape.TraceConstraintResult.FromString(payload)

        assert decoded.withheld is False
        assert decoded.passed is True


def test_the_grader_shape_mirrors_the_runner_shape_field_by_field():
    """The grader mirror admits the withheld field alongside every other one.

    Reads the descriptors rather than the values — the parity guard at
    ``tests/canonical/test_grader_service_contract.py::
    test_wire_message_types_carry_the_full_grade_shape`` sweeps every
    sub-message; this row pins the single ``TraceConstraintResult`` mirror as
    an isolated assertion the diff of a proto-only PR reads at a glance.
    """
    runner_fields = {field.name for field in runner_pb2.TraceConstraintResult.DESCRIPTOR.fields}
    grader_fields = {field.name for field in grader_pb2.TraceConstraintResult.DESCRIPTOR.fields}

    assert "withheld" in runner_fields
    assert "withheld" in grader_fields
    assert runner_fields == grader_fields
