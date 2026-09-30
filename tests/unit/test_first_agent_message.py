"""``actors.user.first_agent_message`` — the agent's opening line, ahead of the user.

Four layers, each with the real code under test and only the network faked:

* the declaration on the user actor, and what it refuses;
* a whole :class:`TrialRunner` trial, with an agent that keeps a copy of every
  request it was sent and a user that keeps every context it was given, so "the
  agent reads the line as its own first turn" and "the simulator answers the line"
  are asserted on what each party actually received;
* :class:`BuiltinUserSimulator`'s own request, where the line replaces the built-in
  greeting rather than joining it;
* the readers of a recorded transcript: the turn count leaves the line out, and
  grading reads it as the agent's first message.

The provider-shape tests push the agent's request through the conversions litellm
runs before sending, so what reaches a provider is a request that opens with the
agent's line: litellm inserts no placeholder turn ahead of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import litellm
import pytest
from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt
from litellm.llms.vertex_ai.gemini.transformation import _gemini_convert_messages_with_history
from pydantic import ValidationError

from tolokaforge.core.actors.tool_turn_rule import UserToolTurnRule
from tolokaforge.core.grading.trace_event_kind import TraceEventKind
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.llm import SIMULATOR_GREETING, GenerationResult
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import BuiltinUserSimulator, LLMClient
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import classify_loop_error
from tolokaforge.core.models import (
    FirstUserMessageSource,
    Message,
    MessageRole,
    ModelConfig,
    TaskConfig,
    TerminationReason,
    ToolCall,
    TrialStatus,
    UserSimulatorConfig,
)
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit

LINE = "Hi! How can I help you today?"


def _msg(role: MessageRole, content: str = "", **fields: Any) -> Message:
    return Message(role=role, content=content, **fields)


def _say(text: str) -> GenerationResult:
    return GenerationResult(text=text, tool_calls=[])


def _said(messages: list[Message]) -> list[tuple[str, str]]:
    return [(m.role.value, m.content) for m in messages if m.role is not MessageRole.SYSTEM]


def _task(**user: Any) -> TaskConfig:
    return TaskConfig(task_id="t1", description="d", actors={"user": user})


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_the_line_resolves_from_the_user_actor(self) -> None:
        assert _task(first_agent_message=LINE).resolve_user_simulator().first_agent_message == LINE

    def test_it_is_off_by_default(self) -> None:
        assert _task().resolve_user_simulator().first_agent_message is None
        assert UserSimulatorConfig().first_agent_message is None

    @pytest.mark.parametrize("blank", ["", "  \n"])
    def test_a_blank_line_is_refused(self, blank: str) -> None:
        with pytest.raises(ValidationError, match="first_agent_message"):
            _task(first_agent_message=blank)
        with pytest.raises(ValidationError, match="first_agent_message"):
            UserSimulatorConfig(first_agent_message=blank)

    def test_agent_only_refuses_a_line_no_user_answers(self) -> None:
        with pytest.raises(ValidationError, match="agent_only"):
            TaskConfig(
                task_id="t1",
                description="d",
                interaction_mode="agent_only",
                initial_user_message="Do the task.",
                actors={"user": {"first_agent_message": LINE}},
            )

    def test_the_resolved_config_round_trips_its_dump(self) -> None:
        resolved = _task(first_agent_message=LINE).resolve_user_simulator()
        assert UserSimulatorConfig.model_validate(resolved.model_dump()) == resolved

    def test_first_message_on_the_actor_points_at_both_openings(self) -> None:
        with pytest.raises(ValidationError, match="first_agent_message"):
            _task(first_message="Hi, I need help.")


# ---------------------------------------------------------------------------
# Whole trials
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """The agent's generate seam: queued replies, and a copy of every request's messages."""

    def __init__(self, *replies: str | GenerationResult | Exception) -> None:
        self._replies = list(replies)
        self.requests: list[list[Message]] = []
        self.capabilities = ModelCapabilities()

    def generate(self, *, messages: list[Message], **_: Any) -> GenerationResult:
        self.requests.append(list(messages))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            reply = _say(reply)
        return GenerationResult(
            text=reply.text,
            tool_calls=list(reply.tool_calls),
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )

    def classify_loop_error(self, exc: Exception):
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> None:
        return None


class _QueuedUser:
    """A user actor answering from a queue, keeping a copy of every context it was given."""

    last_system_prompt = None

    def __init__(self, *replies: GenerationResult) -> None:
        self._replies = list(replies)
        self.contexts: list[list[Message]] = []

    def reply(self, context: list[Message], *, observation: Any = None) -> GenerationResult:
        self.contexts.append(list(context))
        return self._replies.pop(0)


class _UserTools:
    """A user-side executor that answers every call."""

    def execute(
        self,
        tool_name: str,
        arguments: dict | None = None,
        *,
        call_id: str,
        validation_schema: dict | None = None,
    ) -> ToolResult:
        return ToolResult(success=True, output=f"{tool_name}: ok")


def _trial(
    agent: _RecordingAgent,
    user: _QueuedUser,
    *,
    line: str | None = LINE,
    tool_turns: UserToolTurnRule = UserToolTurnRule(),
) -> TrialRunner:
    return TrialRunner(
        task_id="opening",
        trial_index=0,
        agent_client=agent,  # type: ignore[arg-type]
        user_simulator=user,  # type: ignore[arg-type]
        tool_executor=MagicMock(),
        tool_schemas=[],
        user_tool_executor=_UserTools(),
        user_tool_turns=tool_turns,
        first_agent_message=line,
    )


class TestTrial:
    def test_the_agent_reads_the_line_as_its_own_first_turn(self) -> None:
        agent = _RecordingAgent("I can move it to Friday.")
        user = _QueuedUser(_say("I need to change my trip."), _say("###STOP###"))

        trajectory = _trial(agent, user).run("System")

        assert trajectory.termination_reason == TerminationReason.USER_STOP
        assert _said(trajectory.messages)[:3] == [
            ("assistant", LINE),
            ("user", "I need to change my trip."),
            ("assistant", "I can move it to Friday."),
        ]
        assert _said(agent.requests[0]) == [
            ("assistant", LINE),
            ("user", "I need to change my trip."),
        ]
        assert trajectory.first_user_message_source is FirstUserMessageSource.SIMULATOR

    def test_the_simulator_answers_the_line_instead_of_the_built_in_greeting(self) -> None:
        agent = _RecordingAgent("Sure.")
        user = _QueuedUser(_say("Hello, I need a refund."), _say("###STOP###"))

        _trial(agent, user, line="Welcome to Acme support.").run("System")

        assert _said(user.contexts[0]) == [("assistant", "Welcome to Acme support.")]
        assert all(m.content != SIMULATOR_GREETING for c in user.contexts for m in c)

    def test_the_line_counts_as_no_turn(self) -> None:
        agent = _RecordingAgent("One moment.", "Done.")
        user = _QueuedUser(_say("Change my trip."), _say("To Friday."), _say("###STOP###"))

        trajectory = _trial(agent, user).run("System")

        assert trajectory.metrics.turns == 2

    def test_an_agent_that_never_generated_is_not_reported_as_an_unrecorded_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The line is recorded, not generated: when the agent's first generation fails,
        no model call was made, and the metrics-sink audit must not read the line as an
        assistant turn nobody recorded."""
        findings: list[str] = []
        monkeypatch.setattr(
            TrialRunner,
            "_report_postcondition_finding",
            lambda self, message, **_: findings.append(message),
        )
        error = litellm.exceptions.APIError(
            status_code=500, message="boom", llm_provider="openrouter", model="m"
        )

        trajectory = _trial(_RecordingAgent(error), _QueuedUser(_say("Change my trip."))).run(
            "System"
        )

        assert trajectory.status is TrialStatus.ERROR
        assert trajectory.metrics.api_calls == 0
        assert _said(trajectory.messages)[:2] == [("assistant", LINE), ("user", "Change my trip.")]
        assert not [f for f in findings if "without recording a single model call" in f]

    def test_a_pinned_opening_follows_the_line(self) -> None:
        agent = _RecordingAgent("Sure, which one?")
        user = _QueuedUser(_say("###STOP###"))

        trajectory = _trial(agent, user).run("System", "Change my trip.")

        assert _said(trajectory.messages)[:2] == [("assistant", LINE), ("user", "Change my trip.")]
        assert trajectory.first_user_message_source is FirstUserMessageSource.PINNED
        assert _said(user.contexts[0]) == [
            ("assistant", LINE),
            ("user", "Change my trip."),
            ("assistant", "Sure, which one?"),
        ]

    def test_without_a_line_the_transcript_opens_with_the_user(self) -> None:
        agent = _RecordingAgent("Sure.")
        user = _QueuedUser(_say("Change my trip."), _say("###STOP###"))

        trajectory = _trial(agent, user, line=None).run("System")

        assert trajectory.messages[0].role is MessageRole.USER
        assert _said(user.contexts[0]) == [("assistant", SIMULATOR_GREETING)]
        assert _said(agent.requests[0]) == [("user", "Change my trip.")]

    def test_an_isolated_opening_records_its_steps_after_the_line(self) -> None:
        agent = _RecordingAgent("Thanks, let me look.")
        user = _QueuedUser(
            GenerationResult(
                text="", tool_calls=[ToolCall(id="u1", name="check_balance", arguments={})]
            ),
            _say("My balance is 12.50 and my card was declined."),
            _say("###STOP###"),
        )

        trajectory = _trial(agent, user, tool_turns=UserToolTurnRule("isolated", 10)).run("System")

        roles = [
            f"{m.role.value}+calls" if m.tool_calls else m.role.value
            for m in trajectory.messages
            if m.role is not MessageRole.SYSTEM
        ]
        assert roles[:4] == ["assistant", "user+calls", "tool", "user"]
        assert [m.role.value for m in user.contexts[1]] == ["assistant", "user", "tool"]
        assert user.contexts[1][0].content == LINE
        assert _said(agent.requests[0]) == [
            ("assistant", LINE),
            ("user", "My balance is 12.50 and my card was declined."),
        ]


