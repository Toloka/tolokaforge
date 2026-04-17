"""Unit tests for StuckDetector stuck-detection heuristics."""

import pytest

from tolokaforge.core.models import Message, MessageRole, ToolCall
from tolokaforge.core.stuck import StuckDetector

pytestmark = pytest.mark.unit


def _assistant_msg(content: str, tool_calls: list[ToolCall] | None = None) -> Message:
    """Create an assistant message with optional tool calls."""
    return Message(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)


def _tool_log(tool: str, arguments: dict | None = None) -> dict:
    """Create a tool-log dict matching the format StuckDetector expects."""
    return {"tool": tool, "arguments": arguments or {}}


# ---------------------------------------------------------------------------
# is_stuck — integration of all heuristics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStuckDetectorNotStuck:
    """Cases where the detector should return False."""

    def test_not_stuck_with_varied_tool_calls(self) -> None:
        """Diverse tool calls across the window should not trigger stuck."""
        detector = StuckDetector()
        logs = [_tool_log(f"tool_{i}", {"arg": i}) for i in range(15)]
        messages: list[Message] = []
        assert detector.is_stuck(messages, logs) is False

    def test_not_stuck_active_turns(self) -> None:
        """Assistant messages that all carry tool_calls should not be idle."""
        detector = StuckDetector()
        tc = [ToolCall(id="1", name="search", arguments={})]
        messages = [_assistant_msg(f"step {i}", tool_calls=tc) for i in range(15)]
        assert detector.is_stuck(messages, []) is False

    def test_not_stuck_unique_content(self) -> None:
        """Unique assistant messages should not trigger looping detection."""
        detector = StuckDetector()
        # Each message uses entirely different words to avoid shared trigrams
        # hitting the ≥10 threshold.
        unique_sentences = [
            "the quick brown fox jumps over lazy dogs",
            "alice went through mirror into wonderland today",
            "quantum computers solve problems exponentially faster",
            "three blind mice ran after farmer wife",
            "mars rover landed safely on red planet",
            "jazz musicians improvised melodies during late concert",
            "ocean waves crashed against rocky northern cliffs",
            "ancient pyramids stand tall beneath blazing sun",
            "software engineers debug production before monday release",
            "mountain climbers reached summit despite harsh weather",
        ]
        messages = [_assistant_msg(s) for s in unique_sentences]
        assert detector.is_stuck(messages, []) is False

    def test_not_stuck_empty_inputs(self) -> None:
        """Empty messages and tool_logs should never flag as stuck."""
        detector = StuckDetector()
        assert detector.is_stuck([], []) is False


# ---------------------------------------------------------------------------
# _has_repeated_tool_calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRepeatedToolCalls:
    """Tests for the repeated-tool-call heuristic."""

    def test_stuck_repeated_tool_calls(self) -> None:
        """Same tool called more than threshold times → stuck."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [_tool_log("search", {"q": "hello"})] * 6
        assert detector._has_repeated_tool_calls(logs) is True

    def test_stuck_repeated_tool_calls_at_threshold(self) -> None:
        """Exactly threshold identical calls in the window → stuck."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [_tool_log("search", {"q": "hello"})] * 5
        assert detector._has_repeated_tool_calls(logs) is True

    def test_not_stuck_below_repeated_threshold(self) -> None:
        """Fewer identical calls than threshold → not stuck."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [_tool_log("search", {"q": "hello"})] * 4
        assert detector._has_repeated_tool_calls(logs) is False

    def test_mixed_calls_below_threshold(self) -> None:
        """Mixed different tool calls within the window → not stuck."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [
            _tool_log("search", {"q": "a"}),
            _tool_log("read", {"file": "x"}),
            _tool_log("search", {"q": "b"}),
            _tool_log("write", {"file": "y"}),
            _tool_log("search", {"q": "a"}),
        ]
        assert detector._has_repeated_tool_calls(logs) is False


