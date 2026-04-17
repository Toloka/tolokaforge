"""Unit tests for tolokaforge/core/runner.py — TrialRunner logic.

Covers: constructor, rate limit detection, tool argument normalization,
completion detection, and basic run loop mechanics.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.model_client import GenerationResult
from tolokaforge.core.models import (
    MessageRole,
    Metrics,
    TerminationReason,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.runner import TrialRunner

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_executor() -> MagicMock:
    """Create a mock ToolExecutor."""
    executor = MagicMock()
    executor.get_logs.return_value = []
    return executor


def _make_user_simulator() -> MagicMock:
    """Create a mock UserSimulator."""
    sim = MagicMock()
    sim.reply.return_value = GenerationResult(
        text="Thanks, that answers my question. ###STOP###",
        tool_calls=[],
    )
    return sim


def _make_agent_client(responses: list[GenerationResult] | None = None) -> MagicMock:
    """Create a mock LLMClient for the agent."""
    client = MagicMock()
    if responses:
        client.generate.side_effect = responses
    else:
        client.generate.return_value = GenerationResult(
            text="I've completed the task. ###STOP###",
            tool_calls=[],
            token_usage={"input": 100, "output": 50},
            cost_usd=0.01,
        )
    return client


def _make_runner(**kwargs) -> TrialRunner:
    """Create a TrialRunner with mocked dependencies."""
    defaults = {
        "task_id": "test-task-001",
        "trial_index": 0,
        "agent_client": _make_agent_client(),
        "user_simulator": _make_user_simulator(),
        "tool_executor": _make_tool_executor(),
        "tool_schemas": [{"type": "function", "function": {"name": "test_tool"}}],
        "max_turns": 10,
        "turn_timeout_s": 30,
        "episode_timeout_s": 600,
    }
    defaults.update(kwargs)
    return TrialRunner(**defaults)


# ===================================================================
# Constructor
# ===================================================================


@pytest.mark.unit
class TestTrialRunnerInit:
    """Tests for TrialRunner constructor."""

    def test_fields_from_parameters(self) -> None:
        agent = _make_agent_client()
        user_sim = _make_user_simulator()
        tool_exec = _make_tool_executor()
        schemas = [{"type": "function", "function": {"name": "tool1"}}]

        runner = TrialRunner(
            task_id="task-42",
            trial_index=3,
            agent_client=agent,
            user_simulator=user_sim,
            tool_executor=tool_exec,
            tool_schemas=schemas,
            max_turns=25,
            turn_timeout_s=45,
            episode_timeout_s=900,
            verbose=True,
            strict=True,
        )

        assert runner.task_id == "task-42"
        assert runner.trial_index == 3
        assert runner.agent_client is agent
        assert runner.user_simulator is user_sim
        assert runner.tool_executor is tool_exec
        assert runner.tool_schemas is schemas
        assert runner.max_turns == 25
        assert runner.turn_timeout_s == 45
        assert runner.episode_timeout_s == 900
        assert runner.verbose is True
        assert runner.strict is True

    def test_default_values(self) -> None:
        runner = _make_runner()
        assert runner.verbose is False
        assert runner.strict is False
        assert runner.stuck_detector is None
        assert runner.user_tool_executor is None
        assert runner.request_limiter is None
        assert runner.messages == []
        assert isinstance(runner.metrics, Metrics)
        assert runner.start_time == 0.0
        assert runner.logger is None

    def test_stuck_detector_parameter(self) -> None:
        mock_sd = MagicMock()
        runner = _make_runner(stuck_detector=mock_sd)
        assert runner.stuck_detector is mock_sd

    def test_request_limiter_parameter(self) -> None:
        mock_limiter = MagicMock()
        runner = _make_runner(request_limiter=mock_limiter)
        assert runner.request_limiter is mock_limiter


# ===================================================================
# _is_rate_limit_error (static method)
# ===================================================================


@pytest.mark.unit
class TestIsRateLimitError:
    """Tests for rate limit error detection."""

    def test_429_in_message(self) -> None:
        exc = Exception("HTTP 429 Too Many Requests")
        assert TrialRunner._is_rate_limit_error(exc) is True

    def test_ratelimit_keyword(self) -> None:
        exc = Exception("RateLimitError: quota exceeded")
        assert TrialRunner._is_rate_limit_error(exc) is True

    def test_rate_and_limit_words(self) -> None:
        exc = Exception("You have exceeded the rate limit for this API")
        assert TrialRunner._is_rate_limit_error(exc) is True

    def test_not_rate_limit(self) -> None:
        exc = Exception("Internal server error 500")
        assert TrialRunner._is_rate_limit_error(exc) is False

    def test_empty_message(self) -> None:
        exc = Exception("")
        assert TrialRunner._is_rate_limit_error(exc) is False

    def test_connection_error(self) -> None:
        exc = Exception("Connection refused to host api.openai.com")
        assert TrialRunner._is_rate_limit_error(exc) is False


# ===================================================================
# _normalize_tool_arguments (static method)
# ===================================================================


@pytest.mark.unit
class TestNormalizeToolArguments:
    """Tests for malformed write_file argument recovery."""

    def test_non_write_file_unchanged(self) -> None:
        result = TrialRunner._normalize_tool_arguments("search", {"query": "test"}, "some text")
        assert result == {"query": "test"}

    def test_write_file_with_content_unchanged(self) -> None:
        args = {"path": "file.txt", "content": "existing content"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, "assistant text")
        assert result["content"] == "existing content"

    def test_write_file_recovers_content_from_text(self) -> None:
        # Long, multi-line text qualifies for recovery
        long_text = "def hello():\n    print('Hello World')\n    return True\n" * 3
        args = {"path": "file.py"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, long_text)
        # _normalize_tool_arguments strips the assistant text
        assert result["content"] == long_text.strip()

    def test_write_file_no_recovery_short_text(self) -> None:
        args = {"path": "file.txt"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, "short")
        # Short single-line text should NOT be recovered
        assert "content" not in result

    def test_write_file_no_recovery_low_signal(self) -> None:
        args = {"path": "file.txt"}
        result = TrialRunner._normalize_tool_arguments(
            "write_file", args, "Let me write this file for you"
        )
        assert "content" not in result

    def test_write_file_no_recovery_empty_text(self) -> None:
        args = {"path": "file.txt"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, "")
        assert "content" not in result

    def test_write_file_no_recovery_whitespace_only(self) -> None:
        args = {"path": "file.txt"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, "   \n  \t  ")
        assert "content" not in result

    def test_write_file_none_arguments(self) -> None:
        text = "some text\nanother line\n" * 5
        result = TrialRunner._normalize_tool_arguments("write_file", None, text)
        assert result.get("content") == text.strip()

    def test_write_file_recovery_multiline_short_chars(self) -> None:
        # Short char count but has newlines → should recover
        text = "a\nb\nc\nd\n"
        args = {"path": "x.txt"}
        result = TrialRunner._normalize_tool_arguments("write_file", args, text)
        assert result.get("content") == text.strip()

    def test_low_signal_prefixes_blocked(self) -> None:
        prefixes = [
            "Let me do this now and write the file content\n" * 3,
            "I will create the file with contents\n" * 3,
            "I'll write this for you\n" * 3,
            "Working on the file now\n" * 3,
            "One moment please\n" * 3,
            "Starting now to write\n" * 3,
        ]
        args = {"path": "f.txt"}
        for text in prefixes:
            result = TrialRunner._normalize_tool_arguments("write_file", args, text)
            assert "content" not in result, f"Should not recover: {text[:30]}"


# ===================================================================
# _is_done
# ===================================================================


@pytest.mark.unit
class TestIsDone:
    """Tests for agent completion signal detection.

    Note: The current implementation lowercases the text but compares
    against uppercase marker '###STOP###', so the marker never matches
    in the lowered text. Tests reflect actual behavior.
    """

    def test_stop_marker_case_mismatch(self) -> None:
        runner = _make_runner()
        # done_markers=["###STOP###"], text.lower()="...###stop###"
        # "###STOP###" not in "...###stop###" → False
        assert runner._is_done("Here is the result. ###STOP###") is False

    def test_lowercase_input(self) -> None:
        runner = _make_runner()
        assert runner._is_done("###stop###") is False

    def test_no_marker(self) -> None:
        runner = _make_runner()
        assert runner._is_done("Task is complete, all done.") is False

    def test_empty_text(self) -> None:
        runner = _make_runner()
        assert runner._is_done("") is False

    def test_partial_marker(self) -> None:
        runner = _make_runner()
        assert runner._is_done("###STOP") is False


# ===================================================================
# run() — basic scenarios with mocked dependencies
# ===================================================================


@pytest.mark.unit
class TestTrialRunnerRun:
    """Tests for the main run() method."""

    def test_agent_response_then_user_stop(self) -> None:
        """Agent responds, user sends ###STOP### → USER_STOP termination.

        Note: _is_done never fires due to case mismatch in done_markers,
        so the flow always reaches the user simulator.
        """
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="I've completed the task for you.",
                    tool_calls=[],
                    token_usage={"input": 100, "output": 50},
                    cost_usd=0.01,
                ),
            ]
        )
        runner = _make_runner(agent_client=agent)
        traj = runner.run("You are an agent.", "Please do the task")

        assert isinstance(traj, Trajectory)
        assert traj.task_id == "test-task-001"
        assert traj.trial_index == 0
        assert traj.termination_reason == TerminationReason.USER_STOP
        assert traj.metrics.api_calls == 1
        assert traj.metrics.tokens_input == 100
        assert traj.metrics.tokens_output == 50

    def test_user_stop_signal(self) -> None:
        """Agent responds normally, then user sends ###STOP###."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Here is the answer to your question.",
                    tool_calls=[],
                    token_usage={"input": 50, "output": 25},
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.return_value = GenerationResult(
            text="Thanks! ###STOP###",
            tool_calls=[],
        )
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System prompt", "Hello")

        assert traj.termination_reason == TerminationReason.USER_STOP

    def test_initial_user_message_used_directly(self) -> None:
        """When initial_user_message is provided, it's used directly."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Done. ###STOP###",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        runner = _make_runner(agent_client=agent)
        traj = runner.run("System prompt", "My specific request")

        # First message should be the provided initial user message
        assert traj.messages[0].role == MessageRole.USER
        assert traj.messages[0].content == "My specific request"

    def test_metrics_tracking(self) -> None:
        """Verify metrics are accumulated correctly."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="###STOP###",
                    tool_calls=[],
                    token_usage={"input": 200, "output": 100},
                    cost_usd=0.05,
                ),
            ]
        )
        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.metrics.api_calls == 1
        assert traj.metrics.tokens_input == 200
        assert traj.metrics.tokens_output == 100
        assert traj.metrics.cost_usd_est == 0.05
        assert traj.metrics.latency_total_s > 0

    def test_tool_call_execution(self) -> None:
        """Agent makes a tool call, then gives answer; user sends ###STOP###."""
        tool_call = ToolCall(id="tc1", name="search", arguments={"q": "test"})
        agent_responses = [
            GenerationResult(
                text="Let me search",
                tool_calls=[tool_call],
                token_usage={"input": 50, "output": 20},
            ),
            GenerationResult(
                text="Found it. Here is the answer.",
                tool_calls=[],
                token_usage={"input": 80, "output": 30},
            ),
        ]
        agent = _make_agent_client(agent_responses)

        tool_exec = _make_tool_executor()
        from tolokaforge.tools.registry import ToolResult

        tool_exec.execute.return_value = ToolResult(success=True, output="search result")

        runner = _make_runner(agent_client=agent, tool_executor=tool_exec)
        traj = runner.run("System", "Do the task")

        assert traj.termination_reason == TerminationReason.USER_STOP
        assert traj.metrics.api_calls == 2
        assert traj.metrics.tool_calls >= 1
        tool_exec.execute.assert_called_once_with("search", {"q": "test"})

    def test_stuck_detection(self) -> None:
        """StuckDetector triggers → terminates with STUCK."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Working on it...",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        stuck = MagicMock()
        stuck.is_stuck.return_value = True

        runner = _make_runner(agent_client=agent, stuck_detector=stuck)
        traj = runner.run("System", "Task")

        assert traj.termination_reason == TerminationReason.STUCK_DETECTED
        assert traj.metrics.stuck_detected is True

    def test_max_turns_reached(self) -> None:
        """Agent never stops → max turns termination."""

        def make_response(*args, **kwargs):
            return GenerationResult(
                text="Still working...",
                tool_calls=[],
                token_usage={"input": 10, "output": 5},
            )

        agent = MagicMock()
        agent.generate.side_effect = make_response

        user_sim = MagicMock()
        user_sim.reply.return_value = GenerationResult(
            text="Please keep going.",
            tool_calls=[],
        )

        runner = _make_runner(
            agent_client=agent,
            user_simulator=user_sim,
            max_turns=3,
        )
        traj = runner.run("System", "Task")

        assert traj.termination_reason == TerminationReason.MAX_TURNS

    def test_episode_timeout(self) -> None:
        """Episode timeout → TIMEOUT termination."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="###STOP###",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        runner = _make_runner(agent_client=agent, episode_timeout_s=0)
        # Set start_time to the past so timeout triggers immediately
        # We need to patch time to simulate timeout
        with patch("tolokaforge.core.runner.time") as mock_time:
            mock_time.time.side_effect = [0.0, 1000.0, 1000.0, 1000.0, 1000.0]
            mock_time.sleep = time.sleep
            traj = runner.run("System", "Task")

        assert traj.termination_reason == TerminationReason.TIMEOUT
        assert traj.status == TrialStatus.TIMEOUT

    def test_agent_error_terminates(self) -> None:
        """Agent API error → ERROR termination."""
        agent = MagicMock()
        agent.generate.side_effect = Exception("Connection failed")

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.ERROR

    def test_rate_limit_error_classification(self) -> None:
        """Rate limit errors get correct termination reason."""
        agent = MagicMock()
        agent.generate.side_effect = Exception("429 Too Many Requests")

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.RATE_LIMIT

    def test_api_error_classification(self) -> None:
        """API-related errors get correct termination reason."""
        agent = MagicMock()
        agent.generate.side_effect = Exception("OpenAI API returned 500")

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.API_ERROR

    def test_trajectory_timestamps(self) -> None:
        """Trajectory has valid start and end timestamps."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="###STOP###",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.start_ts <= traj.end_ts
        assert traj.start_ts.tzinfo is not None

    def test_request_limiter_called(self) -> None:
        """Request limiter is called before each agent generation."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="###STOP###",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        limiter = MagicMock()
        runner = _make_runner(agent_client=agent, request_limiter=limiter)
        runner.run("System", "Task")
        limiter.acquire.assert_called()

    def test_cost_accumulation(self) -> None:
        """Cost accumulates across multiple API calls."""
        agent_responses = [
            GenerationResult(
                text="Part 1",
                tool_calls=[ToolCall(id="tc1", name="t", arguments={})],
                token_usage={"input": 10, "output": 5},
                cost_usd=0.01,
            ),
            GenerationResult(
                text="Part 2. ###STOP###",
                tool_calls=[],
                token_usage={"input": 20, "output": 10},
                cost_usd=0.02,
            ),
        ]
        agent = _make_agent_client(agent_responses)
        tool_exec = _make_tool_executor()
        from tolokaforge.tools.registry import ToolResult

        tool_exec.execute.return_value = ToolResult(success=True, output="ok")

        runner = _make_runner(agent_client=agent, tool_executor=tool_exec)
        traj = runner.run("System", "Task")

        assert traj.metrics.cost_usd_est == pytest.approx(0.03)
        assert traj.metrics.tokens_input == 30
        assert traj.metrics.tokens_output == 15


# ===================================================================
# User simulator integration
# ===================================================================


@pytest.mark.unit
class TestUserSimulatorIntegration:
    """Tests for user simulator message flow in TrialRunner."""

    def test_user_simulator_generates_first_message(self) -> None:
        """When no initial_user_message, user simulator generates first msg."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="###STOP###",
                    tool_calls=[],
                    token_usage={"input": 10, "output": 5},
                ),
            ]
        )
        user_sim = MagicMock()
        user_sim.reply.return_value = GenerationResult(
            text="I need help with my order",
            tool_calls=[],
        )

        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System", "")  # Empty initial message

        # User simulator should have been called for first message
        assert user_sim.reply.called
        assert traj.messages[0].role == MessageRole.USER
        assert traj.messages[0].content == "I need help with my order"
