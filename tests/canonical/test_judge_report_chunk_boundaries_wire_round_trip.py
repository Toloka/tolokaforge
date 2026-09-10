"""Wire round-trip lock for ``JudgeReport.chunk_boundaries_json`` on both
runner + grader ``JudgeReport`` messages (field 16, mirrored).

Locks the runner + grader wire encodings symmetrically: a chunked-run
payload survives ``SerializeToString / FromString`` verbatim, and an old
image sending ``""`` (proto3 default for the unset field 16) decodes on
the host to ``None`` via the shared wire helper — the "new host reads
old image" migration path.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.chunk_boundaries_wire import decode_chunk_boundaries
from tolokaforge.grader import grader_pb2
from tolokaforge.runner import runner_pb2

pytestmark = pytest.mark.canonical


class TestRunnerWireRoundTrip:
    def test_chunked_payload_survives_serialize_deserialize(self) -> None:
        response = runner_pb2.GradeTrialResponse(
            success=True,
            grade=runner_pb2.Grade(
                binary_pass=True,
                score=0.5,
                judge_report=runner_pb2.JudgeReport(chunk_boundaries_json='[["a","b"],["c"]]'),
            ),
        )
        wire = response.SerializeToString()
        decoded = runner_pb2.GradeTrialResponse.FromString(wire)
        assert decoded.grade.judge_report.chunk_boundaries_json == '[["a","b"],["c"]]'
        assert decode_chunk_boundaries(decoded.grade.judge_report.chunk_boundaries_json) == [
            ["a", "b"],
            ["c"],
        ]

    def test_empty_string_is_proto3_default_and_decodes_to_none(self) -> None:
        report = runner_pb2.JudgeReport()
        assert report.chunk_boundaries_json == ""
        assert decode_chunk_boundaries(report.chunk_boundaries_json) is None


class TestGraderWireRoundTrip:
    def test_chunked_payload_survives_serialize_deserialize(self) -> None:
        response = grader_pb2.GradeResponse(
            success=True,
            grade=grader_pb2.Grade(
                binary_pass=True,
                score=0.5,
                judge_report=grader_pb2.JudgeReport(chunk_boundaries_json='[["a","b"],["c"]]'),
            ),
        )
        wire = response.SerializeToString()
        decoded = grader_pb2.GradeResponse.FromString(wire)
        assert decoded.grade.judge_report.chunk_boundaries_json == '[["a","b"],["c"]]'
        assert decode_chunk_boundaries(decoded.grade.judge_report.chunk_boundaries_json) == [
            ["a", "b"],
            ["c"],
        ]

    def test_empty_string_is_proto3_default_and_decodes_to_none(self) -> None:
        report = grader_pb2.JudgeReport()
        assert report.chunk_boundaries_json == ""
        assert decode_chunk_boundaries(report.chunk_boundaries_json) is None


class TestFieldNumberParity:
    """Field 16 must be at the same number on both messages (grader.proto
    line 183: ``mirrors runner.JudgeReport EXACTLY, including field
    numbers``). Enforce it against the descriptor so a rename or renumber
    on one side alone fails this test loudly."""

    def test_chunk_boundaries_json_is_field_16_on_both_messages(self) -> None:
        runner_field = runner_pb2.JudgeReport.DESCRIPTOR.fields_by_name["chunk_boundaries_json"]
        grader_field = grader_pb2.JudgeReport.DESCRIPTOR.fields_by_name["chunk_boundaries_json"]
        assert runner_field.number == 16
        assert grader_field.number == 16
