"""``actors.user.tool_turns: isolated`` — the user's tool steps, and who sees them.

Three layers, each with the real code under test and only the network faked:

* the two projections of a transcript, as pure functions;
* a whole :class:`TrialRunner` trial, with an agent that keeps a copy of every
  request it was sent, so "the agent never reads a user tool step" is asserted on
  what actually went to the agent, turn by turn;
* :class:`BuiltinUserSimulator` in ``isolated`` mode, with a fake wire client.

The provider-shape tests below push a simulator's request through the provider
conversions litellm runs before sending, so the claim that a flipped context with
tool steps is a request providers accept rests on their own message rules.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt
from litellm.llms.vertex_ai.gemini.transformation import _gemini_convert_messages_with_history

from tolokaforge.core.actors.tool_steps import (
    TurnShape,
    turn_shape_of,
    user_tool_step_positions_of,
)
from tolokaforge.core.actors.tool_turn_rule import UserToolTurnRule
from tolokaforge.core.actors.tool_turns import (
    agent_view,
    shared_view,
    simulator_view,
    user_tool_step_call_ids,
)
from tolokaforge.core.actors.user_stop import UserStopRule
from tolokaforge.core.grading.trace_event_kind import TraceEventKind
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import BuiltinUserSimulator
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import classify_loop_error
from tolokaforge.core.models import (
    FirstUserMessageSource,
    Message,
    MessageRole,
    ModelConfig,
    TerminationReason,
    ToolCall,
    ToolExecutorIdentity,
    TrialStatus,
    UserSimulatorConfig,
)
from tolokaforge.core.models.task_config import UserStopWithText
from tolokaforge.core.runner import TrialRunner
from tolokaforge.runner.models import RequiredAction, TranscriptRulesConfig
from tolokaforge.tools.registry import ToolExecutionStatus, ToolResult

pytestmark = pytest.mark.unit

GREETING = "Hi! How can I help you today?"


def _msg(role: MessageRole, content: str = "", **fields: Any) -> Message:
    return Message(role=role, content=content, **fields)


def _call(call_id: str, name: str = "check_balance", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _tool_step(*calls: ToolCall, text: str = "") -> GenerationResult:
    return GenerationResult(text=text, tool_calls=list(calls))


def _say(text: str) -> GenerationResult:
    return GenerationResult(text=text, tool_calls=[])


# An isolated trial's recorded transcript: the agent asks, the user checks its
# balance in one step, replies, and the agent looks something up itself.
ISOLATED_TRANSCRIPT = [
    _msg(MessageRole.USER, "My card was declined."),
    _msg(MessageRole.ASSISTANT, "Can you check your balance?"),
    _msg(MessageRole.USER, "", tool_calls=[_call("u1")]),
    _msg(MessageRole.TOOL, "balance: 12.50", tool_call_id="u1"),
    _msg(MessageRole.USER, "It says 12.50."),
    _msg(
        MessageRole.ASSISTANT,
        "Let me look at the card.",
        tool_calls=[_call("a1", "get_card")],
    ),
    _msg(MessageRole.TOOL, "card: blocked", tool_call_id="a1"),
    _msg(MessageRole.ASSISTANT, "Your card is blocked."),
]

# The same exchange under ``shared``: the result rides in the user's text, the
# calls stay on its message, and no TOOL message answers them.
SHARED_TRANSCRIPT = [
    _msg(MessageRole.USER, "My card was declined."),
    _msg(MessageRole.ASSISTANT, "Can you check your balance?"),
    _msg(
        MessageRole.USER,
        "Let me check that.\n\ncheck_balance() result: balance: 12.50",
        tool_calls=[_call("u1")],
    ),
    _msg(MessageRole.ASSISTANT, "Your card is blocked."),
]


class TestProjections:
    def test_a_step_is_known_by_the_tool_messages_answering_it(self) -> None:
        assert user_tool_step_call_ids(ISOLATED_TRANSCRIPT) == frozenset({"u1"})
        assert user_tool_step_call_ids(SHARED_TRANSCRIPT) == frozenset()

    def test_the_agent_reads_neither_the_step_nor_its_result(self) -> None:
        assert [(m.role, m.content) for m in agent_view(ISOLATED_TRANSCRIPT)] == [
            (MessageRole.USER, "My card was declined."),
            (MessageRole.ASSISTANT, "Can you check your balance?"),
            (MessageRole.USER, "It says 12.50."),
            (MessageRole.ASSISTANT, "Let me look at the card."),
            (MessageRole.TOOL, "card: blocked"),
            (MessageRole.ASSISTANT, "Your card is blocked."),
        ]

    def test_a_stored_transcript_reads_by_the_same_rule(self) -> None:
        """The judge and the trace projection read bundle dicts through one
        extractor; role case and a malformed call entry do not change the shape."""
        stored = [
            {"role": "USER", "content": "", "tool_calls": [{"id": "u1"}, "not-a-call"]},
            {"role": "tool", "content": "12.50", "tool_call_id": "u1"},
            {"role": "user", "content": "It says 12.50."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a1"}]},
            {"role": "tool", "content": "card: blocked", "tool_call_id": "a1"},
        ]

        assert turn_shape_of(stored[0]) == TurnShape(role="user", call_ids=("u1",))
        assert turn_shape_of(stored[1]) == TurnShape(role="tool", answers="u1")
        assert user_tool_step_positions_of(stored) == frozenset({0, 1})

    def test_the_agent_view_leaves_a_shared_transcript_alone(self) -> None:
        assert agent_view(SHARED_TRANSCRIPT) == SHARED_TRANSCRIPT

    def test_only_the_isolated_view_drops_agent_text_beside_its_calls(self) -> None:
        """An agent message that calls tools is addressed to the environment, so the
        isolated simulator never reads it; the shared view keeps its text, as the
        shared simulator always has."""
        transcript = [
            _msg(MessageRole.USER, "My card was declined."),
            _msg(MessageRole.ASSISTANT, "Let me look.", tool_calls=[_call("a1", "get_card")]),
            _msg(MessageRole.TOOL, "card: blocked", tool_call_id="a1"),
            _msg(MessageRole.ASSISTANT, "Your card is blocked."),
        ]

        assert [(m.role, m.content) for m in shared_view(transcript)] == [
            (MessageRole.ASSISTANT, "My card was declined."),
            (MessageRole.USER, "Let me look.\n\nYour card is blocked."),
        ]
        assert [(m.role, m.content) for m in simulator_view(transcript)] == [
            (MessageRole.ASSISTANT, "My card was declined."),
            (MessageRole.USER, "Your card is blocked."),
        ]

    def test_a_shared_turn_reusing_an_agent_call_id_is_not_a_step(self) -> None:
        """A bundle recorded before call ids were unique across actors can give a
        ``shared`` user call the raw id of an agent call. The step rule reads the
        TOOL messages right after the user message, so the agent's own result
        elsewhere does not turn the user's turn into a step."""
        legacy = [
            _msg(MessageRole.USER, "Hi."),
            _msg(MessageRole.ASSISTANT, "", tool_calls=[_call("call_1", "get_card")]),
            _msg(MessageRole.TOOL, "card: blocked", tool_call_id="call_1"),
            _msg(MessageRole.ASSISTANT, "Can you check your balance?"),
            _msg(
                MessageRole.USER,
                "Let me check that.\n\ncheck_balance() result: 12.50",
                tool_calls=[_call("call_1")],
            ),
            _msg(MessageRole.ASSISTANT, "Your card is blocked."),
        ]

        assert user_tool_step_call_ids(legacy) == frozenset()
        assert agent_view(legacy) == legacy
        kinds = [event.kind for event in build_trial_timeline(legacy, [], None).events]
        assert kinds.count(TraceEventKind.USER_MESSAGE) == 2

    def test_a_step_needs_every_call_answered_right_after_it(self) -> None:
        answered_later = [
            _msg(MessageRole.USER, "", tool_calls=[_call("u1"), _call("u2", "list_cards")]),
            _msg(MessageRole.TOOL, "balance: 12.50", tool_call_id="u1"),
            _msg(MessageRole.ASSISTANT, "Hm."),
            _msg(MessageRole.TOOL, "cards: [visa]", tool_call_id="u2"),
        ]

        assert user_tool_step_call_ids(answered_later) == frozenset()

    def test_the_simulator_sees_its_own_steps_and_none_of_the_agents(self) -> None:
        view = simulator_view(ISOLATED_TRANSCRIPT)

        assert [(m.role, m.content, m.tool_calls, m.tool_call_id) for m in view] == [
            (MessageRole.ASSISTANT, "My card was declined.", None, None),
            (MessageRole.USER, "Can you check your balance?", None, None),
            (MessageRole.ASSISTANT, "", [_call("u1")], None),
            (MessageRole.TOOL, "balance: 12.50", None, "u1"),
            (MessageRole.ASSISTANT, "It says 12.50.", None, None),
            (MessageRole.USER, "Your card is blocked.", None, None),
        ]

    def test_an_agent_message_carrying_calls_is_dropped_with_its_text(self) -> None:
        """A message with calls is addressed to the environment, so its prose is
        not a turn the customer answers — the reference harness drops it too."""
        view = simulator_view(ISOLATED_TRANSCRIPT)
        assert "Let me look at the card." not in [m.content for m in view]

    def test_text_turns_are_joined_but_a_step_is_never_joined(self) -> None:
        transcript = [
            _msg(MessageRole.ASSISTANT, "Hello."),
            _msg(MessageRole.ASSISTANT, "What do you need?"),
            _msg(MessageRole.USER, "One moment.", tool_calls=[_call("u1")]),
            _msg(MessageRole.TOOL, "ok", tool_call_id="u1"),
            _msg(MessageRole.USER, "", tool_calls=[_call("u2")]),
            _msg(MessageRole.TOOL, "ok", tool_call_id="u2"),
        ]

        view = simulator_view(transcript)

        assert [(m.role, m.content) for m in view] == [
            (MessageRole.USER, "Hello.\n\nWhat do you need?"),
            (MessageRole.ASSISTANT, "One moment."),
            (MessageRole.TOOL, "ok"),
            (MessageRole.ASSISTANT, ""),
            (MessageRole.TOOL, "ok"),
        ]

    def test_a_step_keeps_the_reasoning_it_was_generated_with(self) -> None:
        reasoning = StructuredReasoning(summary="Check the balance before answering.")
        step = _msg(MessageRole.USER, "", tool_calls=[_call("u1")], reasoning=reasoning)
        answer = _msg(MessageRole.TOOL, "ok", tool_call_id="u1")

        assert simulator_view([step, answer])[0].reasoning is reasoning


