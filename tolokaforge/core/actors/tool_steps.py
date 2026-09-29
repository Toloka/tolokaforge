"""Where a user simulator's tool steps sit in a transcript.

Under ``actors.user.tool_turns: isolated`` a user reply that calls tools is recorded
as a *tool step*: a user message carrying the calls and, right after it, one tool
message answering each call. Nothing on a message says which kind it is; that shape
does. A ``shared`` user turn keeps its results inside its own text and is followed
by the agent's next message, so no tool message follows it — which tells the two
apart even in a bundle whose agent and user calls share a raw provider id.

The rule reads each message as plain facts — its role, the ids of the calls it
carries, the call id it answers — so every reader of a transcript applies the same
one: the engine on :class:`~tolokaforge.core.models.Message` objects, the judge and
the trace projection on the dicts a bundle stores. This module imports nothing from
the engine, so any of them can load it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["TurnShape", "user_tool_step_positions"]


@dataclass(frozen=True)
class TurnShape:
    """The facts about one transcript message the tool-step rule reads."""

    role: str
    """``"user"``, ``"assistant"``, ``"tool"`` or ``"system"``."""
    call_ids: tuple[str, ...] = ()
    """Ids of the tool calls the message carries."""
    answers: str | None = None
    """The call id a tool message answers."""


def user_tool_step_positions(shapes: Sequence[TurnShape]) -> frozenset[int]:
    """Positions of the messages that make up user tool steps.

    A step is a user message with calls whose every call id is answered by the
    run of tool messages right after it; its positions are that user message and
    the tool messages of the run that answer its calls.
    """
    positions: set[int] = set()
    for index, shape in enumerate(shapes):
        if shape.role != "user" or not shape.call_ids:
            continue
        answering = [
            position
            for position in _tool_run_after(shapes, index)
            if shapes[position].answers in shape.call_ids
        ]
        if set(shape.call_ids) <= {shapes[position].answers for position in answering}:
            positions.add(index)
            positions.update(answering)
    return frozenset(positions)


def _tool_run_after(shapes: Sequence[TurnShape], index: int) -> range:
    end = index + 1
    while end < len(shapes) and shapes[end].role == "tool":
        end += 1
    return range(index + 1, end)
