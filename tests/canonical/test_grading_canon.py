"""Canonical tests for grading output — compares against golden snapshots."""

import pytest

from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.core.grading.transcript import TranscriptChecker
from tolokaforge.core.models import Message, MessageRole

pytestmark = pytest.mark.canonical


class TestStateCheckerCanon:
    """Canonical tests for StateChecker grading."""

    def test_minimal_calculation_state_pass(self, canon_snapshot):
        """StateChecker grades a passing state for minimal_calculation."""
        checker = StateChecker()

        jsonpath_assertions = [
            {
                "path": "$.counter",
                "op": "eq",
                "expected": 5,
                "required": True,
                "reason": "Counter should be 5",
            },
        ]

        final_state = {"counter": 5, "operations": ["add_5"]}

        score, reasons = checker.grade(
            state=final_state,
            jsonpath_assertions=jsonpath_assertions,
        )

        snap = canon_snapshot("grading_state_calc")
        snap.assert_match({"score": score, "reasons": reasons}, "pass_result.json")

    def test_minimal_calculation_state_fail(self, canon_snapshot):
        """StateChecker grades a failing state for minimal_calculation."""
        checker = StateChecker()

        jsonpath_assertions = [
            {
                "path": "$.counter",
                "op": "eq",
                "expected": 5,
                "required": True,
                "reason": "Counter should be 5",
            },
        ]

        final_state = {"counter": 0, "operations": []}

        score, reasons = checker.grade(
            state=final_state,
            jsonpath_assertions=jsonpath_assertions,
        )

        snap = canon_snapshot("grading_state_calc")
        snap.assert_match({"score": score, "reasons": reasons}, "fail_result.json")


class TestTranscriptCheckerCanon:
    """Canonical tests for TranscriptChecker grading."""

    def test_minimal_calculation_transcript_pass(self, canon_snapshot):
        """TranscriptChecker grades a passing transcript for minimal_calculation."""
        checker = TranscriptChecker()

        messages = [
            Message(
                role=MessageRole.USER,
                content="Please calculate 17 times 23 and save the answer to result.txt.",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="I'll calculate 17 × 23 = 391 and save it.",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="Done! The result 391 has been saved to result.txt.",
            ),
        ]

        tool_log = [
            {
                "tool": "write_file",
                "success": True,
                "args": {"path": "result.txt", "content": "391"},
            },
        ]

        score, reasons = checker.grade(
            messages=messages,
            tool_log=tool_log,
            must_contain=["391"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["write_file"],
            disallowed_tools=[],
        )

        snap = canon_snapshot("grading_transcript_calc")
        snap.assert_match({"score": score, "reasons": reasons}, "pass_result.json")

    def test_minimal_calculation_transcript_fail(self, canon_snapshot):
        """TranscriptChecker grades a failing transcript — missing required tool."""
        checker = TranscriptChecker()

        messages = [
            Message(
                role=MessageRole.USER,
                content="Please calculate 17 times 23 and save the answer to result.txt.",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="The answer is 391.",
            ),
        ]

        tool_log = []  # No tool calls

        score, reasons = checker.grade(
            messages=messages,
            tool_log=tool_log,
            must_contain=["391"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["write_file"],
            disallowed_tools=[],
        )

        snap = canon_snapshot("grading_transcript_calc")
        snap.assert_match({"score": score, "reasons": reasons}, "fail_result.json")
