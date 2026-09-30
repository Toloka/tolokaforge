"""The rubric judge's transcript rendering reads a user's tool steps as the user's.

Under ``actors.user.tool_turns: isolated`` a user reply that calls tools is
recorded as a step: a USER message with the calls and no text, then one TOOL
message answering each. Rendered like any other message it prints no role line,
so its calls would sit under the agent's previous line and read as the agent's.
These tests drive the wire the judge actually receives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tolokaforge.core.grading.judge import format_transcript
from tolokaforge.core.grading.transcript_wire import (
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.models import Message, MessageRole, ToolCall

pytestmark = pytest.mark.unit


def _judge_view(messages: list[Message]) -> str:
    wire = encode_transcript_wire(SimpleNamespace(messages=messages), "SYS")
    _, transcript = split_leading_system_message(json.loads(wire))
    return format_transcript(transcript)


def _message(role: MessageRole, content: str = "", **fields: object) -> Message:
    return Message(role=role, content=content, **fields)


def test_a_user_tool_step_and_its_result_are_labelled_as_the_users() -> None:
    rendered = _judge_view(
        [
            _message(MessageRole.USER, "My card was declined."),
            _message(MessageRole.ASSISTANT, "Can you check your balance?"),
            _message(
                MessageRole.USER,
                tool_calls=[ToolCall(id="u1", name="check_balance", arguments={})],
            ),
            _message(MessageRole.TOOL, "balance: 12.50", tool_call_id="u1"),
            _message(MessageRole.USER, "It says 12.50."),
        ]
    )

    assert rendered.splitlines() == [
        "USER: My card was declined.",
        "ASSISTANT: Can you check your balance?",
        "USER (tool step, not shown to the agent)",
        "  -> tool_call check_balance({})",
        "TOOL (result for the user): balance: 12.50",
        "USER: It says 12.50.",
    ]


def test_a_step_that_carries_text_keeps_it_under_its_label() -> None:
    rendered = _judge_view(
        [
            _message(MessageRole.USER, "Hi."),
            _message(MessageRole.ASSISTANT, "Hello."),
            _message(
                MessageRole.USER,
                "Checking.",
                tool_calls=[ToolCall(id="u1", name="check_balance", arguments={})],
            ),
            _message(MessageRole.TOOL, "", tool_call_id="u1"),
        ]
    )

    assert rendered.splitlines()[2:] == [
        "USER (tool step, not shown to the agent): Checking.",
        "  -> tool_call check_balance({})",
        "TOOL (result for the user): (tool result)",
    ]


def test_a_transcript_without_steps_renders_as_before() -> None:
    """A ``shared`` user turn keeps its results in its own text and no TOOL message
    answers it, so it is not a step; the agent's tool traffic is unlabelled."""
    rendered = _judge_view(
        [
            _message(
                MessageRole.USER,
                "Let me check that.\n\ncheck_balance() result: 12.50",
                tool_calls=[ToolCall(id="u1", name="check_balance", arguments={})],
            ),
            _message(
                MessageRole.ASSISTANT,
                tool_calls=[ToolCall(id="a1", name="get_card", arguments={"n": 1})],
            ),
            _message(MessageRole.TOOL, "card: blocked", tool_call_id="a1"),
            _message(MessageRole.ASSISTANT, "Your card is blocked."),
        ]
    )

    assert rendered.splitlines() == [
        "USER: Let me check that.",
        "",
        "check_balance() result: 12.50",
        "  -> tool_call check_balance({})",
        '  -> tool_call get_card({"n": 1})',
        "TOOL: card: blocked",
        "ASSISTANT: Your card is blocked.",
    ]