class TestUserToolTurnRule:
    def test_it_is_read_off_the_resolved_actor(self) -> None:
        config = UserSimulatorConfig(tool_turns="isolated", max_tool_steps=3)
        assert UserToolTurnRule.from_config(config) == UserToolTurnRule("isolated", 3)

    def test_the_default_is_shared(self) -> None:
        assert not UserToolTurnRule().isolated
        assert UserToolTurnRule.from_config(UserSimulatorConfig()) == UserToolTurnRule()

    def test_a_turn_with_no_steps_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            UserToolTurnRule("isolated", 0)


# ---------------------------------------------------------------------------
# Whole trials
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """The agent's generate seam: queued replies, and a copy of every request's messages.

    A reply is a text, or a :class:`GenerationResult` for a turn that calls tools.
    """

    def __init__(self, *replies: str | GenerationResult) -> None:
        self._replies = list(replies)
        self.requests: list[list[Message]] = []
        self.capabilities = ModelCapabilities()

    def generate(self, *, messages: list[Message], **_: Any) -> GenerationResult:
        self.requests.append(list(messages))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
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
    """A user-side executor that answers every call, optionally raising on one name."""

    def __init__(self, *, raise_on: str | None = None, on_execute: Any = None) -> None:
        self.calls: list[str] = []
        self._raise_on = raise_on
        self._on_execute = on_execute

    def execute(
        self,
        tool_name: str,
        arguments: dict | None = None,
        *,
        call_id: str,
        validation_schema: dict | None = None,
    ) -> ToolResult:
        self.calls.append(tool_name)
        if self._on_execute is not None:
            self._on_execute()
        if tool_name == self._raise_on:
            raise RuntimeError("the user's device dropped its connection")
        return ToolResult(success=True, output=f"{tool_name}: ok")


