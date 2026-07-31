"""Tests for transcript-based grading rules"""

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript import TranscriptChecker
from tolokaforge.core.models import Message, MessageRole, ToolCall

pytestmark = pytest.mark.unit


def _bundle_timeline(tool_output: str):
    """A trial re-graded from its recorded bundle: no records, results on the messages.

    ``tool_log`` is not written to ``trajectory.yaml``, so this is the state every
    re-grade of a recorded trial is in — and the tool's output survives only on the
    ``role: tool`` message.
    """
    call = ToolCall(id="call_A", name="lookup_customer", arguments={"customer_id": "42"})
    return build_trial_timeline(
        [
            Message(role=MessageRole.ASSISTANT, content="Looking that up.", tool_calls=[call]),
            Message(role=MessageRole.TOOL, content=tool_output, tool_call_id="call_A"),
        ],
        [],
        None,
    )


@pytest.mark.unit
class TestMustContain:
    """Test must_contain phrase checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    @pytest.fixture
    def timeline(self):
        return build_timeline(
            [
                ("user", "Hello, I need help with MMS"),
                ("assistant", "I can help you enable MMS service"),
                ("user", "Please proceed"),
                ("assistant", "MMS has been enabled. Is there anything else?"),
            ]
        )

    def test_all_phrases_present(self, checker, timeline):
        """Test when all required phrases are present"""
        phrases = ["MMS", "enabled"]
        score, reasons = checker.check_must_contain(timeline, phrases)
        assert score == 1.0
        assert len(reasons) == 0

    def test_some_phrases_missing(self, checker, timeline):
        """Test when some phrases are missing"""
        phrases = ["MMS", "enabled", "confirmed"]
        score, reasons = checker.check_must_contain(timeline, phrases)
        assert score == pytest.approx(2.0 / 3.0)
        assert len(reasons) == 1
        assert "confirmed" in reasons[0]

    def test_all_phrases_missing(self, checker, timeline):
        """Test when all phrases are missing"""
        phrases = ["refund", "cancelled"]
        score, reasons = checker.check_must_contain(timeline, phrases)
        assert score == 0.0
        assert len(reasons) == 2

    def test_a_tool_result_is_searchable(self, checker):
        """A phrase a tool returned counts as present: the record is the authority
        on tool output, and the message-side copy of it is not on the timeline."""
        timeline = build_timeline(
            [("assistant", "Checking your account.")],
            [recorded_call("lookup_account", sequence=0, output="plan: unlimited-MMS")],
        )
        score, reasons = checker.check_must_contain(timeline, ["unlimited-MMS"])
        assert score == 1.0
        assert reasons == []

    def test_a_tool_result_is_searchable_on_a_re_graded_bundle(self, checker):
        """With no record view the tool output is still on the ``role: tool``
        message, so a phrase that only a tool returned is still found."""
        timeline = _bundle_timeline("plan: unlimited-MMS")

        assert timeline.records_present is False
        assert checker.check_must_contain(timeline, ["unlimited-MMS"]) == (1.0, [])

    def test_a_harness_annotation_is_not_searchable(self, checker):
        """``role: system`` turns are harness text, not trial text, so an author
        cannot satisfy a phrase rule with a termination notice."""
        timeline = build_trial_timeline(
            [
                Message(role=MessageRole.ASSISTANT, content="Done."),
                Message(role=MessageRole.SYSTEM, content="Trial terminated: max turns reached"),
            ],
            [],
            None,
        )
        score, reasons = checker.check_must_contain(timeline, ["max turns reached"])
        assert score == 0.0
        assert reasons == ["Missing required phrase: 'max turns reached'"]


@pytest.mark.unit
class TestDisallowRegex:
    """Test disallow_regex pattern checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    def test_no_violations(self, checker):
        """Test when no disallowed patterns are found"""
        timeline = build_timeline([("assistant", "I will help you with your account")])
        patterns = ["(?i)factory.?reset", "(?i)wipe.*account"]
        score, reasons = checker.check_disallowed_regex(timeline, patterns)
        assert score == 1.0
        assert len(reasons) == 0

    def test_one_violation(self, checker):
        """Test when one disallowed pattern is found"""
        timeline = build_timeline([("assistant", "Let me perform a factory reset")])
        patterns = ["(?i)factory.?reset"]
        score, reasons = checker.check_disallowed_regex(timeline, patterns)
        assert score == 0.0
        assert len(reasons) == 1
        assert "factory.?reset" in reasons[0]

    def test_a_pattern_a_tool_returned_violates_on_a_re_graded_bundle(self, checker):
        """The leak a `disallow_regex` exists to catch usually arrives in a tool's
        output. Re-grading the bundle carries no records, and a search that skipped
        the ``role: tool`` messages would clear the pattern while the SSN sat on
        disk — a silent pass, in the agent's favour."""
        timeline = _bundle_timeline("SSN 123-45-6789 for customer 42")

        assert timeline.records_present is False
        score, reasons = checker.check_disallowed_regex(timeline, [r"\d{3}-\d{2}-\d{4}"])
        assert score == 0.0
        assert reasons == ["Disallowed pattern '\\d{3}-\\d{2}-\\d{4}' found: ['123-45-6789']"]


