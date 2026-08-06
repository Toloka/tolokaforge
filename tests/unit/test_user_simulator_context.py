"""UserSimulator._llm_reply context construction — the simulator's view of
the shared transcript.

The simulator converses from the customer's seat: its own past USER messages
replay as ``assistant`` turns and the agent's ASSISTANT messages as ``user``
turns. Four invariants locked here:

1. The trial's seeded opening (a USER message at index 0, which flips to
   ``assistant``) is *preserved* behind a synthetic agent-side greeting —
   not trimmed. Trimming it made the simulator believe it never asked, so
   on its first live turn it restarted the conversation verbatim after the
   agent had already answered (observed in production runs: duplicate
   side effects, ``state_checks`` fail).
2. Agent tool-call turns with no dialogue text are skipped instead of
   replaying as empty ``user`` turns, and adjacent same-role turns are
   coalesced so the request alternates strictly.
3. TOOL and SYSTEM messages never reach the simulator.
4. The request always ends on a user-role turn the simulator can answer;
   a transcript whose last agent turn carries no dialogue text raises
   instead of sending a trailing assistant-role message (a prefill of the
   simulator's own words) or an empty request. A refused dispatch stamps
   no ``last_system_prompt``.

The shared ``context`` list itself is never mutated — the greeting exists
only in the simulator's private request, never in the trial transcript.

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
        backstory="Ask whether a replacement warranty certificate was issued.",
    )
    client = _CapturingClient()
    # UserSimulator has no client-injection seam; the stub matches generate() only.
    sim.llm_client = client  # type: ignore[assignment]
    return sim, client


def test_seeded_opening_survives_into_simulator_context() -> None:
    """The flipped context keeps the simulator's own opening turn, and the
    shared transcript is left untouched."""
    sim, client = _sim_with_capture()
    opening = "Hi, has a replacement warranty certificate been issued for my order?"
    context = [
        Message(role=MessageRole.USER, content=opening, ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),  # tool-call turn
        Message(role=MessageRole.TOOL, content='{"certificates": []}', ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="No replacement is on file.", ts=_TS),
    ]
    before = [(m.role, m.content) for m in context]

    sim.reply(context)

    assert client.messages is not None
    roles = [m.role for m in client.messages]
    contents = [m.content for m in client.messages]
    assert roles == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER]
    assert contents == [
        SIMULATOR_GREETING,
        opening,
        "No replacement is on file.",
    ]
    # The greeting lives only in the simulator's private request — the shared
    # transcript must come back exactly as it went in.
    assert [(m.role, m.content) for m in context] == before


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


def test_raises_when_final_agent_turn_has_no_dialogue_text() -> None:
    """A transcript ending on a dialogue-free agent turn is unanswerable —
    silently sending it would trail the simulator's own assistant-role
    message, which providers treat as a prefill to continue. The refused
    dispatch stamps no ``last_system_prompt``: ``prompts.yaml`` must never
    name a prompt that drove no generation."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),
    ]

    with pytest.raises(RuntimeError, match="no agent dialogue turn to answer"):
        sim.reply(context)
    assert client.messages is None
    assert sim.last_system_prompt is None


def test_raises_when_no_turn_carries_dialogue_text() -> None:
    """All-dialogue-free transcripts must not produce an empty request."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),
        Message(role=MessageRole.TOOL, content='{"ok": true}', ts=_TS),
    ]

    with pytest.raises(RuntimeError, match="no agent dialogue turn to answer"):
        sim.reply(context)
    assert client.messages is None
    assert sim.last_system_prompt is None


def test_adjacent_same_role_turns_coalesce() -> None:
    """Two agent dialogue turns separated only by a skipped tool-call turn
    coalesce into one user-role turn — strict-alternation providers reject
    back-to-back same-role messages."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Let me check that.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="", ts=_TS),  # tool-call turn
        Message(role=MessageRole.TOOL, content='{"ok": true}', ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Here is your answer.", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert [(m.role, m.content) for m in client.messages] == [
        (MessageRole.USER, SIMULATOR_GREETING),
        (MessageRole.ASSISTANT, "Opening request."),
        (MessageRole.USER, "Let me check that.\n\nHere is your answer."),
    ]


def test_whitespace_user_turn_dropped_and_alternation_preserved() -> None:
    """A whitespace-only simulator reply recorded in the shared transcript is
    skipped, and the agent turns it separated coalesce — the request never
    carries back-to-back same-role messages."""
    sim, client = _sim_with_capture()
    context = [
        Message(role=MessageRole.USER, content="Opening request.", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="First answer.", ts=_TS),
        Message(role=MessageRole.USER, content="   ", ts=_TS),
        Message(role=MessageRole.ASSISTANT, content="Anything else?", ts=_TS),
    ]

    sim.reply(context)

    assert client.messages is not None
    assert [(m.role, m.content) for m in client.messages] == [
        (MessageRole.USER, SIMULATOR_GREETING),
        (MessageRole.ASSISTANT, "Opening request."),
        (MessageRole.USER, "First answer.\n\nAnything else?"),
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
