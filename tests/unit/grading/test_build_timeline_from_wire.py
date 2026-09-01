"""The four branches :func:`build_timeline_from_wire` collapses to.

The wrapper composes ``split_leading_system_message`` +
``decode_transcript_wire`` + ``build_trial_timeline`` — the shared recipe
both grading dispatchers reach the timeline through. The behaviour it locks
is the shape a records-empty call produces, the way records join with the
message view when both views are present, and the pass-through of
``termination_reason``.

The parity fixture ``_PARITY_FIXTURE_LLM_MESSAGES`` shadows the one
``tests/canonical/test_grader_timeline_from_wire_alone.py`` ships so drift
between the two suites is visible: the byte-parity 10-pack exercises the
same shape end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_timeline import (
    TraceEventKind,
    build_timeline_from_wire,
)
from tolokaforge.core.models import TerminationReason, ToolExecutionStatus

pytestmark = pytest.mark.unit


_PARITY_FIXTURE_LLM_MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "you are a test assistant"},
    {"role": "user", "content": "please help"},
    {"role": "assistant", "content": "done"},
]


def test_empty_wire_and_empty_records_reconciles_to_events_empty_timeline() -> None:
    """An empty message list with no records builds an events-empty timeline.

    The dispatcher-side shape when the wire carries nothing and the grader
    holds no records — the composite still needs a timeline to skip
    ``llm_judge`` and run ``trace_checks``. Reconciliation must not raise.
    """
    timeline = build_timeline_from_wire([], [], None)

    assert timeline.events == ()
    assert timeline.message_view_present is False
    assert timeline.records_present is False
    assert timeline.termination_reason is None


def test_empty_wire_with_records_emits_records_alone_and_passes_reason_through() -> None:
    """A records-only trial (hash-only shape) reflects records + termination.

    The runner reaches this branch on a hash-only trial: no messages on
    the wire, but ``trial_context.recorded`` still carries the executed
    tool calls. The wrapper must forward records and ``termination_reason``
    to :func:`build_trial_timeline` unchanged.
    """
    record = recorded_call(
        "read_file",
        sequence=0,
        status=ToolExecutionStatus.SUCCESS,
        output="hello",
    )

    timeline = build_timeline_from_wire([], [record], TerminationReason.MAX_TURNS)

    assert timeline.message_view_present is False
    assert timeline.records_present is True
    assert timeline.termination_reason is TerminationReason.MAX_TURNS
    kinds = [event.kind for event in timeline.events]
    assert kinds == [TraceEventKind.TOOL_CALL, TraceEventKind.TOOL_RESULT]


def test_system_user_assistant_wire_no_records_yields_two_events() -> None:
    """System message stripped; user + assistant survive as events.

    The parity-fixture shape: the leading ``role: system`` is stripped by
    the wrapper's internal ``split_leading_system_message`` and does not
    become an event. The two non-system turns join the timeline as
    ``user_message`` then ``assistant_message``.
    """
    timeline = build_timeline_from_wire(_PARITY_FIXTURE_LLM_MESSAGES, [], None)

    assert timeline.message_view_present is True
    assert timeline.records_present is False
    assert [event.kind for event in timeline.events] == [
        TraceEventKind.USER_MESSAGE,
        TraceEventKind.ASSISTANT_MESSAGE,
    ]


def test_wire_with_tool_call_and_matching_record_joins_by_episode_key() -> None:
    """A wire tool_call joins its matching record; both views mark present.

    Assistant declares a tool call; the record answers the same
    ``call_id``. The join produces ``TOOL_CALL`` + ``TOOL_RESULT`` events
    around the assistant turn, with ``termination_reason`` forwarded.
    """
    llm_messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a test assistant"},
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "toolu_read_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "hello", "tool_call_id": "toolu_read_1"},
    ]
    record = recorded_call(
        "read_file",
        sequence=0,
        status=ToolExecutionStatus.SUCCESS,
        output="hello",
        call_id="toolu_read_1",
    )

    timeline = build_timeline_from_wire(llm_messages, [record], TerminationReason.AGENT_DONE)

    assert timeline.message_view_present is True
    assert timeline.records_present is True
    assert timeline.termination_reason is TerminationReason.AGENT_DONE
    kinds = [event.kind for event in timeline.events]
    assert TraceEventKind.TOOL_CALL in kinds
    assert TraceEventKind.TOOL_RESULT in kinds
