"""Older observations collapse on the wire; the window holds still between advances.

``tool_output_max_chars`` bounds one observation once. It does nothing about the
cost of *replaying* that observation on every later turn, which is where the
money goes in a long trial: a 74-turn trial resends its early observations 73
times. ``observation_window`` bounds that.

The polling behaviour is the part worth locking. Collapsing an observation
rewrites the wire history at that position, so a boundary advancing every turn
rewrites the prefix every turn and the provider's cache never survives. Holding
it still for ``polling`` turns is what makes the rewrite occasional.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.loop import _with_collapsed_observations
from tolokaforge.core.models import Message, MessageRole

pytestmark = pytest.mark.unit


def _obs(text: str) -> Message:
    return Message(role=MessageRole.TOOL, content=text, tool_call_id="c")


def _assistant(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


def _history(n: int) -> list[Message]:
    wire: list[Message] = [Message(role=MessageRole.USER, content="task")]
    for i in range(n):
        wire.append(_assistant(f"turn {i}"))
        wire.append(_obs(f"output {i} " + "x" * 100))
    return wire


def test_only_observations_outside_the_window_collapse() -> None:
    wire = _history(5)

    out = _with_collapsed_observations(wire, keep_last=2, turn=0, polling=1)

    observations = [m for m in out if m.role is MessageRole.TOOL]
    assert len(observations) == 5
    # The three oldest lose their content, the two newest keep it verbatim.
    assert [o.content.startswith("[earlier output") for o in observations] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert observations[-1].content == wire[-1].content


def test_collapsed_observations_say_how_much_was_dropped() -> None:
    wire = _history(3)

    out = _with_collapsed_observations(wire, keep_last=1, turn=0, polling=1)

    collapsed = [m for m in out if m.role is MessageRole.TOOL][0]
    # The model is told a command ran and that its output is retrievable by
    # re-running it, rather than being shown a gap.
    assert collapsed.content == "[earlier output, 109 characters, not shown]"


def test_assistant_turns_are_untouched_and_identity_preserved() -> None:
    wire = _history(4)

    out = _with_collapsed_observations(wire, keep_last=1, turn=0, polling=1)

    for original, result in zip(wire, out):
        if original.role is not MessageRole.TOOL:
            assert result is original


def test_the_window_holds_still_between_polling_advances() -> None:
    wire = _history(10)

    def live(turn: int, polling: int) -> int:
        out = _with_collapsed_observations(wire, keep_last=3, turn=turn, polling=polling)
        return sum(
            1
            for m in out
            if m.role is MessageRole.TOOL and not m.content.startswith("[earlier output")
        )

    # polling=1 pins the live count exactly; polling=4 lets it breathe between
    # advances so three turns in four send a byte-identical prefix.
    assert [live(t, 1) for t in range(4)] == [3, 3, 3, 3]
    assert [live(t, 4) for t in range(4)] == [3, 4, 5, 6]


def test_a_history_inside_the_window_is_returned_unchanged() -> None:
    wire = _history(2)

    assert _with_collapsed_observations(wire, keep_last=5, turn=0, polling=1) is wire


def test_the_original_list_is_not_mutated() -> None:
    wire = _history(3)
    before = wire[2].content

    _with_collapsed_observations(wire, keep_last=1, turn=0, polling=1)

    assert wire[2].content == before
