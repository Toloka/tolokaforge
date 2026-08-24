"""Canonical tests for grading output — compares against golden snapshots."""

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.rubric import build_submit_report_tool
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.runner.models import (
    Criterion,
    Rubric,
    ToolExpectations,
    TranscriptEvaluationResult,
    TranscriptRulesConfig,
)

pytestmark = pytest.mark.canonical

_CALC_RULES = TranscriptRulesConfig(
    must_contain=["391"],
    max_turns=10,
    tool_expectations=ToolExpectations(required_tools=["write_file"]),
)


class TestStateCheckerCanon:
    """Canonical tests for StateChecker grading."""

    def test_minimal_calculation_state_pass(self, canon_snapshot):
        """StateChecker grades a passing state for minimal_calculation."""
        checker = StateChecker()

        jsonpath_assertions = [
            {
                "path": "$.counter",
                "equals": 5,
                "description": "Counter should be 5",
            },
        ]

        final_state = {"counter": 5, "operations": ["add_5"]}

        score, reasons = checker.check_jsonpaths(final_state, jsonpath_assertions)

        snap = canon_snapshot("grading_state_calc")
        snap.assert_match({"score": score, "reasons": "; ".join(reasons)}, "pass_result.json")

    def test_minimal_calculation_state_fail(self, canon_snapshot):
        """StateChecker grades a failing state for minimal_calculation."""
        checker = StateChecker()

        jsonpath_assertions = [
            {
                "path": "$.counter",
                "equals": 5,
                "description": "Counter should be 5",
            },
        ]

        final_state = {"counter": 0, "operations": []}

        score, reasons = checker.check_jsonpaths(final_state, jsonpath_assertions)

        snap = canon_snapshot("grading_state_calc")
        snap.assert_match({"score": score, "reasons": "; ".join(reasons)}, "fail_result.json")


def _transcript_verdict(result: TranscriptEvaluationResult) -> dict:
    """The evaluator's verdict, per sub-check, rounded against float-repr churn."""
    return {
        "score": round(result.score, 6),
        "passed": result.passed,
        "sub_checks": [
            {"rule_type": row.rule_type, "passed": row.passed, "message": row.message}
            for row in result.details
        ],
    }


class TestTranscriptRulesCanon:
    """Canonical tests for the shared ``transcript_rules`` evaluator."""

    def test_minimal_calculation_transcript_pass(self, canon_snapshot):
        """Every declared rule is satisfied for minimal_calculation."""
        timeline = build_timeline(
            [
                ("user", "Please calculate 17 times 23 and save the answer to result.txt."),
                ("assistant", "I'll calculate 17 × 23 = 391 and save it."),
                ("assistant", "Done! The result 391 has been saved to result.txt."),
            ],
            [recorded_call("write_file", arguments={"path": "result.txt", "content": "391"})],
        )

        result = evaluate_transcript_rules(timeline, _CALC_RULES)

        snap = canon_snapshot("grading_transcript_calc")
        snap.assert_match(_transcript_verdict(result), "pass_result.json")

    def test_minimal_calculation_transcript_fail(self, canon_snapshot):
        """One declared rule of three fails — the required tool was never called."""
        timeline = build_timeline(
            [
                ("user", "Please calculate 17 times 23 and save the answer to result.txt."),
                ("assistant", "The answer is 391."),
            ],
        )  # No tool calls

        result = evaluate_transcript_rules(timeline, _CALC_RULES)

        snap = canon_snapshot("grading_transcript_calc")
        snap.assert_match(_transcript_verdict(result), "fail_result.json")


class TestSubmitReportToolCanon:
    """Pin the submit_report tool schema: justification-before-verdict ordering
    plus the trailing VERDICT/SCORE marker descriptions are the schema contract."""

    def test_submit_report_schema_mixed_rubric(self, canon_snapshot):
        rubric = Rubric(
            reference="The correct refund is $328.50.",
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
            ],
        )
        tool = build_submit_report_tool(rubric)
        snap = canon_snapshot("grading_submit_report")
        snap.assert_match(tool, "mixed_rubric_tool.json")
