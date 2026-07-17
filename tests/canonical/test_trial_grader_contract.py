"""Pin the ``TrialGrader`` Protocol contract.

Every concrete grader must satisfy the Protocol via ``isinstance`` (not
just structural type-hint compatibility) and produce a :class:`Grade`
with the expected shape. This file is the load-bearing contract when
future implementations land (Judge-lift per GH #131, remote grader for
the multi-container future).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.models import Grade, TrialStatus
from tolokaforge.core.trial import TrialSpec
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader, TrialGrader

pytestmark = pytest.mark.canonical


class _StubRuntimeBackendForGrading:
    """Minimal runtime-backend stand-in that satisfies the ``grade_trial``
    surface the grader actually calls."""

    def grade_trial(self, trial_id: str, **_kwargs: object) -> dict[str, object]:
        return {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {"state_checks": 1.0},
                "reasons": "stub",
            },
        }


def _make_grader() -> RunnerRPCTrialGrader:
    return RunnerRPCTrialGrader(
        runtime_backend=_StubRuntimeBackendForGrading(),
        logger=MagicMock(),
    )


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; every implementation
    satisfies it structurally.
    """

    def test_runner_rpc_trial_grader_passes_isinstance(self) -> None:
        assert isinstance(_make_grader(), TrialGrader)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAGrader:
            pass

        assert not isinstance(_NotAGrader(), TrialGrader)

    def test_object_with_matching_shape_passes_isinstance(self) -> None:
        class _DuckGrader:
            def grade(
                self,
                spec: TrialSpec,
                trajectory: object,
                agent_system_prompt: str,
            ) -> Grade:  # pragma: no cover — never called
                return Grade(binary_pass=True, score=1.0)

        assert isinstance(_DuckGrader(), TrialGrader)


class TestGradeShapeParity:
    """The :class:`Grade` returned by the grader matches the shape the
    conductor's grading phase produces. A regression here would silently
    break downstream consumers.
    """

    def test_success_grade_has_required_fields(self) -> None:
        grader = _make_grader()

        grade = grader.grade(
            make_trial_spec(), make_trajectory(status=TrialStatus.COMPLETED), "sys"
        )

        assert isinstance(grade, Grade)
        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert grade.components is not None
        assert grade.components.state_checks == 1.0


class _FakeGradeStub:
    """A gRPC stub stand-in whose ``GradeTrial`` returns a fixed proto ``Grade``,
    so the real proto→dict builder in ``GrpcRunnerClient.grade_trial`` runs."""

    def __init__(self, grade) -> None:
        self._grade = grade

    def GradeTrial(self, request):  # noqa: N802 — matches the gRPC stub method name
        from tolokaforge.runner import runner_pb2

        return runner_pb2.GradeTrialResponse(success=True, grade=self._grade)


def _grade_dict_from_proto(grade) -> dict:
    """Drive the real ``GrpcRunnerClient.grade_trial`` proto→dict mapping."""
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient

    client = GrpcRunnerClient(runner_address="unused:0")
    client.stub = _FakeGradeStub(grade)
    return client.grade_trial("t:0")["grade"]


class TestJudgeKbGatingRoundTrip:
    """The judge's KB gating survives proto → dict → ``Grade`` intact (#465). This
    is the record #451 replay reads: ``knowledge_search_disabled`` is authoritative,
    offered/withheld are audit detail."""

    def test_disabled_gating_round_trips(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(
            knowledge_search_disabled=True,
            kb_tools_offered=[],
            kb_tools_withheld=["search_kb", "search_policy"],
        )
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_kb_gating is not None
        assert parsed.judge_kb_gating.knowledge_search_disabled is True
        assert parsed.judge_kb_gating.offered == []
        assert parsed.judge_kb_gating.withheld == ["search_kb", "search_policy"]

    def test_offered_gating_round_trips_when_not_disabled(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(
            knowledge_search_disabled=False,
            kb_tools_offered=["search_kb", "search_policy"],
            kb_tools_withheld=[],
        )
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_kb_gating is not None
        assert parsed.judge_kb_gating.knowledge_search_disabled is False
        assert parsed.judge_kb_gating.offered == ["search_kb", "search_policy"]
        assert parsed.judge_kb_gating.withheld == []

    def test_absent_judge_report_yields_no_kb_gating(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        grade = runner_pb2.Grade(binary_pass=True, score=1.0)  # no judge_report

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_kb_gating is None