def _isolated_trial(
    agent: _RecordingAgent,
    user: _QueuedUser,
    *,
    tools: _UserTools | None = None,
    agent_tools: _UserTools | None = None,
    max_steps: int = 10,
    max_turns: int = 50,
    stop_with_text: UserStopWithText = "deliver",
    simulation_max_steps: int | None = None,
    simulation_max_errors: int | None = None,
    user_tool_turns: UserToolTurnRule | None = None,
    episode_timeout_s: int = 600,
) -> TrialRunner:
    return TrialRunner(
        task_id="isolated",
        trial_index=0,
        agent_client=agent,  # type: ignore[arg-type]
        user_simulator=user,  # type: ignore[arg-type]
        tool_executor=agent_tools or MagicMock(),
        tool_schemas=[],
        max_turns=max_turns,
        user_tool_executor=tools or _UserTools(),
        episode_timeout_s=episode_timeout_s,
        user_tool_turns=user_tool_turns or UserToolTurnRule("isolated", max_steps),
        user_stop=UserStopRule(with_text=stop_with_text),
        max_simulation_steps=simulation_max_steps,
        max_environment_errors=simulation_max_errors,
    )


def _roles(messages: list[Message]) -> list[str]:
    return [
        f"{m.role.value}+calls" if m.tool_calls else m.role.value
        for m in messages
        if m.role is not MessageRole.SYSTEM
    ]


