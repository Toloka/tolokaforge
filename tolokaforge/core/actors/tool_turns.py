"""The user simulator's own tool steps, and who gets to see them.

Under ``actors.user.tool_turns: isolated`` a user reply that calls tools is not a
dialogue turn: it is recorded as a *tool step* — a USER message carrying the
calls, then one TOOL message answering each — and the simulator is asked again
with the results in view, until it replies with text alone. The agent reads only
that text. A call is addressed to the environment, so neither side of the
dialogue sees the other's tool traffic.

Nothing on :class:`~tolokaforge.core.models.Message` says which kind a message
is; the transcript's shape does. A step's calls are answered by TOOL messages
carrying their ids, while a ``shared`` user turn keeps its results inside its own
text and has no TOOL messages at all, and call ids are unique across both actors
for the whole episode. So the functions here read any transcript — a live one,
one decoded from the grading wire, a recorded bundle — without being told the
mode, and on a ``shared`` transcript each is the identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.core.models.task_config import (
    DEFAULT_MAX_USER_TOOL_STEPS,
    UserSimulatorConfig,
    UserToolTurns,
)

__all__ = [
    "UserToolTurnRule",
    "agent_view",
    "is_user_tool_step",
    "simulator_view",
    "user_tool_step_call_ids",
]


@dataclass(frozen=True)
class UserToolTurnRule:
    """How a trial runs its user simulator's tool calls: the resolved actor's
    ``tool_turns`` and ``max_tool_steps``."""

    mode: UserToolTurns = "shared"
    max_steps: int = DEFAULT_MAX_USER_TOOL_STEPS

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError(f"max_steps is {self.max_steps}; a user turn needs at least one.")

    @property
    def isolated(self) -> bool:
        return self.mode == "isolated"

    @classmethod
    def from_config(cls, config: UserSimulatorConfig) -> UserToolTurnRule:
        return cls(mode=config.tool_turns, max_steps=config.max_tool_steps)


def user_tool_step_call_ids(messages: Sequence[Message]) -> frozenset[str]:
    """Ids of the user's calls that a TOOL message answers — the calls of its tool steps."""
    user_call_ids = {
        call.id
        for message in messages
        if message.role is MessageRole.USER
        for call in message.tool_calls or ()
    }
    return frozenset(
        message.tool_call_id
        for message in messages
        if message.role is MessageRole.TOOL and message.tool_call_id in user_call_ids
    )


def is_user_tool_step(message: Message, step_call_ids: frozenset[str]) -> bool:
    """Whether *message* is a user tool step, given the transcript's step call ids."""
    return message.role is MessageRole.USER and any(
        call.id in step_call_ids for call in message.tool_calls or ()
    )


def agent_view(messages: Sequence[Message]) -> list[Message]:
    """The transcript as the agent reads it: without the user's tool steps or their results."""
    step_call_ids = user_tool_step_call_ids(messages)
    return [
        message
        for message in messages
        if not is_user_tool_step(message, step_call_ids)
        and not (message.role is MessageRole.TOOL and message.tool_call_id in step_call_ids)
    ]


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
    step_call_ids = user_tool_step_call_ids(messages)
    view: list[Message] = []
    for message in messages:
        if is_user_tool_step(message, step_call_ids):
            view.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=message.content,
                    tool_calls=message.tool_calls,
                    reasoning=message.reasoning,
                    ts=message.ts,
                )
            )
            continue
        if message.role is MessageRole.TOOL:
            if message.tool_call_id in step_call_ids:
                view.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=message.content,
                        tool_call_id=message.tool_call_id,
                        ts=message.ts,
                    )
                )
            continue
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            continue
        role = {
            MessageRole.USER: MessageRole.ASSISTANT,
            MessageRole.ASSISTANT: MessageRole.USER,
        }.get(message.role)
        if role is None or not message.content.strip():
            continue
        previous = view[-1] if view else None
        if previous is not None and previous.role is role and not previous.tool_calls:
            view[-1] = Message(
                role=role, content=f"{previous.content}\n\n{message.content}", ts=message.ts
            )
        else:
            view.append(Message(role=role, content=message.content, ts=message.ts))
    return view
