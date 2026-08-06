"""UserSimulator._llm_reply context construction — the simulator's view of
the shared transcript.

The simulator converses from the customer's seat: its own past USER messages
replay as ``assistant`` turns and the agent's ASSISTANT messages as ``user``
turns. Three invariants locked here:

1. The trial's seeded opening (a USER message at index 0, which flips to
   ``assistant``) is *preserved* behind a synthetic agent-side greeting —
   not trimmed. Trimming it made the simulator believe it never asked, so
   on its first live turn it restarted the conversation verbatim after the
   agent had already answered (observed in CBT-021: duplicate tickets,
   ``state_checks`` fail).
2. Agent tool-call turns with no dialogue text are skipped instead of
   replaying as empty ``user`` turns.
3. TOOL and SYSTEM messages never reach the simulator.

Uses a stub client capturing ``generate`` kwargs — no live API traffic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from tolokaforge.core.llm.client import SIMULATOR_GREETING, GenerationResult, UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _CapturingClient:
    """Stands in for LLMClient; records the messages the simulator sends."""

    def __init__(self) -> None:
        self.messages: list[Message] | None = None

    def generate(self, system: str, messages: list[Message], **kwargs: Any) -> GenerationResult:
        self.messages = messages
        return GenerationResult(text="Understood, thanks.", tool_calls=[])


def _sim_with_capture() -> tuple[UserSimulator, _CapturingClient]:
    sim = UserSimulator(
        mode="llm",
        llm_config=ModelConfig(provider="mock", name="user-sim-mock"),
        backstory="Ask whether a corrected 1099-B was issued for tax year 2024.",
    )
    client = _CapturingClient()
    sim.llm_client = client  # type: ignore[assignment]
    return sim, client


def test_seeded_opening_survives_into_simulator_context() -> None:
    """The flipped context keeps the simulator's own opening turn."""
    sim, client = _sim_with_capture()
    opening = "Hi, has a corrected 1099-B been issued for my account for 2024?"
    context = [
        Message(role=MessageRole.USER, content=opening, ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),  # tool-call turn
        Message(role=MessageRole.TOOL, content='{"documents": []}', ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="No corrected 1099-B is on file.", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    roles = [m.role for m in client.messages]
    contents = [m.content for m in client.messages]
    assert roles == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER]
    assert contents == [
        SIMULATOR_GREETING,
        opening,
        "No corrected 1099-B is on file.",
    ]


def test_first_message_is_user_role() -> None:
    """Provider constraint: the simulator's request starts with a user turn."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Answer.", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert client.messages[0].role == MessageRole.USER


def test_empty_and_non_dialogue_turns_are_skipped() -> None:
    """Tool-call-only agent turns, TOOL results, and SYSTEM notes stay out."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="   ", ts=_TS),
        Message(role=MessageRole.TOOL, content="tool output", ts=_TS),
        Message(role=MessageRole.SYSTEM, content="system note", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Answer.", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert [m.content for m in client.messages] == [
        SIMULATOR_GREETING,
        "Opening request.",
        "Answer.",
    ]


def test_multi_turn_alternation_preserved() -> None:
    """A full back-and-forth flips cleanly with every dialogue turn intact."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="First answer.", ts=_TS),
        Message(role=MessageRole.USER, content="Follow-up question.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Second answer.", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert [(m.role, m.content) for m in client.messages] == [
        (MessageRole.USER, SIMULATOR_GREETING),
        (MessageRole.ASSISTANT, "Opening request."),
        (MessageRole.USER, "First answer."),
        (MessageRole.ASSISTANT, "Follow-up question."),
        (MessageRole.USER, "Second answer."),
    ]


def test_no_greeting_when_context_starts_agent_side() -> None:
    """A conversation the agent opened flips to a leading user turn — no
    synthetic greeting is inserted."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.ASSISTANT, content="Hello, how can I help?", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert [(m.role, m.content) for m in client.messages] == [
        (MessageRole.USER, "Hello, how can I help?"),
    ]
