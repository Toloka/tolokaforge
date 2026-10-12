"""What each side of a dialogue reads of a transcript that holds user tool steps.

Under ``actors.user.tool_turns: isolated`` a user reply that calls tools is not a
dialogue turn: it is recorded as a *tool step* — a USER message carrying the
calls, then one TOOL message answering each — and the simulator is asked again
with the results in view, until it replies with text alone. The agent reads only
that text. A call is addressed to the environment, so neither side of the
dialogue sees the other's tool traffic.

Which messages are steps is read from the transcript's shape
(:mod:`tolokaforge.core.actors.tool_steps`), so these functions work on any
transcript — a live one, one decoded from the grading wire, a recorded bundle —
without being told the mode. A ``shared`` transcript has no steps, so
:func:`agent_view` returns it unchanged; :func:`simulator_view` is the isolated
simulator's view and :func:`shared_view` the shared one's.
"""

from __future__ import annotations

from collections.abc import Sequence

from tolokaforge.core.actors.tool_steps import TurnShape, user_tool_step_positions
from tolokaforge.core.models import Message, MessageRole

__all__ = [
    "agent_view",
    "is_user_tool_step",
    "shared_view",
    "simulator_view",
    "user_tool_step_call_ids",
]

_FLIPPED = {MessageRole.USER: MessageRole.ASSISTANT, MessageRole.ASSISTANT: MessageRole.USER}


def user_tool_step_call_ids(messages: Sequence[Message]) -> frozenset[str]:
    """Ids of the calls the user's tool steps carry."""
    return frozenset(
        call.id
        for index in _step_positions(messages)
        if messages[index].role is MessageRole.USER
        for call in messages[index].tool_calls or ()
    )


def is_user_tool_step(message: Message, step_call_ids: frozenset[str]) -> bool:
    """Whether *message* is a user tool step, given the transcript's step call ids."""
    return message.role is MessageRole.USER and any(
        call.id in step_call_ids for call in message.tool_calls or ()
    )


def agent_view(messages: Sequence[Message]) -> list[Message]:
    """The transcript as the agent reads it: without the user's tool steps or their results."""
    positions = _step_positions(messages)
    return [message for index, message in enumerate(messages) if index not in positions]


def simulator_view(messages: Sequence[Message]) -> list[Message]:
    """The transcript from the customer's seat, with the simulator's own tool steps in it.

    The simulator's messages replay as ``assistant`` turns and the agent's as
    ``user`` turns. Its tool steps keep their calls, text and reasoning — the
    provider needs the calls to pair each result with, and a reasoning model its
    own signed reasoning back — and their results replay as ``tool`` messages.
    The agent's tool traffic is dropped whole, text included: a message carrying
    calls is addressed to the environment, not to the customer. Adjacent text
    turns of one party are joined so the request alternates; a tool step is
    never joined to anything.
    """
    positions = _step_positions(messages)
    view: list[Message] = []
    for index, message in enumerate(messages):
        replayed = _as_the_simulator_reads_it(message, in_step=index in positions)
        if replayed is not None:
            _append_joining_text(view, replayed)
    return view


def shared_view(messages: Sequence[Message]) -> list[Message]:
    """The transcript from the customer's seat under ``shared`` tool turns.

    The simulator's past messages replay as ``assistant`` turns and the agent's as
    ``user`` turns. Turns with no dialogue text (agent tool-call turns,
    whitespace-only replies) are skipped — replaying them as empty turns adds
    noise the simulator's provider may reject. The skip is text-only: a turn
    carrying ``content_blocks`` with no text would be dropped too, a latent gap
    no USER/ASSISTANT call site produces today. Adjacent same-role turns are
    coalesced so the request alternates strictly — a skipped turn can leave two
    dialogue turns of the same party back to back, which strict-alternation
    providers reject. An agent turn that carries text beside its calls keeps its
    text here, unlike :func:`simulator_view`.
    """
    view: list[Message] = []
    for message in messages:
        if not message.content.strip():
            continue
        role = _FLIPPED.get(message.role)
        if role is None:
            continue
        if view and view[-1].role == role:
            previous = view[-1]
            view[-1] = Message(
                role=role, content=f"{previous.content}\n\n{message.content}", ts=message.ts
            )
        else:
            view.append(Message(role=role, content=message.content, ts=message.ts))
    return view


def _step_positions(messages: Sequence[Message]) -> frozenset[int]:
    return user_tool_step_positions(
        [
            TurnShape(
                role=message.role.value,
                call_ids=tuple(call.id for call in message.tool_calls or ()),
                answers=message.tool_call_id,
            )
            for message in messages
        ]
    )


def _as_the_simulator_reads_it(message: Message, *, in_step: bool) -> Message | None:
    """*message* as the isolated simulator reads it, or ``None`` when it does not."""
    if in_step and message.role is MessageRole.USER:
        return Message(
            role=MessageRole.ASSISTANT,
            content=message.content,
            tool_calls=message.tool_calls,
            reasoning=message.reasoning,
            ts=message.ts,
        )
    if in_step:
        return Message(
            role=MessageRole.TOOL,
            content=message.content,
            tool_call_id=message.tool_call_id,
            ts=message.ts,
        )
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        return None
    role = _FLIPPED.get(message.role)
    if role is None or not message.content.strip():
        return None
    return Message(role=role, content=message.content, ts=message.ts)


def _append_joining_text(view: list[Message], message: Message) -> None:
    """Append *message*, joined onto the previous turn when both are one party's text."""
    previous = view[-1] if view else None
    joins = (
        previous is not None
        and message.role is not MessageRole.TOOL
        and not message.tool_calls
        and previous.role is message.role
        and not previous.tool_calls
    )
    if not joins:
        view.append(message)
        return
    view[-1] = Message(
        role=message.role, content=f"{previous.content}\n\n{message.content}", ts=message.ts
    )
