"""Unit tests for tolokaforge/core/grading/rubric.py — Stage 3 pure helpers.

Pins real behaviour of the three pure rubric functions:
- ``build_submit_report_tool`` — schema derived from criteria (met vs score).
- ``parse_submit_report`` — fail-loud validation + score/met derivation.
- ``aggregate_rubric`` — required-gate + weighted average.
"""

import pytest

from tolokaforge.core.grading.rubric import (
    GRADED_MET_THRESHOLD,
    SUBMIT_REPORT_TOOL_NAME,
    SubmitReportValidationError,
    aggregate_rubric,
    build_submit_report_tool,
    parse_submit_report,
)
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mixed_rubric() -> Rubric:
    """A binary (required) + graded rubric with mixed weights."""
    return Rubric(
        criteria=[
            Criterion(
                id="refund_amount",
                description="Reply quotes the correct refund amount",
                expected="$328.50",
                kind="binary",
                required=True,
                weight=2.0,
            ),
            Criterion(
                id="tone",
                description="Reply is polite and professional",
                kind="graded",
                weight=1.0,
            ),
        ]
    )


def _valid_args(refund_met: bool = True, tone_score: float = 1.0) -> dict:
    return {
        "refund_amount": refund_met,
        "refund_amount_justification": "Quoted $328.50.",
        "tone": tone_score,
        "tone_justification": "Courteous throughout.",
        "reasons": "Overall good.",
    }


# ===================================================================
# build_submit_report_tool
# ===================================================================


class TestBuildSubmitReportTool:
    def test_schema_shape_and_name(self) -> None:
        tool = build_submit_report_tool(_mixed_rubric())
        assert tool["type"] == "function"
        assert tool["function"]["name"] == SUBMIT_REPORT_TOOL_NAME
        params = tool["function"]["parameters"]
        assert params["type"] == "object"

    def test_binary_field_is_boolean_graded_is_bounded_number(self) -> None:
        props = build_submit_report_tool(_mixed_rubric())["function"]["parameters"]["properties"]
        assert props["refund_amount"]["type"] == "boolean"
        assert props["tone"]["type"] == "number"
        assert props["tone"]["minimum"] == 0.0
        assert props["tone"]["maximum"] == 1.0

    def test_description_and_expected_embedded(self) -> None:
        props = build_submit_report_tool(_mixed_rubric())["function"]["parameters"]["properties"]
        assert "Reply quotes the correct refund amount" in props["refund_amount"]["description"]
        assert "$328.50" in props["refund_amount"]["description"]
        assert "Reply is polite and professional" in props["tone"]["description"]

    def test_each_criterion_has_justification_and_overall_reasons_required(self) -> None:
        params = build_submit_report_tool(_mixed_rubric())["function"]["parameters"]
        required = set(params["required"])
        assert {
            "refund_amount",
            "refund_amount_justification",
            "tone",
            "tone_justification",
            "reasons",
        } == required
        assert params["properties"]["reasons"]["type"] == "string"


# ===================================================================
# parse_submit_report — success + derivation
# ===================================================================


class TestParseSubmitReportSuccess:
    def test_binary_and_graded_derivation(self) -> None:
        results = parse_submit_report(_valid_args(refund_met=True, tone_score=0.8), _mixed_rubric())
        by_id = {r.id: r for r in results}
        # binary: met given, score derived 1.0
        assert by_id["refund_amount"].met is True
        assert by_id["refund_amount"].score == 1.0
        # graded: score given, met derived from threshold
        assert by_id["tone"].score == 0.8
        assert by_id["tone"].met is True

    def test_binary_false_scores_zero(self) -> None:
        results = parse_submit_report(_valid_args(refund_met=False), _mixed_rubric())
        refund = next(r for r in results if r.id == "refund_amount")
        assert refund.met is False
        assert refund.score == 0.0

    def test_graded_below_threshold_not_met(self) -> None:
        low = GRADED_MET_THRESHOLD - 0.1
        results = parse_submit_report(_valid_args(tone_score=low), _mixed_rubric())
        tone = next(r for r in results if r.id == "tone")
        assert tone.met is False
        assert tone.score == low


