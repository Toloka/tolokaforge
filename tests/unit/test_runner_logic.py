"""Unit tests for tolokaforge/core/runner.py — TrialRunner logic.

Covers: constructor, rate limit detection, tool argument normalization,
completion detection, and basic run loop mechanics.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import Usage
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
    return MagicMock()


def _make_user_simulator() -> MagicMock:
    """Create a mock UserSimulator.

    Default reply is a bare ``###STOP###`` so tests that don't care about the
    stop-token split path get the immediate-terminate branch. Tests exercising
    the split-on-final-reply path override the reply explicitly.
    """
    sim = MagicMock()
    sim.reply.return_value = GenerationResult(
        text="###STOP###",
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
            usage=Usage(prompt_tokens=100, completion_tokens=50),
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
    """The completion marker is matched whatever case the agent emitted it in."""

    @pytest.mark.parametrize(
        "text",
        [
            "Here is the result. ###STOP###",
            "###STOP###",
            "###stop###",
            "###Stop###",
            "###STOP### and some trailing chatter",
        ],
    )
    def test_marker_present_in_any_case(self, text: str) -> None:
        assert _make_runner()._is_done(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Task is complete, all done.",
            "",
            "###STOP",
            "##STOP##",
        ],
    )
    def test_no_marker(self, text: str) -> None:
        assert _make_runner()._is_done(text) is False


# ===================================================================
# run() — basic scenarios with mocked dependencies
# ===================================================================


@pytest.mark.unit
class TestTrialRunnerRun:
    """Tests for the main run() method."""

    def test_agent_response_then_user_stop(self) -> None:
        """Agent responds without a completion marker, user sends ###STOP###."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="I've completed the task for you.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=100, completion_tokens=50),
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
        assert traj.metrics.usage.prompt_tokens == 100
        assert traj.metrics.usage.completion_tokens == 50

    def test_user_stop_signal(self) -> None:
        """Agent responds normally, then user sends a bare ###STOP###."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Here is the answer to your question.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=50, completion_tokens=25),
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.return_value = GenerationResult(
            text="###STOP###",
            tool_calls=[],
        )
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System prompt", "Hello")

        assert traj.termination_reason == TerminationReason.USER_STOP

    def test_user_stop_with_final_reply_is_delivered_first(self) -> None:
        """Simulator glues a substantive reply to ``###STOP###`` in one message.

        The pre-token text is delivered to the agent as a USER message, the
        agent gets one more turn to react, and the dialogue ends with
        ``USER_STOP``. Pre-fix, the entire text (including the mandated reply)
        was discarded on the ``###STOP###`` substring match.
        """
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="I understand. I'll close this case now.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=100, completion_tokens=50),
                    cost_usd=0.01,
                ),
                GenerationResult(
                    text="Case closed. Reference CASE-12345.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=120, completion_tokens=40),
                    cost_usd=0.01,
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.side_effect = [
            GenerationResult(
                text=(
                    "That's okay, I'll handle the payment through the online portal "
                    "instead — please go ahead and close this request. ###STOP###"
                ),
                tool_calls=[],
            ),
        ]
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System", "I'd like to make a payment")

        assert traj.termination_reason == TerminationReason.USER_STOP
        assert traj.metrics.api_calls == 2

        user_messages = [m for m in traj.messages if m.role == MessageRole.USER]
        # The final reply must be delivered to the agent verbatim (sans token).
        delivered = [m.content for m in user_messages]
        assert any("handle the payment through the online portal" in text for text in delivered)
        # The stop token itself must NOT survive on any user message.
        assert not any("###STOP###" in text for text in delivered)
        # Exactly one simulator call — pending flag terminates the next user turn
        # without another simulator invocation.
        assert user_sim.reply.call_count == 1
        # The pending flag must reset after the terminating turn so an
        # (unlikely) re-use of the runner instance doesn't abort turn 1.
        assert runner._user_stop_pending is False

    def test_user_stop_whitespace_only_pre_token_terminates_immediately(self) -> None:
        """Whitespace-only text before the token routes through the bare-stop
        branch — no extra agent turn, no pending flag.
        """
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Working on it.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.return_value = GenerationResult(
            text="   \n\t  ###STOP###",
            tool_calls=[],
        )
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System", "Do the task")

        assert traj.termination_reason == TerminationReason.USER_STOP
        assert traj.metrics.api_calls == 1  # Only the initial agent turn
        assert runner._user_stop_pending is False

    def test_user_stop_first_token_wins_when_multiple(self) -> None:
        """When the simulator reply contains ``###STOP###`` more than once,
        ``str.partition`` splits on the first occurrence; the remainder
        (including any additional tokens) is discarded, and the pre-token
        text is delivered as a USER message.
        """
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Acknowledged.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                ),
                GenerationResult(
                    text="Done.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.side_effect = [
            GenerationResult(
                text="first reply ###STOP### middle chunk ###STOP### tail",
                tool_calls=[],
            ),
        ]
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System", "Hello")

        assert traj.termination_reason == TerminationReason.USER_STOP
        user_messages = [m for m in traj.messages if m.role == MessageRole.USER]
        delivered = [m.content for m in user_messages]
        # First-occurrence split: the delivered user message keeps ONLY the
        # pre-first-token text; downstream chunks are dropped even if they
        # contain a second token.
        assert any(text == "first reply" for text in delivered)
        assert not any("middle chunk" in text for text in delivered)
        assert not any("tail" in text for text in delivered)
        assert not any("###STOP###" in text for text in delivered)

    def test_user_stop_with_final_reply_preserves_tool_calls(self) -> None:
        """Simulator reply with both tool_calls and text-glued ``###STOP###``.

        The stop-token strip must not drop the ``tool_calls`` — they still
        need to reach ``ActionEvaluator`` for required-action tracking.
        """
        sim_tool_call = ToolCall(id="uc1", name="user_lookup", arguments={"id": "42"})
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="I'll check that for you.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                ),
                GenerationResult(
                    text="Case closed.",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                ),
            ]
        )
        user_sim = _make_user_simulator()
        user_sim.reply.side_effect = [
            GenerationResult(
                text="Please close it. ###STOP###",
                tool_calls=[sim_tool_call],
            ),
        ]
        runner = _make_runner(agent_client=agent, user_simulator=user_sim)
        traj = runner.run("System", "Hi")

        assert traj.termination_reason == TerminationReason.USER_STOP
        user_messages = [m for m in traj.messages if m.role == MessageRole.USER]
        # Find the delivered user message (not the initial one).
        delivered = next(m for m in user_messages if "Please close it" in (m.content or ""))
        assert "###STOP###" not in delivered.content
        assert delivered.tool_calls is not None
        assert len(delivered.tool_calls) == 1
        assert delivered.tool_calls[0].id == "uc1"

    def test_initial_user_message_used_directly(self) -> None:
        """When initial_user_message is provided, it's used directly."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Done. ###STOP###",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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
                    usage=Usage(prompt_tokens=200, completion_tokens=100),
                    cost_usd=0.05,
                ),
            ]
        )
        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.metrics.api_calls == 1
        assert traj.metrics.usage.prompt_tokens == 200
        assert traj.metrics.usage.completion_tokens == 100
        assert traj.metrics.cost_usd == 0.05
        assert traj.metrics.latency_total_s > 0

    def test_tool_call_execution(self) -> None:
        """Agent makes a tool call, then gives answer; user sends ###STOP###."""
        tool_call = ToolCall(id="tc1", name="search", arguments={"q": "test"})
        agent_responses = [
            GenerationResult(
                text="Let me search",
                tool_calls=[tool_call],
                usage=Usage(prompt_tokens=50, completion_tokens=20),
            ),
            GenerationResult(
                text="Found it. Here is the answer.",
                tool_calls=[],
                usage=Usage(prompt_tokens=80, completion_tokens=30),
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
        tool_exec.execute.assert_called_once_with("search", {"q": "test"}, call_id="tc1")

    def test_stuck_detection(self) -> None:
        """StuckDetector triggers → terminates with STUCK."""
        agent = _make_agent_client(
            [
                GenerationResult(
                    text="Working on it...",
                    tool_calls=[],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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
                usage=Usage(prompt_tokens=10, completion_tokens=5),
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
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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
        """A provider's typed 429 → RATE_LIMIT, reached through the wrapper the
        client re-raises it inside."""
        from litellm.exceptions import RateLimitError

        inner = RateLimitError(
            message="Rate limit exceeded", llm_provider="openrouter", model="anthropic/claude"
        )
        wrapped = RuntimeError(f"LLM API call failed: {inner}")
        wrapped.__cause__ = inner
        agent = MagicMock()
        agent.generate.side_effect = wrapped

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.RATE_LIMIT

    def test_untyped_429_text_is_not_a_rate_limit(self) -> None:
        """A 429-shaped message with no typed exception behind it is counted,
        not excused: ``RATE_LIMIT`` takes the trial out of every rate, and prose
        cannot tell a provider's throttle from a transcript that discusses one.
        The trial records why it was not treated as one."""
        agent = MagicMock()
        agent.generate.side_effect = Exception("429 Too Many Requests")

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.ERROR
        assert "no typed provider exception" in traj.messages[-1].content

    def test_api_timeout_classification(self) -> None:
        """``LLMApiTimeoutError`` raised by the client → API_TIMEOUT."""
        from tolokaforge.core.llm.client import LLMApiTimeoutError

        agent = MagicMock()
        agent.generate.side_effect = LLMApiTimeoutError(
            "LLM API call timed out after 6 attempts (timeout=120.0s)"
        )

        runner = _make_runner(agent_client=agent)
        traj = runner.run("System", "Task")

        assert traj.status == TrialStatus.ERROR
        assert traj.termination_reason == TerminationReason.API_TIMEOUT

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
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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
                usage=Usage(prompt_tokens=10, completion_tokens=5),
                cost_usd=0.01,
            ),
            GenerationResult(
                text="Part 2. ###STOP###",
                tool_calls=[],
                usage=Usage(prompt_tokens=20, completion_tokens=10),
                cost_usd=0.02,
            ),
        ]
        agent = _make_agent_client(agent_responses)
        tool_exec = _make_tool_executor()
        from tolokaforge.tools.registry import ToolResult

        tool_exec.execute.return_value = ToolResult(success=True, output="ok")

        runner = _make_runner(agent_client=agent, tool_executor=tool_exec)
        traj = runner.run("System", "Task")

        assert traj.metrics.cost_usd == pytest.approx(0.03)
        assert traj.metrics.usage.prompt_tokens == 30
        assert traj.metrics.usage.completion_tokens == 15


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
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
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

    def test_empty_bootstrap_first_message_fails_loud(self) -> None:
        """A simulator bootstrap that returns empty/whitespace text raises.

        A blank opening would seed the transcript with an empty USER turn:
        the simulator's flipped context later drops dialogue-free turns, so
        it would lose every trace of having asked and restart the
        conversation — the failure mode the seeded-opening fix prevents.
        """
        user_sim = MagicMock()
        user_sim.reply.return_value = GenerationResult(
            text="   \n\t ",
            tool_calls=[],
        )
        runner = _make_runner(user_simulator=user_sim)

        with pytest.raises(RuntimeError, match="empty first message"):
            runner._bootstrap_via_simulator()
