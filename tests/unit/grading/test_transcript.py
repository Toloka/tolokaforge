"""Tests for transcript-based grading rules"""

import pytest

from tolokaforge.core.grading.transcript import TranscriptChecker
from tolokaforge.core.models import Message, MessageRole

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestMustContain:
    """Test must_contain phrase checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    @pytest.fixture
    def messages(self):
        return [
            Message(role=MessageRole.USER, content="Hello, I need help with MMS"),
            Message(role=MessageRole.ASSISTANT, content="I can help you enable MMS service"),
            Message(role=MessageRole.USER, content="Please proceed"),
            Message(
                role=MessageRole.ASSISTANT, content="MMS has been enabled. Is there anything else?"
            ),
        ]

    def test_all_phrases_present(self, checker, messages):
        """Test when all required phrases are present"""
        phrases = ["MMS", "enabled"]
        score, reasons = checker.check_must_contain(messages, phrases)
        assert score == 1.0
        assert len(reasons) == 0

    def test_some_phrases_missing(self, checker, messages):
        """Test when some phrases are missing"""
        phrases = ["MMS", "enabled", "confirmed"]
        score, reasons = checker.check_must_contain(messages, phrases)
        assert score == pytest.approx(2.0 / 3.0)
        assert len(reasons) == 1
        assert "confirmed" in reasons[0]

    def test_all_phrases_missing(self, checker, messages):
        """Test when all phrases are missing"""
        phrases = ["refund", "cancelled"]
        score, reasons = checker.check_must_contain(messages, phrases)
        assert score == 0.0
        assert len(reasons) == 2


@pytest.mark.unit
class TestDisallowRegex:
    """Test disallow_regex pattern checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    def test_no_violations(self, checker):
        """Test when no disallowed patterns are found"""
        messages = [
            Message(role=MessageRole.ASSISTANT, content="I will help you with your account")
        ]
        patterns = ["(?i)factory.?reset", "(?i)wipe.*account"]
        score, reasons = checker.check_disallowed_regex(messages, patterns)
        assert score == 1.0
        assert len(reasons) == 0

    def test_one_violation(self, checker):
        """Test when one disallowed pattern is found"""
        messages = [Message(role=MessageRole.ASSISTANT, content="Let me perform a factory reset")]
        patterns = ["(?i)factory.?reset"]
        score, reasons = checker.check_disallowed_regex(messages, patterns)
        assert score == 0.0
        assert len(reasons) == 1
        assert "factory.?reset" in reasons[0]


@pytest.mark.unit
class TestMaxTurns:
    """Test max_turns checking"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    def test_under_limit(self, checker):
        """Test when turns are under the limit"""
        messages = [
            Message(role=MessageRole.USER, content="Hello"),
            Message(role=MessageRole.ASSISTANT, content="Hi"),
            Message(role=MessageRole.USER, content="Help me"),
            Message(role=MessageRole.ASSISTANT, content="Sure"),
        ]
        score, reason = checker.check_max_turns(messages, max_turns=5)
        assert score == 1.0
        assert reason == ""

    def test_over_limit(self, checker):
        """Test when turns exceed the limit"""
        messages = [
            Message(role=MessageRole.ASSISTANT, content="Turn 1"),
            Message(role=MessageRole.USER, content="User"),
            Message(role=MessageRole.ASSISTANT, content="Turn 2"),
            Message(role=MessageRole.USER, content="User"),
            Message(role=MessageRole.ASSISTANT, content="Turn 3"),
        ]
        score, reason = checker.check_max_turns(messages, max_turns=2)
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
    def tool_log(self):
        return [
            {"tool": "db_query", "success": True},
            {"tool": "db_update", "success": True},
            {"tool": "search_kb", "success": True},
        ]

    def test_all_required_tools_used(self, checker, tool_log):
        """Test when all required tools are used"""
        required = ["db_query", "db_update"]
        score, reasons = checker.check_tool_expectations(tool_log, required, None)
        assert score == 1.0
        assert len(reasons) == 0

    def test_missing_required_tool(self, checker, tool_log):
        """Test when a required tool is missing"""
        required = ["db_query", "db_update", "write_file"]
        score, reasons = checker.check_tool_expectations(tool_log, required, None)
        assert score == 0.5
        assert len(reasons) == 1
        assert "write_file" in reasons[0]

    def test_disallowed_tool_used(self, checker, tool_log):
        """Test when a disallowed tool is used"""
        disallowed = ["search_kb"]
        score, reasons = checker.check_tool_expectations(tool_log, None, disallowed)
        assert score == 0.0
        assert len(reasons) == 1
        assert "search_kb" in reasons[0]


@pytest.mark.unit
class TestTranscriptGrading:
    """Test complete transcript grading"""

    @pytest.fixture
    def checker(self):
        return TranscriptChecker()

    @pytest.fixture
    def good_messages(self):
        return [
            Message(role=MessageRole.USER, content="I need help with MMS"),
            Message(role=MessageRole.ASSISTANT, content="I'll help you enable MMS"),
            Message(role=MessageRole.USER, content="Thanks"),
            Message(role=MessageRole.ASSISTANT, content="MMS is now enabled"),
        ]

    @pytest.fixture
    def good_tool_log(self):
        return [
            {"tool": "db_query", "success": True},
            {"tool": "db_update", "success": True},
        ]

    def test_all_rules_pass(self, checker, good_messages, good_tool_log):
        """Test when all transcript rules pass"""
        score, reasons = checker.grade(
            messages=good_messages,
            tool_log=good_tool_log,
            must_contain=["MMS", "enabled"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["db_query", "db_update"],
            disallowed_tools=[],
        )
        assert score == 1.0
        assert "All checks passed" in reasons

    def test_some_rules_fail(self, checker, good_messages, good_tool_log):
        """Test when some rules fail"""
        score, reasons = checker.grade(
            messages=good_messages,
            tool_log=good_tool_log,
            must_contain=["MMS", "enabled", "confirmed"],
            disallow_regex=[],
            max_turns=10,
            required_tools=["db_query", "db_update"],
            disallowed_tools=[],
        )
        assert score < 1.0
        assert score > 0.0
        assert "confirmed" in reasons.lower()

    def test_critical_violation(self, checker, good_tool_log):
        """Test with critical violation (disallowed pattern)"""
        messages = [Message(role=MessageRole.ASSISTANT, content="I will factory reset your device")]
        score, reasons = checker.grade(
            messages=messages,
            tool_log=good_tool_log,
            must_contain=[],
            disallow_regex=["(?i)factory.?reset"],
            max_turns=10,
            required_tools=[],
            disallowed_tools=[],
        )
        assert score < 1.0
