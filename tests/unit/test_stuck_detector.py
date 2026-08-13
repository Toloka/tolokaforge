"""Unit tests for StuckDetector stuck-detection heuristics."""

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.models import Message, MessageRole, RecordedToolCall, ToolCall
from tolokaforge.core.stuck import StuckDetector

pytestmark = pytest.mark.unit

# What the cases below that are not about the repeated-call threshold run at.
_THRESHOLD_NOT_UNDER_TEST = 10


def _assistant_msg(content: str, tool_calls: list[ToolCall] | None = None) -> Message:
    """Create an assistant message with optional tool calls."""
    return Message(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)


def _tool_log(tool: str, arguments: dict | None = None) -> RecordedToolCall:
    """One recorded tool call, as :class:`StuckDetector` reads them."""
    return recorded_call(tool, arguments=arguments)


def test_the_repeated_call_threshold_has_to_be_named() -> None:
    """The detector has no answer of its own to "how many is too many".

    Its one production caller resolves the threshold from config and always
    passes it, so a class-level default would be a second answer that nothing
    reads and no configuration ships.
    """
    with pytest.raises(TypeError):
        StuckDetector()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# is_stuck — integration of all heuristics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStuckDetectorNotStuck:
    """Cases where the detector should return False."""

    def test_not_stuck_with_varied_tool_calls(self) -> None:
        """Diverse tool calls across the window should not trigger stuck."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        logs = [_tool_log(f"tool_{i}", {"arg": i}) for i in range(15)]
        messages: list[Message] = []
        assert detector.is_stuck(messages, logs) is False

    def test_not_stuck_working_dialogue(self) -> None:
        """An agent acting on every turn, in the alternating shape the loop
        produces, is not stuck."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        messages: list[Message] = []
        logs: list[RecordedToolCall] = []
        for i in range(15):
            messages.append(Message(role=MessageRole.USER, content=f"question {i}"))
            messages.append(
                _assistant_msg(
                    f"reading record {i}",
                    tool_calls=[ToolCall(id=str(i), name="search", arguments={"record": i})],
                )
            )
            logs.append(_tool_log("search", {"record": i}))
        assert detector.is_stuck(messages, logs) is False

    def test_not_stuck_unique_content(self) -> None:
        """Unique assistant messages should not trigger looping detection."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
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
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
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
# _has_looping_content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoopingContent:
    """Tests for the looping-content heuristic."""

    def test_stuck_looping_content(self) -> None:
        """Repeated identical multi-word messages → stuck (trigram count ≥ 10)."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        # Same sentence repeated 10 times produces the same trigrams 10 times
        repeated = "I am trying to complete the task right now"
        messages = [_assistant_msg(repeated) for _ in range(10)]
        assert detector._has_looping_content(messages) is True

    def test_not_stuck_unique_content(self) -> None:
        """All-different messages should not trigger looping."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
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
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        messages = [_assistant_msg("same words over and over") for _ in range(4)]
        assert detector._has_looping_content(messages) is False

    def test_short_messages_no_trigrams(self) -> None:
        """Messages with fewer than 3 words produce no trigrams."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        messages = [_assistant_msg("hi") for _ in range(10)]
        assert detector._has_looping_content(messages) is False


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCustomThresholds:
    """Verify that constructor parameters control detection sensitivity."""

    def test_custom_thresholds(self) -> None:
        """StuckDetector with a low threshold should detect stuck earlier."""
        detector = StuckDetector(max_repeated_tool_calls=2)
        logs = [_tool_log("search", {"q": "x"})] * 2
        assert detector.is_stuck([], logs) is True

    def test_high_thresholds_avoid_false_positives(self) -> None:
        """High thresholds should tolerate more repetition."""
        detector = StuckDetector(max_repeated_tool_calls=100)
        logs = [_tool_log("search", {"q": "x"})] * 20
        assert detector.is_stuck([], logs) is False