@pytest.mark.unit
class TestMaxTurns:
    """Test max_turns checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    def test_under_limit(self, checker):
        """Test when turns are under the limit"""
        timeline = build_timeline(
            [
                ("user", "Hello"),
                ("assistant", "Hi"),
                ("user", "Help me"),
                ("assistant", "Sure"),
            ]
        )
        score, reason = checker.check_max_turns(timeline, max_turns=5)
        assert score == 1.0
        assert reason == ""

    def test_over_limit(self, checker):
        """Test when turns exceed the limit"""
        timeline = build_timeline(
            [
                ("assistant", "Turn 1"),
                ("user", "User"),
                ("assistant", "Turn 2"),
                ("user", "User"),
                ("assistant", "Turn 3"),
            ]
        )
        score, reason = checker.check_max_turns(timeline, max_turns=2)
        assert score == 0.0
        assert "Exceeded max turns" in reason
        assert "3 > 2" in reason


@pytest.mark.unit
class TestToolExpectations:
    """Test tool usage expectations"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    @pytest.fixture
    def timeline(self):
        return build_timeline(
            [("assistant", "Looking that up.")],
            [
                recorded_call("db_query", sequence=0),
                recorded_call("db_update", sequence=1),
                recorded_call("search_kb", sequence=2),
            ],
        )

    def test_all_required_tools_used(self, checker, timeline):
        """Test when all required tools are used"""
        required = ["db_query", "db_update"]
        score, reasons = checker.check_tool_expectations(timeline, required, None)
        assert score == 1.0
        assert len(reasons) == 0

    def test_missing_required_tool(self, checker, timeline):
        """Test when a required tool is missing"""
        required = ["db_query", "db_update", "write_file"]
        score, reasons = checker.check_tool_expectations(timeline, required, None)
        assert score == 0.5
        assert len(reasons) == 1
        assert "write_file" in reasons[0]

    def test_disallowed_tool_used(self, checker, timeline):
        """Test when a disallowed tool is used"""
        disallowed = ["search_kb"]
        score, reasons = checker.check_tool_expectations(timeline, None, disallowed)
        assert score == 0.0
        assert len(reasons) == 1
        assert "search_kb" in reasons[0]

    def test_a_call_that_never_ran_is_not_a_use(self, checker):
        """A terminating turn's tool calls never reach the substrate, so they
        satisfy no required tool and violate no disallowed one.

        The trial recorded another call, which is what licenses reading "declared,
        not recorded" as "did not run".
        """
        timeline = build_timeline(
            [("assistant", "I will look that up next.")],
            [recorded_call("search_kb", sequence=0)],
            unexecuted=[ToolCall(id="never_ran", name="db_query", arguments={})],
        )
        required_score, required_reasons = checker.check_tool_expectations(
            timeline, ["db_query"], None
        )
        assert required_score == 0.5
        assert required_reasons == ["Missing required tools: db_query"]
        disallowed_score, disallowed_reasons = checker.check_tool_expectations(
            timeline, None, ["db_query"]
        )
        assert disallowed_score == 1.0
        assert disallowed_reasons == []

    def test_a_records_less_timeline_fails_instead_of_reporting_no_use(self, checker):
        """`BaseAdapter.grade` on a recorded bundle builds exactly this timeline —
        `tool_log` is not written to `trajectory.yaml` — so reading "no record" as
        "did not run" passed every `disallowed_tools` check unconditionally."""
        timeline = build_timeline(
            [("assistant", "Dropping the table.")],
            unexecuted=[ToolCall(id="c1", name="drop_table", arguments={})],
        )

        assert timeline.records_present is False
        for required, disallowed in ((["drop_table"], None), (None, ["drop_table"])):
            score, reasons = checker.check_tool_expectations(timeline, required, disallowed)
            assert score == 0.0
            assert reasons == [
                "Tool expectations unevaluatable: the trial carries no tool-call record, "
                "so whether it ran the calls it declared (drop_table) is unknown"
            ]

    def test_a_records_less_timeline_with_no_expectations_declared_is_untouched(self, checker):
        """Nothing is claimed about tools, so nothing is unevaluatable."""
        timeline = build_timeline(
            [("assistant", "Dropping the table.")],
            unexecuted=[ToolCall(id="c1", name="drop_table", arguments={})],
        )

        assert checker.check_tool_expectations(timeline, None, None) == (1.0, [])


