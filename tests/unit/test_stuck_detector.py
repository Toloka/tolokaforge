"""Unit tests for StuckDetector stuck-detection heuristics."""

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.models import RecordedToolCall
from tolokaforge.core.stuck import StuckDetector

pytestmark = pytest.mark.unit

# What the cases below that are not about the repeated-call threshold run at.
_THRESHOLD_NOT_UNDER_TEST = 10

_BASH_ARGS = {"command": "pytest -x tests/foo.py"}


def _tool_log(tool: str, arguments: dict | None = None) -> RecordedToolCall:
    """One recorded tool call, as :class:`StuckDetector` reads them."""
    return recorded_call(tool, arguments=arguments)


def test_the_repeated_call_threshold_has_to_be_named() -> None:
    """How many repeats is too many is the configuration's answer, never the class's.

    What is locked is that decision, not the exception: the detector's one
    production caller resolves the threshold from config and always passes it,
    so a class-level default would be a second answer that nothing reads and no
    configuration ships — a divergent value that reads as shipped. Re-add one
    and this fails; ``TypeError`` is only how Python reports it.
    """
    with pytest.raises(TypeError):
        StuckDetector()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# is_stuck — the classifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStuckDetectorNotStuck:
    """Cases where the detector should return False."""

    def test_not_stuck_with_varied_tool_calls(self) -> None:
        """Diverse tool calls across the window should not trigger stuck."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        logs = [_tool_log(f"tool_{i}", {"arg": i}) for i in range(15)]
        assert detector.is_stuck(logs) is False

    def test_not_stuck_working_dialogue(self) -> None:
        """An agent acting on every turn with distinct arguments is not stuck."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        logs = [_tool_log("search", {"record": i}) for i in range(15)]
        assert detector.is_stuck(logs) is False

    def test_not_stuck_empty_inputs(self) -> None:
        """Empty tool-call log should never flag as stuck."""
        detector = StuckDetector(max_repeated_tool_calls=_THRESHOLD_NOT_UNDER_TEST)
        assert detector.is_stuck([]) is False


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
# Result-aware identity: same (name, args) with different outputs is progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResultAwareRepetition:
    """The identity a repeated call is counted under includes its recorded output.

    Same tool + same args + *different* result bytes across turns is progress,
    not a stall: the model got new information every turn. Same tool + same args
    + same result bytes across turns is the shape the detector is for.
    """

    def test_same_args_different_output_is_not_stuck(self) -> None:
        """Fix-and-verify: identical ``pytest -x`` command, evolving stdout each turn."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [
            recorded_call(
                "bash",
                sequence=i,
                arguments=_BASH_ARGS,
                output=f"FAILED tests/foo.py::test_case[{i}] AssertionError: seq {i}",
            )
            for i in range(6)
        ]
        assert detector.is_stuck(logs) is False

    def test_same_args_same_output_still_stuck(self) -> None:
        """Byte-identical call and result over N turns is still the stall shape."""
        detector = StuckDetector(max_repeated_tool_calls=5)
        logs = [
            recorded_call(
                "bash",
                sequence=i,
                arguments=_BASH_ARGS,
                output="FAILED tests/foo.py::test_case AssertionError",
            )
            for i in range(6)
        ]
        assert detector.is_stuck(logs) is True


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCustomThresholds:
    """Verify that the constructor parameter controls detection sensitivity."""

    def test_custom_thresholds(self) -> None:
        """StuckDetector with a low threshold should detect stuck earlier."""
        detector = StuckDetector(max_repeated_tool_calls=2)
        logs = [_tool_log("search", {"q": "x"})] * 2
        assert detector.is_stuck(logs) is True

    def test_high_thresholds_avoid_false_positives(self) -> None:
        """High thresholds should tolerate more repetition."""
        detector = StuckDetector(max_repeated_tool_calls=100)
        logs = [_tool_log("search", {"q": "x"})] * 20
        assert detector.is_stuck(logs) is False
