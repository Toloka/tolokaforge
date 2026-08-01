"""Builder for coherent :class:`TrialTimeline` test fixtures.

Both grading substrates read a timeline, so a grading test needs a message view
and a record set that agree: a record naming a call no message asked for is a
reconciliation failure, not a fixture shortcut. :func:`build_timeline` assembles
the timeline from the turns and records a test actually cares about, declaring
every call on the message view so the two agree.

:func:`build_timeline` lands every call on the last assistant turn, which is what a
test asserting about one turn wants. :func:`build_turn_timeline` takes the calls
per turn instead, for the ordering and turn-window properties whose whole subject
is *which* turn declared a call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tolokaforge.core.grading.trace_timeline import TrialTimeline, build_trial_timeline
from tolokaforge.core.models import (
    Message,
    MessageRole,
    RecordedToolCall,
    TerminationReason,
    ToolCall,
)


def _declare_calls(
    messages: Sequence[Message],
    recorded: Sequence[RecordedToolCall],
    *,
    unexecuted: Sequence[ToolCall] = (),
) -> list[Message]:
    """The message view with every call declared on its last assistant turn."""
    declared = [
        ToolCall(id=record.call_id, name=record.tool_name, arguments=record.arguments)
        for record in recorded
    ] + list(unexecuted)
    view = list(messages)
    if not declared:
        return view

    hosts = [index for index, msg in enumerate(view) if msg.role is MessageRole.ASSISTANT]
    if not hosts:
        raise ValueError(
            "a tool call is declared by an assistant turn, so these messages cannot host "
            f"{[call.name for call in declared]} — add one or drop the messages"
        )
    view[hosts[-1]] = view[hosts[-1]].model_copy(update={"tool_calls": declared})
    return view


@dataclass(frozen=True)
class Turn:
    """One conversational turn and the tool calls that turn declared.

    ``unexecuted`` are calls the turn asked for that no record answers — the shape
    a re-graded bundle and an abandoned call both take, and the one that makes a
    status-reading matcher undecidable.
    """

    role: str
    content: str
    recorded: Sequence[RecordedToolCall] = ()
    unexecuted: Sequence[ToolCall] = ()


def build_turn_timeline(
    turns: Sequence[Turn], *, termination_reason: TerminationReason | None = None
) -> TrialTimeline:
    """A timeline whose calls sit on the turn that declared them.

    The opening user prompt shares ``turn_index`` 0 with the first assistant turn,
    which is the timeline's own rule and not this builder's.
    """
    messages = [
        Message(
            role=MessageRole(turn.role),
            content=turn.content,
            tool_calls=[
                ToolCall(id=record.call_id, name=record.tool_name, arguments=record.arguments)
                for record in turn.recorded
            ]
            + list(turn.unexecuted),
        )
        for turn in turns
    ]
    records = [record for turn in turns for record in turn.recorded]
    return build_trial_timeline(messages, records, termination_reason)


def build_timeline(
    turns: Sequence[tuple[str, str]] = (),
    recorded: Sequence[RecordedToolCall] = (),
    *,
    unexecuted: Sequence[ToolCall] = (),
    termination_reason: TerminationReason | None = None,
) -> TrialTimeline:
    """A timeline over ``(role, content)`` turns and the calls they made.

    With no turns at all the result is a records-only timeline — the hash-only
    grading input, where the record is the whole trial.
    """
    messages = [Message(role=MessageRole(role), content=content) for role, content in turns]
    if not messages:
        return build_trial_timeline([], recorded, termination_reason)
    return build_trial_timeline(
        _declare_calls(messages, recorded, unexecuted=unexecuted), recorded, termination_reason
    )