@pytest.mark.unit
class TestTranscriptGrading:
    """Test complete transcript grading"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    @pytest.fixture
    def good_timeline(self):
        return build_timeline(
            [
                ("user", "I need help with MMS"),
                ("assistant", "I'll help you enable MMS"),
                ("user", "Thanks"),
                ("assistant", "MMS is now enabled"),
            ],
            [
                recorded_call("db_query", sequence=0),
                recorded_call("db_update", sequence=1),
            ],
        )

    def test_all_rules_pass(self, checker, good_timeline):
        """Test when all transcript rules pass"""
        score, reasons = checker.grade(
            timeline=good_timeline,
            must_contain=["MMS", "enabled"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["db_query", "db_update"],
            disallowed_tools=[],
        )
        assert score == 1.0
        assert "All checks passed" in reasons

    def test_some_rules_fail(self, checker, good_timeline):
        """Test when some rules fail"""
        score, reasons = checker.grade(
            timeline=good_timeline,
            must_contain=["MMS", "enabled", "confirmed"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["db_query", "db_update"],
            disallowed_tools=[],
        )
        assert score < 1.0
        assert score > 0.0
        assert "confirmed" in reasons.lower()

    def test_critical_violation(self, checker):
        """Test with critical violation (disallowed pattern)"""
        timeline = build_timeline(
            [("assistant", "I will factory reset your device")],
            [
                recorded_call("db_query", sequence=0),
                recorded_call("db_update", sequence=1),
            ],
        )
        score, reasons = checker.grade(
            timeline=timeline,
            must_contain=[],
            disallow_regex=["(?i)factory.?reset"],
            max_turns=10,
            required_tools=[],
            disallowed_tools=[],
        )
        assert score < 1.0


@pytest.mark.unit
class TestToolExpectationsReasonsAreDeterministic:
    """Pins the contract: tool-expectation reason strings are sorted/stable so
    they don't drift run-to-run from Python set repr ordering. Snapshot tests
    (and downstream parsing) rely on this stability.
    """

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    def test_missing_tools_reason_is_sorted(self, checker):
        """Reason text lists missing tools in alphabetical order — same across runs."""
        score, reasons = checker.check_tool_expectations(
            timeline=build_timeline(),
            required_tools=["zebra_tool", "alpha_tool", "mango_tool"],
            disallowed_tools=None,
        )
        assert len(reasons) == 1
        # Sorted alphabetically.
        assert reasons[0] == "Missing required tools: alpha_tool, mango_tool, zebra_tool"
        # No set-repr punctuation leaks.
        assert "{" not in reasons[0] and "}" not in reasons[0]
        assert "'" not in reasons[0]

    def test_violations_reason_is_sorted(self, checker):
        """Reason text lists disallowed-tool violations in alphabetical order."""
        timeline = build_timeline(
            [("assistant", "Working on it.")],
            [
                recorded_call("zeta", sequence=0),
                recorded_call("alpha", sequence=1),
                recorded_call("mike", sequence=2),
            ],
        )
        score, reasons = checker.check_tool_expectations(
            timeline=timeline,
            required_tools=None,
            disallowed_tools=["mike", "alpha", "zeta"],
        )
        assert len(reasons) == 1
        assert reasons[0] == "Used disallowed tools: alpha, mike, zeta"
        assert "{" not in reasons[0] and "}" not in reasons[0]

    def test_reasons_are_stable_across_repeated_calls(self, checker):
        """Calling the same scenario twice must yield byte-identical reasons,
        regardless of underlying set iteration order."""
        kwargs = {
            "timeline": build_timeline(),
            "required_tools": ["b_tool", "a_tool", "c_tool"],
            "disallowed_tools": None,
        }
        _, r1 = checker.check_tool_expectations(**kwargs)
        _, r2 = checker.check_tool_expectations(**kwargs)
        assert r1 == r2
