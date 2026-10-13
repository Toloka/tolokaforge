"""A stall resample drops the deliberation that stalled, and nothing earlier.

``_generate(replay_reasoning=False)`` exists so a resample does not re-roll the
reasoning that just produced an actionless turn. Reaching further back is wrong
on two counts: earlier assistant turns reasoned their way into actions, so their
deliberation is not what is being re-rolled; and on a route whose codec replays
reasoning, rewriting them changes every byte after the first message, which
discards the provider's cached prefix and bills the whole context afresh.

Routes exist where the echo is not optional — Moonshot documents preserved
thinking as mandatory on ``kimi-k2.7-code`` — so the blast radius is a protocol
question, not only a cost one.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.loop import _without_last_assistant_reasoning
from tolokaforge.core.models import Message, MessageRole
from tolokaforge.core.models.trajectory import StructuredReasoning

pytestmark = pytest.mark.unit


def _assistant(text: str, *, reasoning: str | None) -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content=text,
        reasoning=(StructuredReasoning(summary=reasoning) if reasoning is not None else None),
    )


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def test_only_the_most_recent_assistant_message_loses_its_reasoning() -> None:
    wire = [
        _user("task"),
        _assistant("first", reasoning="thought one"),
        _user("tool result"),
        _assistant("second", reasoning="thought two"),
        _user("tool result"),
        _assistant("stalled", reasoning="thought three"),
    ]

    stripped = _without_last_assistant_reasoning(wire)

    assert stripped[-1].reasoning is None
    # Everything earlier is untouched, and is the same object — so the prefix the
    # provider cached is byte-identical.
    assert [s is w for s, w in zip(stripped[:-1], wire[:-1])] == [True] * 5
    assert stripped[1].reasoning is not None
    assert stripped[3].reasoning is not None


def test_the_original_list_is_not_mutated() -> None:
    wire = [_user("task"), _assistant("stalled", reasoning="thought")]

    _without_last_assistant_reasoning(wire)

    assert wire[1].reasoning is not None


def test_a_reasonless_last_assistant_message_returns_the_same_list() -> None:
    wire = [
        _user("task"),
        _assistant("first", reasoning="thought one"),
        _user("tool result"),
        _assistant("stalled", reasoning=None),
    ]

    # A route that never replays reasoning must send the object it would have
    # sent anyway, rather than an equal copy.
    assert _without_last_assistant_reasoning(wire) is wire


def test_a_history_with_no_assistant_message_is_returned_unchanged() -> None:
    wire = [_user("task")]

    assert _without_last_assistant_reasoning(wire) is wire
