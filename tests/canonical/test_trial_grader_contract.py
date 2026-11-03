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
from tolokaforge.runner.models import JudgeCustomization

pytestmark = pytest.mark.canonical


class _StubRunnerClientForGrading:
    """Minimal ``GrpcRunnerClient`` stand-in that satisfies the ``grade_trial``
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
        runner_address="stub:0",
        logger=MagicMock(),
        runner_client=_StubRunnerClientForGrading(),
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
    """The judge's KB surface survives proto → dict → ``Grade`` intact: which KB tools
    it was offered and which its customization withheld."""

    @pytest.mark.parametrize(
        ("offered", "withheld"),
        [([], ["search_kb", "search_policy"]), (["search_kb", "search_policy"], [])],
        ids=["withheld", "offered"],
    )
    def test_gating_round_trips(self, offered: list[str], withheld: list[str]) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(kb_tools_offered=offered, kb_tools_withheld=withheld)
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_kb_gating is not None
        assert parsed.judge_kb_gating.offered == offered
        assert parsed.judge_kb_gating.withheld == withheld

    def test_absent_judge_report_yields_no_kb_gating(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        grade = runner_pb2.Grade(binary_pass=True, score=1.0)  # no judge_report

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_kb_gating is None


class TestJudgeInputsRoundTrip:
    """The judge's non-derivable ``run()`` inputs — the exact ``state_diff`` string
    and the non-KB read-tool surface — survive proto → dict → ``Grade`` intact.
    This is the record offline replay reads to rebuild the judge's opening message
    and declare which live backends to shim."""

    def test_state_diff_and_read_tools_round_trip(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(
            state_diff_text="orders[1]: status open -> shipped",
            read_tools_offered=["get_db_state", "query_db", "read_file"],
        )
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_inputs is not None
        assert parsed.judge_inputs.state_diff_text == "orders[1]: status open -> shipped"
        assert parsed.judge_inputs.read_tools_offered == ["get_db_state", "query_db", "read_file"]

    def test_empty_state_diff_text_maps_to_none(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        # A judge that ran but built no diff: the wire carries "" (proto3 string
        # default), which must reconstruct as None, not the empty string.
        report = runner_pb2.JudgeReport(state_diff_text="", read_tools_offered=["read_file"])
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_inputs is not None
        assert parsed.judge_inputs.state_diff_text is None
        assert parsed.judge_inputs.read_tools_offered == ["read_file"]

    @pytest.mark.parametrize(
        ("sent", "received"),
        [
            (
                JudgeCustomization(
                    evidence={"db": False, "tool_results": True}, system_prompt="Strict."
                ).model_dump_json(),
                JudgeCustomization(
                    evidence={"db": False, "tool_results": True}, system_prompt="Strict."
                ),
            ),
            ("", None),
        ],
        ids=["recorded", "runner-predating-the-record"],
    )
    def test_the_customization_record_round_trips(
        self, sent: str, received: JudgeCustomization | None
    ) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(customization_json=sent)
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_inputs is not None
        assert parsed.judge_inputs.customization == received

    def test_the_customization_record_round_trips_over_the_grader_wire(self) -> None:
        from tolokaforge.core.models import JudgeInputs
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.grader.client import _grade_from_wire
        from tolokaforge.grader.service import _grade_to_wire

        source = Grade(
            binary_pass=True,
            score=1.0,
            judge_inputs=JudgeInputs(
                customization=JudgeCustomization(evidence={"knowledge_search": False})
            ),
        )

        parsed = _parse_grade_result(_grade_from_wire(_grade_to_wire(source)))

        assert parsed.judge_inputs == source.judge_inputs

    def test_an_unreadable_customization_record_is_a_grading_failure(self) -> None:
        from tolokaforge.core.trial_grader import GradingFailedError, _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(calls=1, customization_json='{"evidence": {"db": "vibes"}}')
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        with pytest.raises(GradingFailedError, match="customization_json payload is not readable"):
            _parse_grade_result(_grade_dict_from_proto(grade))

    def test_absent_judge_report_yields_no_judge_inputs(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        grade = runner_pb2.Grade(binary_pass=True, score=1.0)  # no judge_report

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_inputs is None


class TestJudgeBilledCostRoundTrip:
    """The judge's stated charge survives both wires into ``grade.yaml``'s
    ``judge_usage``. Presence carries meaning: an absent field is "not every judge
    call stated one" (or a runner predating field 17) and must read back ``None``,
    never proto3's 0.0."""

    def test_runner_wire_round_trips_the_charge(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(calls=2, cost_usd=0.002, billed_cost_usd=0.0021)
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_usage is not None
        assert parsed.judge_usage.cost_usd == 0.002
        assert parsed.judge_usage.billed_cost_usd == 0.0021

    def test_a_zero_charge_is_present_not_absent(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(calls=1, billed_cost_usd=0.0)
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        parsed = _parse_grade_result(_grade_dict_from_proto(grade))

        assert parsed.judge_usage is not None
        assert parsed.judge_usage.billed_cost_usd == 0.0

    def test_unset_fields_read_back_unknown(self) -> None:
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.runner import runner_pb2

        report = runner_pb2.JudgeReport(calls=1, cost_usd=0.001)  # field 17 left unset
        grade = runner_pb2.Grade(binary_pass=True, score=1.0, judge_report=report)

        grade_dict = _grade_dict_from_proto(grade)
        assert "billed_cost_usd" not in grade_dict["judge_report"]
        parsed = _parse_grade_result(grade_dict)

        assert parsed.judge_usage is not None
        assert parsed.judge_usage.billed_cost_usd is None

    @pytest.mark.parametrize("billed", [0.0021, 0.0, None])
    def test_grader_wire_round_trips_the_charge(self, billed: float | None) -> None:
        from tolokaforge.core.models import JudgeUsage
        from tolokaforge.core.trial_grader import _parse_grade_result
        from tolokaforge.grader.client import _grade_from_wire
        from tolokaforge.grader.service import _grade_to_wire

        source = Grade(
            binary_pass=True,
            score=1.0,
            judge_usage=JudgeUsage(calls=2, cost_usd=0.002, billed_cost_usd=billed),
        )

        parsed = _parse_grade_result(_grade_from_wire(_grade_to_wire(source)))

        assert parsed.judge_usage is not None
        assert parsed.judge_usage.billed_cost_usd == billed

    @pytest.mark.parametrize("billed", [0.0042, None])
    def test_the_grader_dispatch_carries_the_charge_into_the_grade(
        self, billed: float | None
    ) -> None:
        """The standalone grader's own assembly (``composite_dispatch._build_grade``) keeps
        the judge's charge, so the grader wire above has something to carry."""
        from tolokaforge.core.grading.composite import JudgeGrading
        from tolokaforge.core.grading.composite_fold import CompositeFoldResult
        from tolokaforge.core.grading.grade_components import CompositeGradeComponents
        from tolokaforge.core.grading.judge_result import JudgeResult
        from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
        from tolokaforge.core.grading.judge_result import JudgeUsage as JudgeRunUsage
        from tolokaforge.core.models import JudgeStatus
        from tolokaforge.grader.composite_dispatch import _build_grade
        from tolokaforge.runner.models import TraceChecksResult

        grade = _build_grade(
            fold_result=CompositeFoldResult(
                score=1.0,
                binary_pass=True,
                verdict_reason=None,
                judge_component=1.0,
                state_checks_component=None,
                inert_weight_reason=None,
                reasons="ok",
            ),
            components=CompositeGradeComponents(),
            custom_check_results=[],
            trace_result=TraceChecksResult(),
            judge_grading=JudgeGrading(
                result=JudgeResult(
                    status=JudgeRunStatus.COMPLETED,
                    usage=JudgeRunUsage(calls=2, cost_usd=0.004, billed_cost_usd=billed),
                    reasons="ok",
                    score=1.0,
                ),
                customization=JudgeCustomization(),
            ),
            judge_status=JudgeStatus.COMPLETED,
        )

        assert grade.judge_usage is not None
        assert grade.judge_usage.cost_usd == 0.004
        assert grade.judge_usage.billed_cost_usd == billed

    def test_ondisk_grade_without_the_field_loads_as_unknown(self) -> None:
        grade = Grade.model_validate(
            {
                "binary_pass": True,
                "score": 1.0,
                "judge_usage": {"calls": 1, "cost_usd": 0.001},
            }
        )

        assert grade.judge_usage is not None
        assert grade.judge_usage.billed_cost_usd is None