# ===================================================================
# parse_submit_report — fail loud
# ===================================================================


class TestParseSubmitReportFailLoud:
    def test_missing_criterion_raises_named(self) -> None:
        args = _valid_args()
        del args["tone"]
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "tone" in str(exc.value)
        assert "missing" in str(exc.value).lower()

    def test_missing_justification_raises(self) -> None:
        args = _valid_args()
        del args["tone_justification"]
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "justification" in str(exc.value).lower()

    def test_unknown_id_raises_named(self) -> None:
        args = _valid_args()
        args["bogus_criterion"] = True
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "unknown" in str(exc.value).lower()
        assert "bogus_criterion" in str(exc.value)

    def test_out_of_range_score_raises_named(self) -> None:
        args = _valid_args(tone_score=1.5)
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "tone" in str(exc.value)
        assert "range" in str(exc.value).lower()

    def test_wrong_type_binary_raises(self) -> None:
        args = _valid_args()
        args["refund_amount"] = "yes"  # binary expects bool
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "refund_amount" in str(exc.value)
        assert "boolean" in str(exc.value).lower()

    def test_bool_not_accepted_as_graded_score(self) -> None:
        # bool is a subclass of int — must NOT silently pass as a graded score.
        args = _valid_args()
        args["tone"] = True
        with pytest.raises(SubmitReportValidationError) as exc:
            parse_submit_report(args, _mixed_rubric())
        assert "tone" in str(exc.value)
        assert "number" in str(exc.value).lower()


# ===================================================================
# aggregate_rubric
# ===================================================================


class TestAggregateRubric:
    def test_failed_required_forces_binary_fail_despite_high_average(self) -> None:
        # refund_amount (required) fails → gate trips regardless of the average.
        # Required criteria are pure gates: the score is the non-required average
        # (tone perfect → 1.0), but the gate still forces binary_pass False.
        rubric = _mixed_rubric()
        results = parse_submit_report(_valid_args(refund_met=False, tone_score=1.0), rubric)
        agg = aggregate_rubric(rubric, results)
        assert agg.gate_failed is True
        assert agg.binary_pass is False
        assert agg.failed_required_ids == ("refund_amount",)
        # Score = non-required-only average = tone's score (1.0); the required
        # criterion does NOT drag it down — it is a gate, not a contributor.
        assert agg.score == pytest.approx(1.0)

    def test_weighted_average_over_non_required_only(self) -> None:
        # refund_amount is required (a pure gate, excluded); tone is the only
        # non-required criterion, so the score equals tone's score.
        rubric = _mixed_rubric()
        results = parse_submit_report(_valid_args(refund_met=True, tone_score=0.4), rubric)
        agg = aggregate_rubric(rubric, results)
        assert agg.score == pytest.approx(0.4)
        # Gate passes (refund met), but score 0.4 < 0.5 → indicative pass False.
        assert agg.gate_failed is False
        assert agg.binary_pass is False

    def test_met_required_criterion_does_not_change_weighted_average(self) -> None:
        # A met required-binary + one graded → score == the graded's score, even
        # though the required criterion has a large weight. Required = gate only.
        rubric = _mixed_rubric()  # refund required weight 2.0, tone weight 1.0
        results = parse_submit_report(_valid_args(refund_met=True, tone_score=0.7), rubric)
        agg = aggregate_rubric(rubric, results)
        assert agg.gate_failed is False
        assert agg.score == pytest.approx(0.7)

    def test_all_pass_yields_score_one(self) -> None:
        rubric = _mixed_rubric()
        results = parse_submit_report(_valid_args(refund_met=True, tone_score=1.0), rubric)
        agg = aggregate_rubric(rubric, results)
        assert agg.score == pytest.approx(1.0)
        assert agg.binary_pass is True
        assert agg.gate_failed is False

    def test_non_required_failure_does_not_gate(self) -> None:
        # tone is not required; failing it lowers score but must not trip the gate.
        rubric = _mixed_rubric()
        results = parse_submit_report(_valid_args(refund_met=True, tone_score=0.0), rubric)
        agg = aggregate_rubric(rubric, results)
        assert agg.gate_failed is False
        # Non-required-only average = tone's score = 0.0.
        assert agg.score == pytest.approx(0.0)

    def test_all_required_scores_one_when_gate_passes(self) -> None:
        # Every criterion required (all pure gates) → no non-required to average;
        # the score collapses to the gate verdict: 1.0 when all required are met.
        rubric = Rubric(
            criteria=[
                Criterion(id="a", description="x", kind="binary", required=True),
                Criterion(id="b", description="y", kind="binary", required=True),
            ]
        )
        results = parse_submit_report(
            {
                "a": True,
                "a_justification": "j",
                "b": True,
                "b_justification": "j",
                "reasons": "r",
            },
            rubric,
        )
        agg = aggregate_rubric(rubric, results)
        assert agg.gate_failed is False
        assert agg.score == pytest.approx(1.0)
        assert agg.binary_pass is True

    def test_all_required_scores_zero_when_gate_fails(self) -> None:
        # Every criterion required; one fails → gate trips and score collapses to 0.0.
        rubric = Rubric(
            criteria=[
                Criterion(id="a", description="x", kind="binary", required=True),
                Criterion(id="b", description="y", kind="binary", required=True),
            ]
        )
        results = parse_submit_report(
            {
                "a": True,
                "a_justification": "j",
                "b": False,
                "b_justification": "j",
                "reasons": "r",
            },
            rubric,
        )
        agg = aggregate_rubric(rubric, results)
        assert agg.gate_failed is True
        assert agg.failed_required_ids == ("b",)
        assert agg.score == pytest.approx(0.0)
        assert agg.binary_pass is False

    def test_non_positive_non_required_weight_raises(self) -> None:
        # A non-required criterion with zero total weight can't be averaged.
        rubric = Rubric(
            criteria=[
                Criterion(id="only", description="x", kind="graded", weight=0.0),
            ]
        )
        results = parse_submit_report(
            {"only": 0.5, "only_justification": "j", "reasons": "r"}, rubric
        )
        with pytest.raises(SubmitReportValidationError) as exc:
            aggregate_rubric(rubric, results)
        assert "weight" in str(exc.value).lower()