class TestIsolatedTurns:
    @pytest.mark.parametrize("limit", ["steps", "errors"])
    def test_simulation_budget_refuses_shared_user_tool_turns(self, limit: str) -> None:
        agent = _RecordingAgent("Unreached.")
        user = _QueuedUser(_say("Unreached."))

        with pytest.raises(ValueError, match="require isolated user-tool turns"):
            _isolated_trial(
                agent,
                user,
                user_tool_turns=UserToolTurnRule("shared"),
                simulation_max_steps=200 if limit == "steps" else None,
                simulation_max_errors=10 if limit == "errors" else None,
            )

    def test_two_hundredth_message_precedes_max_turns_safety_cap(self) -> None:
        agent = _RecordingAgent("Still here.")
        user = _QueuedUser(*(_say("Again.") for _ in range(99)))

        trajectory = _isolated_trial(agent, user, max_turns=100, simulation_max_steps=200).run(
            "System", "Hi"
        )

        assert trajectory.termination_reason is TerminationReason.MAX_STEPS
        assert trajectory.simulation_steps == 200
        assert len(agent.requests) == 100
        assert len(user.contexts) == 99

    def test_stop_token_on_step_boundary_is_overridden_by_native_limit(self) -> None:
        agent = _RecordingAgent("Thanks.")
        user = _QueuedUser(_say("###STOP###"))

        trajectory = _isolated_trial(agent, user, simulation_max_steps=3, stop_with_text="end").run(
            "System", "Hi"
        )

        assert trajectory.termination_reason is TerminationReason.MAX_STEPS
        assert trajectory.simulation_steps == 3
        assert trajectory.messages[-2].content == "###STOP###"

    def test_agent_tool_batch_finishes_before_simulation_step_limit(self) -> None:
        agent = _RecordingAgent(_tool_step(_call("a1"), _call("a2", "list_cards")))
        user = _QueuedUser(_say("unreached"))
        tools = _UserTools()

        trajectory = _isolated_trial(agent, user, agent_tools=tools, simulation_max_steps=3).run(
            "System", "Hi"
        )

        assert trajectory.termination_reason is TerminationReason.MAX_STEPS
        assert trajectory.status is TrialStatus.COMPLETED
        assert trajectory.simulation_steps == 3
        assert trajectory.environment_errors == 0
        assert tools.calls == ["check_balance", "list_cards"]
        assert (
            len([message for message in trajectory.messages if message.role is MessageRole.TOOL])
            == 2
        )
        assert user.contexts == []

    def test_user_tool_batch_finishes_before_simulation_step_limit(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1"), _call("u2", "list_cards")))
        tools = _UserTools()

        trajectory = _isolated_trial(agent, user, tools=tools, simulation_max_steps=4).run(
            "System", "Hi"
        )

        assert trajectory.termination_reason is TerminationReason.MAX_STEPS
        assert trajectory.simulation_steps == 4
        assert tools.calls == ["check_balance", "list_cards"]
        assert len(user.contexts) == 1

    def test_environment_errors_count_each_failed_result_in_a_single_batch(self) -> None:
        class ErrorTools(_UserTools):
            def execute(
                self,
                tool_name: str,
                arguments: dict | None = None,
                *,
                call_id: str,
                validation_schema: dict | None = None,
            ) -> ToolResult:
                self.calls.append(tool_name)
                return ToolResult(
                    success=True,
                    output="Error: declared by the environment",
                    status=ToolExecutionStatus.ENVIRONMENT_ERROR,
                )

        tools = ErrorTools()
        agent = _RecordingAgent(_tool_step(_call("a1"), _call("a2", "list_cards")))
        user = _QueuedUser(_say("unreached"))

        trajectory = _isolated_trial(agent, user, agent_tools=tools, simulation_max_errors=2).run(
            "System", "Hi"
        )

        assert trajectory.termination_reason is TerminationReason.TOO_MANY_ERRORS
        assert trajectory.environment_errors == 2
        assert trajectory.simulation_steps == 3
        assert [
            message.tool_status
            for message in trajectory.messages
            if message.role is MessageRole.TOOL
        ] == [
            ToolExecutionStatus.ENVIRONMENT_ERROR,
            ToolExecutionStatus.ENVIRONMENT_ERROR,
        ]
        assert user.contexts == []

    def test_the_agent_never_reads_a_user_tool_step(self) -> None:
        agent = _RecordingAgent("How can I help?", "Thanks, one moment.")
        user = _QueuedUser(_tool_step(_call("u1")), _say("It says 12.50."), _say("###STOP###"))
        runner = _isolated_trial(agent, user)

        trajectory = runner.run("System", "My card was declined.")

        assert _roles(trajectory.messages) == [
            "user",
            "assistant",
            "user+calls",
            "tool",
            "user",
            "assistant",
        ]
        for request in agent.requests:
            assert _roles(request) == [role for role in _roles(request) if "tool" not in role]
            assert not any(m.role is MessageRole.USER and m.tool_calls for m in request)
        assert [m.content for m in agent.requests[-1]] == [
            "My card was declined.",
            "How can I help?",
            "It says 12.50.",
        ]
        assert trajectory.termination_reason == TerminationReason.USER_STOP

    def test_the_simulator_is_asked_again_with_its_step_in_view(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1")), _say("###STOP###"))

        _isolated_trial(agent, user).run("System", "My card was declined.")

        second_ask = user.contexts[1]
        assert _roles(second_ask) == ["user", "assistant", "user+calls", "tool"]
        assert second_ask[-1].content == "check_balance: ok"

    def test_a_step_is_recorded_as_the_users_call(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1"), _call("u2", "list_cards")), _say("###STOP###"))
        runner = _isolated_trial(agent, user)

        runner.run("System", "Hi")

        records = runner.tool_call_recorder.recorded
        assert [(r.tool_name, r.executor) for r in records] == [
            ("check_balance", ToolExecutorIdentity.USER),
            ("list_cards", ToolExecutorIdentity.USER),
        ]
        step, *answers = [m for m in runner.messages if m.tool_calls or m.tool_call_id]
        assert [m.tool_call_id for m in answers] == [c.id for c in step.tool_calls]

    def test_a_stop_token_inside_a_step_is_not_a_stop(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(
            _tool_step(_call("u1"), text="###STOP###"),
            _say("All sorted, thanks. ###STOP###"),
            _say("###STOP###"),
        )
        runner = _isolated_trial(agent, user)

        trajectory = runner.run("System", "Hi")

        assert len(runner.tool_call_recorder.recorded) == 1
        assert "All sorted, thanks." in [m.content for m in trajectory.messages]
        assert trajectory.termination_reason == TerminationReason.USER_STOP

    def test_a_step_is_never_given_filler_text(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1")), _say("###STOP###"))

        trajectory = _isolated_trial(agent, user).run("System", "Hi")

        step = next(m for m in trajectory.messages if m.tool_calls)
        assert step.content == ""

    def test_a_step_past_the_limit_ends_the_dialogue_without_running_its_calls(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(*(_tool_step(_call(f"u{n}")) for n in range(3)))
        tools = _UserTools()
        runner = _isolated_trial(agent, user, tools=tools, max_steps=2)

        trajectory = runner.run("System", "Hi")

        assert trajectory.termination_reason == TerminationReason.USER_TOOL_LOOP_LIMIT
        assert trajectory.status == TrialStatus.COMPLETED
        assert len(tools.calls) == 2
        assert _roles(trajectory.messages) == [
            "user",
            "assistant",
            "user+calls",
            "tool",
            "user+calls",
            "tool",
        ]
        assert trajectory.messages[-1].content == (
            "User took 2 tool step(s) without replying; the next step's 1 call(s), "
            "check_balance, were not run. Dialogue terminated."
        )

    def test_the_episode_timeout_is_checked_between_steps(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1")), _say("never asked for"))
        runner: TrialRunner

        def spend_the_episode() -> None:
            runner.episode_timeout_s = 0

        runner = _isolated_trial(agent, user, tools=_UserTools(on_execute=spend_the_episode))

        trajectory = runner.run("System", "Hi")

        assert trajectory.termination_reason == TerminationReason.TIMEOUT
        assert trajectory.status == TrialStatus.TIMEOUT
        assert len(user.contexts) == 1

    def test_a_raising_executor_still_answers_every_call_of_the_step(self) -> None:
        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(_tool_step(_call("u1"), _call("u2", "list_cards")))
        tools = _UserTools(raise_on="check_balance")
        runner = _isolated_trial(agent, user, tools=tools)

        trajectory = runner.run("System", "Hi")

        assert trajectory.status == TrialStatus.ERROR
        step = next(m for m in trajectory.messages if m.tool_calls)
        answers = [m for m in trajectory.messages if m.role is MessageRole.TOOL]
        assert [m.tool_call_id for m in answers] == [c.id for c in step.tool_calls]
        assert answers[0].content == "Error: the user's device dropped its connection"
        assert answers[1].content == "Error: not run, an earlier call of this step raised."
        assert tools.calls == ["check_balance"]

    def test_an_api_error_retry_after_a_raising_step_keeps_the_original_error(self) -> None:
        class ApiErrorTools(_UserTools):
            def execute(
                self,
                tool_name: str,
                arguments: dict | None = None,
                *,
                call_id: str,
                validation_schema: dict | None = None,
            ) -> ToolResult:
                self.calls.append(tool_name)
                if tool_name == "list_cards":
                    raise RuntimeError("card API unreachable")
                return ToolResult(success=True, output=f"{tool_name}: ok")

        agent = _RecordingAgent("How can I help?")
        user = _QueuedUser(
            _tool_step(_call("u1"), _call("u2", "list_cards")),
            _tool_step(_call("u3"), _call("u4", "list_cards")),
        )
        tools = ApiErrorTools()
        runner = _isolated_trial(agent, user, tools=tools, simulation_max_steps=50)

        trajectory = runner.run("System", "Hi")

        assert trajectory.termination_reason is TerminationReason.API_ERROR
        assert "card API unreachable" in trajectory.messages[-1].content
        assert "pending environment batch" not in trajectory.messages[-1].content
        # Both attempts ran their step, and each raising batch is still one
        # environment step: opening, then (agent, user step, batch) per attempt.
        assert tools.calls == ["check_balance", "list_cards"] * 2
        assert trajectory.simulation_steps == 1 + 3 * 2
        assert trajectory.environment_errors == 0

    def test_each_ask_records_its_guard_event_at_its_own_position(self) -> None:
        rejected = [MagicMock(name="defect")]
        agent = _RecordingAgent("How can I help?")
        step = _tool_step(_call("u1"))
        step.guard_rejections = rejected
        reply = _say("###STOP###")
        reply.guard_rejections = rejected
        runner = _isolated_trial(agent, _QueuedUser(step, reply))

        with patch.object(TrialRunner, "_record_user_reply_guard") as record:
            runner.run("System", "Hi")

        assert [c.kwargs["message_index"] for c in record.call_args_list] == [2, 4]


class TestIsolatedOpening:
    def test_native_budget_ends_bootstrap_after_one_hundred_tool_steps(self) -> None:
        agent = _RecordingAgent("unreached")
        user = _QueuedUser(*(_tool_step(_call(f"u{index}")) for index in range(100)))
        tools = _UserTools()

        trajectory = _isolated_trial(
            agent, user, tools=tools, max_steps=100, simulation_max_steps=200
        ).run("System")

        assert trajectory.status is TrialStatus.COMPLETED
        assert trajectory.termination_reason is TerminationReason.MAX_STEPS
        assert trajectory.simulation_steps == 200
        assert len(tools.calls) == 100
        assert len(user.contexts) == 100
        assert agent.requests == []

    def test_tool_steps_before_the_opening_are_recorded_ahead_of_it(self) -> None:
        agent = _RecordingAgent("Sure, let me help.")
        user = _QueuedUser(
            _tool_step(_call("u1")), _say("I need to move my booking."), _say("###STOP###")
        )

        trajectory = _isolated_trial(agent, user).run("System")

        assert _roles(trajectory.messages)[:4] == ["user+calls", "tool", "user", "assistant"]
        assert [m.content for m in agent.requests[0]] == ["I need to move my booking."]
        assert trajectory.first_user_message_source is FirstUserMessageSource.SIMULATOR
        assert [m.content for m in user.contexts[1]] == [GREETING, "", "check_balance: ok"]

    def test_an_opening_past_the_step_limit_refuses_the_trial(self) -> None:
        agent = _RecordingAgent("unreached")
        user = _QueuedUser(_tool_step(_call("u1")), _tool_step(_call("u2")))

        trajectory = _isolated_trial(agent, user, max_steps=1).run("System")

        assert trajectory.status == TrialStatus.ERROR
        assert "more than 1 tool step(s) before its opening" in trajectory.messages[-1].content
        assert agent.requests == []


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


def _simulator(
    tool_turns: str, *results: GenerationResult
) -> tuple[BuiltinUserSimulator, _FakeWire]:
    simulator = BuiltinUserSimulator(mode="llm", llm_config=None, tool_turns=tool_turns)
    wire = _FakeWire(*results)
    simulator.llm_client = wire  # type: ignore[assignment]
    return simulator, wire


class TestIsolatedTurnsDownstream:
    """What an isolated trial hands its readers: the agent's context, the grading
    timeline, a required user action, and the simulator's next request."""

    def test_the_agent_keeps_its_own_tool_traffic_and_the_timeline_agrees(self) -> None:
        agent = _RecordingAgent(
            _tool_step(_call("a1", "get_card")),
            "Can you check your balance?",
            "Your card is blocked.",
        )
        # The user's raw id collides with the agent's; the trial's assigner keys it apart.
        user = _QueuedUser(_tool_step(_call("a1")), _say("It says 12.50."), _say("###STOP###"))
        runner = _isolated_trial(agent, user, agent_tools=_UserTools())

        trajectory = runner.run("System", "My card was declined.")

        assert trajectory.termination_reason == TerminationReason.USER_STOP
        last_request = agent.requests[-1]
        assert [m.tool_call_id for m in last_request if m.role is MessageRole.TOOL] == ["a1"]
        assert not any(m.role is MessageRole.USER and m.tool_calls for m in last_request)
        step = next(m for m in trajectory.messages if m.role is MessageRole.USER and m.tool_calls)
        assert step.tool_calls[0].id == "a1#2"
        timeline = build_trial_timeline(
            trajectory.messages, runner.tool_call_recorder.recorded, trajectory.termination_reason
        )
        assert [event.kind for event in timeline.events] == [
            TraceEventKind.USER_MESSAGE,
            TraceEventKind.ASSISTANT_MESSAGE,
            TraceEventKind.TOOL_CALL,
            TraceEventKind.TOOL_RESULT,
            TraceEventKind.ASSISTANT_MESSAGE,
            TraceEventKind.TOOL_CALL,
            TraceEventKind.TOOL_RESULT,
            TraceEventKind.USER_MESSAGE,
            TraceEventKind.ASSISTANT_MESSAGE,
        ]
        assert [m.role for m in simulator_view(user.contexts[1])] == [
            MessageRole.ASSISTANT,
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
        ]

    @pytest.mark.parametrize(("requestor", "passed"), [("user", True), ("assistant", False)])
    def test_a_required_user_action_is_met_by_a_step(self, requestor: str, passed: bool) -> None:
        agent = _RecordingAgent("Can you check your balance?", "Thanks.")
        user = _QueuedUser(
            _tool_step(_call("u1", account="main")), _say("It says 12.50."), _say("###STOP###")
        )
        runner = _isolated_trial(agent, user)

        trajectory = runner.run("System", "My card was declined.")
        timeline = build_trial_timeline(
            trajectory.messages, runner.tool_call_recorder.recorded, trajectory.termination_reason
        )
        rules = TranscriptRulesConfig(
            required_actions=[
                RequiredAction(
                    action_id="check",
                    requestor=requestor,
                    name="check_balance",
                    arguments={"account": "main"},
                )
            ]
        )

        assert evaluate_transcript_rules(timeline, rules).passed is passed

    def test_a_step_carries_the_simulators_reasoning_into_its_next_ask(self) -> None:
        """The runner records a step's reasoning, so a thinking model's signed blocks
        reach the simulator's next request (see the provider test below)."""
        thinking = StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text="Check first.", signature="sig-1"),),
            transport="anthropic_native",
        )
        step = GenerationResult(text="", tool_calls=[_call("u1")], reasoning=thinking)
        user = _QueuedUser(step, _say("It says 12.50."), _say("###STOP###"))
        runner = _isolated_trial(_RecordingAgent("Can you check your balance?", "Thanks."), user)

        trajectory = runner.run("System", "My card was declined.")

        recorded = next(
            m for m in trajectory.messages if m.role is MessageRole.USER and m.tool_calls
        )
        assert recorded.reasoning == thinking
        replayed = next(m for m in simulator_view(user.contexts[1]) if m.tool_calls)
        assert replayed.reasoning == thinking

    def test_an_opening_that_overruns_the_budget_ends_on_the_first_turn(self) -> None:
        """Turn 0 runs before the loop, so no timeout check interrupts its steps; the
        loop's first turn ends the trial with ``timeout`` before the agent speaks."""
        agent = _RecordingAgent("never generated")
        user = _QueuedUser(_tool_step(_call("u1")), _tool_step(_call("u2")), _say("I need help."))
        runner: TrialRunner

        def spend_the_budget() -> None:
            runner.episode_timeout_s = 0

        runner = _isolated_trial(agent, user, tools=_UserTools(on_execute=spend_the_budget))

        trajectory = runner.run("System")

        assert len(user.contexts) == 3
        assert agent.requests == []
        assert trajectory.termination_reason == TerminationReason.TIMEOUT
        assert _roles(trajectory.messages) == ["user+calls", "tool", "user+calls", "tool", "user"]


class TestIsolatedSimulator:
    def test_a_step_keeps_the_models_empty_text(self) -> None:
        simulator, _ = _simulator("isolated", _tool_step(_call("u1")))

        result = simulator.reply(ISOLATED_TRANSCRIPT[:2])

        assert result.text == ""
        assert result.filler_substituted is False

    def test_shared_turns_still_get_the_filler(self) -> None:
        simulator, _ = _simulator("shared", _tool_step(_call("u1")))

        assert simulator.reply(ISOLATED_TRANSCRIPT[:2]).filler_substituted is True

    def test_a_request_may_end_on_the_steps_results(self) -> None:
        simulator, wire = _simulator("isolated", _say("It says 12.50."))

        simulator.reply(ISOLATED_TRANSCRIPT[:4])

        assert [m.role for m in wire.requests[0]] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
        ]
        assert wire.requests[0][0].content == GREETING


# ---------------------------------------------------------------------------
# What providers are sent
# ---------------------------------------------------------------------------


def _completion_response() -> MagicMock:
    message = MagicMock()
    message.content = "It says 12.50."
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
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The ``messages`` an isolated simulator sends after one tool step and its result."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-tool-turns")
    simulator = BuiltinUserSimulator(
        mode="llm",
        llm_config=ModelConfig(provider="openrouter", name="openai/gpt-4o-mini"),
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "check_balance",
                    "description": "Read the balance.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_turns="isolated",
    )
    with (
        patch(
            "tolokaforge.core.llm.client.completion", return_value=_completion_response()
        ) as completion,
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        simulator.reply(ISOLATED_TRANSCRIPT[:4])
    return completion.call_args.kwargs["messages"]


def test_a_reasoning_simulators_step_replays_its_signed_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic refuses a tool-use turn replayed without the thinking that preceded
    it, so a step carries its reasoning back into the simulator's next request."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sk-tool-turns")
    thinking = StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text="Check first.", signature="sig-1"),),
        transport="anthropic_native",
    )
    step = _msg(MessageRole.USER, "", tool_calls=[_call("u1")], reasoning=thinking)
    simulator = BuiltinUserSimulator(
        mode="llm",
        llm_config=ModelConfig(provider="anthropic", name="claude-opus-4-7"),
        tool_turns="isolated",
    )
    with (
        patch(
            "tolokaforge.core.llm.client.completion", return_value=_completion_response()
        ) as completion,
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        simulator.reply([*ISOLATED_TRANSCRIPT[:2], step, ISOLATED_TRANSCRIPT[3]])

    replayed = next(m for m in completion.call_args.kwargs["messages"] if m.get("tool_calls"))
    assert [b["signature"] for b in replayed["thinking_blocks"]] == ["sig-1"]


class TestProviderShapes:
    """The flipped request with a tool step, through each provider family's rules."""

    def test_openai_answers_every_call_right_after_the_turn_that_made_it(
        self, sent_messages: list[dict[str, Any]]
    ) -> None:
        roles = [m["role"] for m in sent_messages]
        assert roles == ["system", "user", "assistant", "user", "assistant", "tool"]
        step, answer = sent_messages[-2], sent_messages[-1]
        assert [c["id"] for c in step["tool_calls"]] == [answer["tool_call_id"]] == ["u1"]
        assert json.loads(step["tool_calls"][0]["function"]["arguments"]) == {}

    def test_anthropic_pairs_the_tool_use_with_its_result_and_alternates(
        self, sent_messages: list[dict[str, Any]]
    ) -> None:
        converted = anthropic_messages_pt(
            [m for m in sent_messages if m["role"] != "system"],
            model="claude-sonnet-4-5",
            llm_provider="anthropic",
        )

        roles = [m["role"] for m in converted]
        assert roles == ["user", "assistant", "user", "assistant", "user"]
        tool_use = [b for b in converted[3]["content"] if b["type"] == "tool_use"]
        tool_result = [b for b in converted[4]["content"] if b["type"] == "tool_result"]
        assert [b["id"] for b in tool_use] == [b["tool_use_id"] for b in tool_result] == ["u1"]

    def test_gemini_pairs_the_function_call_with_its_response(
        self, sent_messages: list[dict[str, Any]]
    ) -> None:
        contents = _gemini_convert_messages_with_history(
            [m for m in sent_messages if m["role"] != "system"]
        )

        roles = [c["role"] for c in contents]
        assert roles == ["user", "model", "user", "model", "user"]
        call_parts = [p for p in contents[3]["parts"] if "function_call" in p]
        response_parts = [p for p in contents[4]["parts"] if "function_response" in p]
        assert [p["function_call"]["name"] for p in call_parts] == ["check_balance"]
        assert [p["function_response"]["name"] for p in response_parts] == ["check_balance"]


def test_a_thinking_step_with_two_calls_pairs_both_results_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step with parallel calls, built for an Anthropic model and converted by
    litellm's own Anthropic transformation: the signed thinking leads the step's
    turn, and one user turn answers both calls, in order."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sk-tool-turns")
    thinking = StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text="Check both.", signature="sig-1"),),
        transport="anthropic_native",
    )
    transcript = [
        _msg(MessageRole.USER, "My card was declined."),
        _msg(MessageRole.ASSISTANT, "Can you check your balance and cards?"),
        _msg(
            MessageRole.USER,
            "",
            tool_calls=[_call("u1"), _call("u2", "list_cards")],
            reasoning=thinking,
        ),
        _msg(MessageRole.TOOL, "balance: 12.50", tool_call_id="u1"),
        _msg(MessageRole.TOOL, "cards: [visa]", tool_call_id="u2"),
    ]
    simulator = BuiltinUserSimulator(
        mode="llm",
        llm_config=ModelConfig(provider="anthropic", name="claude-opus-4-7"),
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("check_balance", "list_cards")
        ],
        tool_turns="isolated",
    )
    with (
        patch(
            "tolokaforge.core.llm.client.completion", return_value=_completion_response()
        ) as completion,
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        simulator.reply(transcript)

    converted = anthropic_messages_pt(
        [m for m in completion.call_args.kwargs["messages"] if m["role"] != "system"],
        model="claude-opus-4-7",
        llm_provider="anthropic",
    )
    assert [m["role"] for m in converted] == ["user", "assistant", "user", "assistant", "user"]
    step_blocks = [block["type"] for block in converted[3]["content"]]
    assert step_blocks[0] == "thinking" and step_blocks.count("tool_use") == 2
    assert [block["tool_use_id"] for block in converted[4]["content"]] == ["u1", "u2"]
