"""Builders for :class:`RecordedToolCall` test fixtures.

``RecordedToolCall`` has nine required fields, most of which are noise for a
consumer test that only cares about the tool name and whether the call
succeeded. :func:`recorded_call` names the fields a test is actually asserting
on and fills the rest with inert values.

:func:`records_from_messages` builds a whole trial's record from a message view
that already declares every call, for a fixture whose subject is the *rule*
being scored rather than the record's own shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.models import (
    Message,
    MessageRole,
    RecordedToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def recorded_call(
    tool_name: str,
    *,
    sequence: int = 0,
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
    arguments: dict[str, Any] | None = None,
    executor: ToolExecutorIdentity = ToolExecutorIdentity.AGENT,
    output: str = "",
    latency_seconds: float = 0.0,
    call_id: str | None = None,
) -> RecordedToolCall:
    """One recorded tool call. ``call_id`` defaults to a value unique per sequence."""
    return RecordedToolCall(
        call_id=call_id if call_id is not None else f"toolu_{tool_name}_{sequence}",
        sequence=sequence,
        tool_name=tool_name,
        arguments=arguments or {},
        executor=executor,
        status=status,
        output=output,
        latency_seconds=latency_seconds,
        timestamp=_EPOCH,
    )


def records_from_messages(messages: Sequence[Message]) -> list[RecordedToolCall]:
    """One successful agent record per tool call ``messages`` declares, in message order.

    ``output`` is the answering ``role: tool`` message's content.
    :func:`~tolokaforge.core.grading.trace_timeline.build_trial_timeline` takes a
    ``TOOL_RESULT``'s text from the record whenever one exists and from the message
    only when none does, so a record built without it *replaces* the tool's text
    with ``""``.

    Raises:
        ValueError: a declared call has no answering ``role: tool`` message, so a
            successful record for it would claim an outcome the message view does
            not show.
    """
    answers = {
        message.tool_call_id: message.content
        for message in messages
        if message.role is MessageRole.TOOL and message.tool_call_id
    }
    records: list[RecordedToolCall] = []
    for call in (call for message in messages for call in message.tool_calls or []):
        if call.id not in answers:
            raise ValueError(
                f"tool call {call.id!r} ({call.name!r}) has no answering role:tool message, "
                "so this message view cannot say what a successful call returned"
            )
        records.append(
            recorded_call(
                call.name,
                sequence=len(records),
                call_id=call.id,
                arguments=call.arguments,
                output=answers[call.id],
            )
        )
    return records