# ===================================================================
# Rubric criterion-id construction guards (runner.models.Rubric)
# ===================================================================


class TestRubricIdValidation:
    def test_duplicate_id_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(
                criteria=[
                    Criterion(id="dup", description="a"),
                    Criterion(id="dup", description="b"),
                ]
            )
        assert "unique" in str(exc.value).lower()
        assert "dup" in str(exc.value)

    def test_reserved_reasons_id_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(criteria=[Criterion(id="reasons", description="a")])
        assert "reserved" in str(exc.value).lower()
        assert "reasons" in str(exc.value)

    def test_id_ending_justification_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(criteria=[Criterion(id="foo_justification", description="a")])
        assert "_justification" in str(exc.value)

    def test_id_cross_collision_with_derived_justification_raises(self) -> None:
        # 'foo' derives 'foo_justification', which collides with the other id.
        with pytest.raises(ValueError) as exc:
            Rubric(
                criteria=[
                    Criterion(id="foo", description="a"),
                    Criterion(id="foo_justification", description="b"),
                ]
            )
        # Either guard (suffix or cross-collision) is acceptable; both name the key.
        assert "foo_justification" in str(exc.value)

    def test_unsafe_id_with_space_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(criteria=[Criterion(id="bad id", description="a")])
        assert "identifier-safe" in str(exc.value)

    def test_empty_id_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(criteria=[Criterion(id="", description="a")])
        assert "identifier-safe" in str(exc.value)

    def test_leading_digit_id_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            Rubric(criteria=[Criterion(id="1abc", description="a")])
        assert "identifier-safe" in str(exc.value)

    def test_valid_ids_accepted(self) -> None:
        rubric = Rubric(
            criteria=[
                Criterion(id="refund_amount", description="a"),
                Criterion(id="Tone2", description="b"),
            ]
        )
        assert [c.id for c in rubric.criteria] == ["refund_amount", "Tone2"]