# ---------------------------------------------------------------------------
# _has_idle_turns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdleTurns:
    """Tests for the idle-turns heuristic."""

    def test_stuck_idle_turns(self) -> None:
        """Many assistant messages without tool calls → stuck."""
        detector = StuckDetector(max_idle_turns=5)
        messages = [_assistant_msg(f"thinking {i}") for i in range(6)]
        assert detector._has_idle_turns(messages) is True

    def test_not_stuck_active_turns(self) -> None:
        """Messages with tool calls should not count as idle."""
        detector = StuckDetector(max_idle_turns=5)
        tc = [ToolCall(id="1", name="search", arguments={})]
        messages = [_assistant_msg(f"step {i}", tool_calls=tc) for i in range(6)]
        assert detector._has_idle_turns(messages) is False

    def test_below_idle_threshold(self) -> None:
        """Fewer messages than threshold → not stuck."""
        detector = StuckDetector(max_idle_turns=5)
        messages = [_assistant_msg("thinking") for _ in range(4)]
        assert detector._has_idle_turns(messages) is False

    def test_user_messages_not_counted(self) -> None:
        """Non-assistant messages should not count toward idle turns."""
        detector = StuckDetector(max_idle_turns=5)
        messages = [Message(role=MessageRole.USER, content=f"question {i}") for i in range(6)]
        assert detector._has_idle_turns(messages) is False


# ---------------------------------------------------------------------------
# _has_looping_content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoopingContent:
    """Tests for the looping-content heuristic."""

    def test_stuck_looping_content(self) -> None:
        """Repeated identical multi-word messages → stuck (trigram count ≥ 10)."""
        detector = StuckDetector()
        # Same sentence repeated 10 times produces the same trigrams 10 times
        repeated = "I am trying to complete the task right now"
        messages = [_assistant_msg(repeated) for _ in range(10)]
        assert detector._has_looping_content(messages) is True

    def test_not_stuck_unique_content(self) -> None:
        """All-different messages should not trigger looping."""
        detector = StuckDetector()
        unique_sentences = [
            "the quick brown fox jumps over lazy dogs",
            "alice went through mirror into wonderland today",
            "quantum computers solve problems exponentially faster",
            "three blind mice ran after farmer wife",
            "mars rover landed safely on red planet",
            "jazz musicians improvised melodies during late concert",
            "ocean waves crashed against rocky northern cliffs",
            "ancient pyramids stand tall beneath blazing sun",
            "software engineers debug production before monday release",
            "mountain climbers reached summit despite harsh weather",
        ]
        messages = [_assistant_msg(s) for s in unique_sentences]
        assert detector._has_looping_content(messages) is False

    def test_fewer_than_five_assistant_messages(self) -> None:
        """With fewer than 5 assistant messages, looping cannot trigger."""
        detector = StuckDetector()
        messages = [_assistant_msg("same words over and over") for _ in range(4)]
        assert detector._has_looping_content(messages) is False

    def test_short_messages_no_trigrams(self) -> None:
        """Messages with fewer than 3 words produce no trigrams."""
        detector = StuckDetector()
        messages = [_assistant_msg("hi") for _ in range(10)]
        assert detector._has_looping_content(messages) is False


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCustomThresholds:
    """Verify that constructor parameters control detection sensitivity."""

    def test_custom_thresholds(self) -> None:
        """StuckDetector with low thresholds should detect stuck earlier."""
        detector = StuckDetector(max_repeated_tool_calls=2, max_idle_turns=2)

        # 2 identical tool calls → stuck with threshold=2
        logs = [_tool_log("search", {"q": "x"})] * 2
        assert detector.is_stuck([], logs) is True

        # 2 idle assistant turns → stuck with threshold=2
        messages = [_assistant_msg("idle") for _ in range(2)]
        assert detector.is_stuck(messages, []) is True

    def test_high_thresholds_avoid_false_positives(self) -> None:
        """High thresholds should tolerate more repetition."""
        detector = StuckDetector(max_repeated_tool_calls=100, max_idle_turns=100)
        logs = [_tool_log("search", {"q": "x"})] * 20
        messages = [_assistant_msg("idle") for _ in range(20)]
        assert detector.is_stuck(messages, logs) is False