# ---------------------------------------------------------------------------
# The simulator's own request
# ---------------------------------------------------------------------------


class _FakeWire:
    """Stands in for the simulator's :class:`LLMClient`, returning one queued result per call."""

    def __init__(self, *results: GenerationResult) -> None:
        self._results = list(results)
        self.requests: list[list[Message]] = []

    def generate(self, *, messages: list[Message], **_: Any) -> GenerationResult:
        self.requests.append(list(messages))
        return self._results.pop(0)


@pytest.mark.parametrize("tool_turns", ["shared", "isolated"])
def test_the_simulator_reads_the_line_once_and_first(tool_turns: str) -> None:
    simulator = BuiltinUserSimulator(mode="llm", llm_config=None, tool_turns=tool_turns)
    wire = _FakeWire(_say("To Friday, please."))
    simulator.llm_client = wire  # type: ignore[assignment]

    simulator.reply(
        [
            _msg(MessageRole.ASSISTANT, "Welcome to Acme support."),
            _msg(MessageRole.USER, "Change my trip."),
            _msg(MessageRole.ASSISTANT, "Which day?"),
        ]
    )

    assert _said(wire.requests[0]) == [
        ("user", "Welcome to Acme support."),
        ("assistant", "Change my trip."),
        ("user", "Which day?"),
    ]


# ---------------------------------------------------------------------------
# Readers of the recorded transcript
# ---------------------------------------------------------------------------

OPENED = [
    _msg(MessageRole.SYSTEM, "harness note"),
    _msg(MessageRole.ASSISTANT, LINE),
    _msg(MessageRole.USER, "My card was declined."),
    _msg(
        MessageRole.ASSISTANT,
        "Let me look.",
        tool_calls=[ToolCall(id="a1", name="get_card", arguments={})],
    ),
    _msg(MessageRole.TOOL, "card: blocked", tool_call_id="a1"),
    _msg(MessageRole.ASSISTANT, "Your card is blocked."),
]


class TestReaders:
    """Grading reads the transcript as recorded: the line is the agent's first
    message, in turn 0."""

    def test_the_timeline_keeps_the_line_as_the_agents_turn_zero(self) -> None:
        timeline = build_trial_timeline(OPENED, [], TerminationReason.USER_STOP)

        shape = [(event.kind, event.turn_index) for event in timeline.events]
        assert shape == [
            (TraceEventKind.ASSISTANT_MESSAGE, 0),
            (TraceEventKind.USER_MESSAGE, 0),
            (TraceEventKind.ASSISTANT_MESSAGE, 1),
            (TraceEventKind.TOOL_CALL, 1),
            (TraceEventKind.TOOL_RESULT, 1),
            (TraceEventKind.ASSISTANT_MESSAGE, 2),
        ]
        assert timeline.events[0].text == LINE


# ---------------------------------------------------------------------------
# What providers are sent
# ---------------------------------------------------------------------------


def _response(text: str) -> MagicMock:
    message = MagicMock()
    message.content = text
    message.tool_calls = None
    message.reasoning_content = None
    del message.thinking_blocks
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"
    response = MagicMock()
    response.choices = [choice]
    response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return response


@pytest.fixture
def agent_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The ``messages`` the agent's client sends for a transcript opened by its line."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-opening")
    client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o-mini"))
    with (
        patch(
            "tolokaforge.core.llm.client.completion", return_value=_response("Which day?")
        ) as completion,
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        client.generate(
            system="System",
            messages=[_msg(MessageRole.ASSISTANT, LINE), _msg(MessageRole.USER, "Change my trip.")],
        )
    return completion.call_args.kwargs["messages"]


class TestProviderShapes:
    """The agent's request, opened by its line, through each provider family's rules."""

    def test_openai_is_sent_the_line_right_after_the_system_prompt(
        self, agent_request: list[dict[str, Any]]
    ) -> None:
        assert [m["role"] for m in agent_request] == ["system", "assistant", "user"]
        assert agent_request[1]["content"] == LINE

    def test_anthropic_gets_no_placeholder_turn_ahead_of_the_line(
        self, agent_request: list[dict[str, Any]]
    ) -> None:
        converted = anthropic_messages_pt(
            [m for m in agent_request if m["role"] != "system"],
            model="claude-sonnet-4-5",
            llm_provider="anthropic",
        )

        assert [m["role"] for m in converted] == ["assistant", "user"]
        assert converted[0]["content"][0]["text"] == LINE

    def test_gemini_gets_no_placeholder_turn_ahead_of_the_line(
        self, agent_request: list[dict[str, Any]]
    ) -> None:
        contents = _gemini_convert_messages_with_history(
            [m for m in agent_request if m["role"] != "system"]
        )

        assert [c["role"] for c in contents] == ["model", "user"]
