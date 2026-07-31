"""The guarantees :func:`build_trial_timeline` makes, one test each.

Every fixture here is shaped after a state a real trial produces: the two
identical parallel calls a provider distinguishes only by id, a terminating turn
whose calls never execute, a rejection the executor recorded, a bundle-sourced
trajectory with no records at all, and a hash-only trial with no messages at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    TraceEventKind,
    build_trial_timeline,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
)

pytestmark = pytest.mark.unit

_TOOL_EVENT_KINDS = (TraceEventKind.TOOL_CALL, TraceEventKind.TOOL_RESULT)

_OPTIONAL_FIELDS = frozenset(
    {
        "text",
        "call_id",
        "tool_name",
        "executor",
        "arguments",
        "status",
        "result",
        "latency_seconds",
    }
)


def _call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _assistant(text: str = "", *calls: ToolCall) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, tool_calls=list(calls) or None)


def _user(text: str, *calls: ToolCall) -> Message:
    return Message(role=MessageRole.USER, content=text, tool_calls=list(calls) or None)


def _tool_message(call_id: str, text: str) -> Message:
    return Message(role=MessageRole.TOOL, content=text, tool_call_id=call_id)


def _system(text: str) -> Message:
    return Message(role=MessageRole.SYSTEM, content=text)


def _kinds(timeline) -> list[TraceEventKind]:
    return [event.kind for event in timeline.events]


def _of_kind(timeline, kind: TraceEventKind) -> list:
    return [event for event in timeline.events if event.kind is kind]


def _refund_trial() -> tuple[list[Message], list]:
    """The two-identical-calls trial: same tool, same arguments, different ids.

    One refund succeeds and the second is refused as already refunded, so the
    only thing that can attach each outcome to the right call is the id.
    """
    arguments = {"order_id": "42", "token": "sk-live-x"}
    messages = [
        _user("refund order 42 twice"),
        _assistant(
            "", _call("call_A", "refund", **arguments), _call("call_B", "refund", **arguments)
        ),
        _tool_message("call_A", '{"ok": true}'),
        _tool_message("call_B", 'Error: {"error": "already refunded"}'),
    ]
    records = [
        recorded_call(
            "refund", sequence=0, call_id="call_A", arguments=arguments, output='{"ok": true}'
        ),
        recorded_call(
            "refund",
            sequence=1,
            call_id="call_B",
            arguments=arguments,
            status=ToolExecutionStatus.ERROR,
            output='{"error": "already refunded"}',
        ),
    ]
    return messages, records


def test_two_identical_calls_keep_their_own_results() -> None:
    messages, records = _refund_trial()

    timeline = build_trial_timeline(messages, records, TerminationReason.AGENT_DONE)

    results = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert {event.call_id: event.result for event in results} == {
        "call_A": '{"ok": true}',
        "call_B": '{"error": "already refunded"}',
    }
    assert {event.call_id: event.status for event in results} == {
        "call_A": ToolExecutionStatus.SUCCESS,
        "call_B": ToolExecutionStatus.ERROR,
    }


def test_arguments_reach_the_event_verbatim() -> None:
    messages, records = _refund_trial()

    timeline = build_trial_timeline(messages, records, None)

    assert [event.arguments for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)] == [
        {"order_id": "42", "token": "sk-live-x"},
        {"order_id": "42", "token": "sk-live-x"},
    ], "a token-named argument must reach the grader raw; a redacted value cannot be asserted"


def test_position_is_dense_and_equals_the_index() -> None:
    messages, records = _refund_trial()

    timeline = build_trial_timeline(messages, records, None)

    assert [event.position for event in timeline.events] == list(range(len(timeline.events)))
    assert len(timeline.events) == 6


def test_result_comes_from_the_record_not_the_message() -> None:
    """The two views word the same failure differently; the record is authoritative."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_A", "Error: Tool error: ValueError: already refunded"),
    ]
    records = [
        recorded_call(
            "refund",
            call_id="call_A",
            status=ToolExecutionStatus.ERROR,
            output="already refunded",
        )
    ]

    timeline = build_trial_timeline(messages, records, None)

    (result,) = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert result.result == "already refunded"


def test_turn_index_follows_assistant_generations() -> None:
    """Every event of one assistant generation shares its index, and the initial
    user prompt carries turn 0 while preceding the first assistant message."""
    messages = [
        _user("book me a flight"),
        _assistant("", _call("call_A", "search", origin="LHR")),
        _tool_message("call_A", "[]"),
        _user("try tomorrow"),
        _assistant("", _call("call_B", "search", origin="LHR")),
        _tool_message("call_B", "[]"),
        _assistant("nothing available"),
    ]
    records = [
        recorded_call("search", sequence=0, call_id="call_A"),
        recorded_call("search", sequence=1, call_id="call_B"),
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert [(event.kind, event.turn_index) for event in timeline.events] == [
        (TraceEventKind.USER_MESSAGE, 0),
        (TraceEventKind.ASSISTANT_MESSAGE, 0),
        (TraceEventKind.TOOL_CALL, 0),
        (TraceEventKind.TOOL_RESULT, 0),
        (TraceEventKind.USER_MESSAGE, 0),
        (TraceEventKind.ASSISTANT_MESSAGE, 1),
        (TraceEventKind.TOOL_CALL, 1),
        (TraceEventKind.TOOL_RESULT, 1),
        (TraceEventKind.ASSISTANT_MESSAGE, 2),
    ]


def test_a_terminating_turns_unexecuted_call_is_a_call_with_no_result() -> None:
    """``should_terminate`` runs before the calls execute, so a terminating turn's
    calls reach the message view and never run. Dropping them would make an
    ``absent`` or ``count`` constraint wrong in the agent's favour."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _assistant("###STOP###", _call("call_B", "refund", order_id="43")),
    ]
    records = [recorded_call("refund", sequence=0, call_id="call_A")]

    timeline = build_trial_timeline(messages, records, TerminationReason.AGENT_DONE)

    assert [event.call_id for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)] == [
        "call_A",
        "call_B",
    ]
    assert [event.call_id for event in _of_kind(timeline, TraceEventKind.TOOL_RESULT)] == ["call_A"]
    (unexecuted,) = [
        event for event in _of_kind(timeline, TraceEventKind.TOOL_CALL) if event.call_id == "call_B"
    ]
    assert unexecuted.status is None
    assert unexecuted.executor is None


@pytest.mark.parametrize(
    "status",
    [ToolExecutionStatus.TOOL_NOT_FOUND, ToolExecutionStatus.INVALID_ARGUMENTS],
)
def test_a_rejected_call_is_a_pair_carrying_the_rejection_status(
    status: ToolExecutionStatus,
) -> None:
    """A call the executor refused was attempted, so a ``status`` matcher must see
    it. Recorded as a normal pair, not as a message-only call."""
    messages = [
        _assistant("", _call("call_A", "nope")),
        _tool_message("call_A", "Error: Tool 'nope' not found"),
    ]
    records = [
        recorded_call("nope", call_id="call_A", status=status, output="Tool 'nope' not found")
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert _kinds(timeline) == [
        TraceEventKind.ASSISTANT_MESSAGE,
        TraceEventKind.TOOL_CALL,
        TraceEventKind.TOOL_RESULT,
    ]
    (result,) = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert result.status is status


def test_within_a_turn_calls_follow_execution_order_then_the_ones_that_never_ran() -> None:
    """Declaration order is ``A, B, C``; execution order is ``B, A``; ``C`` never ran."""
    messages = [
        _assistant(
            "",
            _call("call_A", "search", q="a"),
            _call("call_B", "search", q="b"),
            _call("call_C", "search", q="c"),
        )
    ]
    records = [
        recorded_call("search", sequence=0, call_id="call_B"),
        recorded_call("search", sequence=1, call_id="call_A"),
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert [(event.kind, event.call_id) for event in timeline.events] == [
        (TraceEventKind.ASSISTANT_MESSAGE, None),
        (TraceEventKind.TOOL_CALL, "call_B"),
        (TraceEventKind.TOOL_RESULT, "call_B"),
        (TraceEventKind.TOOL_CALL, "call_A"),
        (TraceEventKind.TOOL_RESULT, "call_A"),
        (TraceEventKind.TOOL_CALL, "call_C"),
    ]


def test_a_result_never_precedes_its_own_call() -> None:
    messages, records = _refund_trial()

    timeline = build_trial_timeline(messages, records, None)

    calls = {
        event.call_id: event.position for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)
    }
    results = {
        event.call_id: event.position for event in _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    }
    assert results and all(results[call_id] > calls[call_id] for call_id in results)


def test_a_duplicate_call_id_raises_naming_both_positions() -> None:
    messages = [
        _assistant(
            "", _call("call_A", "refund", order_id="42"), _call("call_A", "refund", order_id="43")
        )
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, [], None)

    assert "'call_A'" in str(raised.value)
    assert "positions 1 and 2" in str(raised.value)


def test_a_call_id_recorded_twice_raises_naming_both_sequences() -> None:
    """Indexing records by id would otherwise let the later record win silently."""
    messages = [_assistant("", _call("call_A", "refund", order_id="42"))]
    records = [
        recorded_call("refund", sequence=0, call_id="call_A", output="first"),
        recorded_call("refund", sequence=1, call_id="call_A", output="second"),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, records, None)

    assert "'call_A'" in str(raised.value)
    assert "sequences 0 and 1" in str(raised.value)


def test_a_record_matching_no_message_side_call_raises() -> None:
    messages = [_assistant("", _call("call_A", "refund", order_id="42"))]
    records = [
        recorded_call("refund", sequence=0, call_id="call_A"),
        recorded_call("wire_transfer", sequence=1, call_id="call_ghost"),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, records, None)

    message = str(raised.value)
    assert "'call_ghost'" in message
    assert "sequence 1" in message
    assert "'wire_transfer'" in message


def test_a_bundle_with_no_records_has_calls_but_no_results() -> None:
    """The normal state for a timeline rebuilt from a recorded bundle: ``tool_log``
    is not written to ``trajectory.yaml``, so every call is unpaired."""
    messages, _ = _refund_trial()

    timeline = build_trial_timeline(messages, [], None)

    assert timeline.records_present is False
    assert timeline.message_view_present is True
    assert _of_kind(timeline, TraceEventKind.TOOL_RESULT) == []
    calls = _of_kind(timeline, TraceEventKind.TOOL_CALL)
    assert [event.call_id for event in calls] == ["call_A", "call_B"]
    assert all(event.executor is None and event.status is None for event in calls)


def test_records_only_input_pairs_every_call_at_turn_zero() -> None:
    """Hash-only grading omits the transcript, so the records are the whole trial."""
    records = [
        recorded_call("search", sequence=1, call_id="call_B"),
        recorded_call("refund", sequence=0, call_id="call_A"),
    ]

    timeline = build_trial_timeline([], records, TerminationReason.MAX_TURNS)

    assert timeline.message_view_present is False
    assert timeline.records_present is True
    assert [(event.kind, event.call_id, event.turn_index) for event in timeline.events] == [
        (TraceEventKind.TOOL_CALL, "call_A", 0),
        (TraceEventKind.TOOL_RESULT, "call_A", 0),
        (TraceEventKind.TOOL_CALL, "call_B", 0),
        (TraceEventKind.TOOL_RESULT, "call_B", 0),
    ]


def test_system_messages_are_not_events() -> None:
    """The loop appends termination and max-turns notices as system messages;
    grading on harness strings is not grading the agent."""
    messages = [
        _system("You are a helpful assistant."),
        _user("refund order 42"),
        _assistant("done"),
        _system("Maximum turns (3) reached. Dialogue terminated."),
    ]

    timeline = build_trial_timeline(messages, [], None)

    assert _kinds(timeline) == [TraceEventKind.USER_MESSAGE, TraceEventKind.ASSISTANT_MESSAGE]
    assert [event.text for event in timeline.events] == ["refund order 42", "done"]


def test_a_view_of_only_harness_text_is_not_a_message_view() -> None:
    """The wire prepends the agent policy as a leading system message, so a
    hash-only trial can arrive with messages that carry no turn. Treating that as
    a message view would make every record unlinkable and fail a legitimate run."""
    records = [recorded_call("refund", sequence=0, call_id="call_A")]

    timeline = build_trial_timeline([_system("You are a helpful assistant.")], records, None)

    assert timeline.message_view_present is False
    assert [(event.kind, event.call_id) for event in timeline.events] == [
        (TraceEventKind.TOOL_CALL, "call_A"),
        (TraceEventKind.TOOL_RESULT, "call_A"),
    ]


def test_a_user_executed_call_pairs_with_the_record_not_a_tool_message() -> None:
    """A user-simulator call reaches the message view but emits no ``role: tool``
    message, so its result can only come from the record."""
    messages = [_user("checking my end", _call("call_U", "user_lookup", ref="42"))]
    records = [
        recorded_call(
            "user_lookup",
            sequence=0,
            call_id="call_U",
            executor=ToolExecutorIdentity.USER,
            output="ok",
        )
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert _kinds(timeline) == [
        TraceEventKind.USER_MESSAGE,
        TraceEventKind.TOOL_CALL,
        TraceEventKind.TOOL_RESULT,
    ]
    assert all(
        event.executor is ToolExecutorIdentity.USER
        for event in timeline.events
        if event.kind in _TOOL_EVENT_KINDS
    )


def test_an_empty_trial_has_no_events_and_neither_view() -> None:
    timeline = build_trial_timeline([], [], TerminationReason.PROVISION_ERROR)

    assert timeline.events == ()
    assert timeline.message_view_present is False
    assert timeline.records_present is False
    assert timeline.termination_reason is TerminationReason.PROVISION_ERROR


def test_fields_are_none_exactly_when_they_do_not_apply_to_the_kind() -> None:
    """A predicate over an inapplicable field must be unmatched rather than
    vacuously true, which holds only if the field is ``None``."""
    messages = [
        _user("refund order 42"),
        _assistant("", _call("call_A", "refund", order_id="42"), _call("call_B", "refund")),
        _tool_message("call_A", '{"ok": true}'),
    ]
    records = [recorded_call("refund", sequence=0, call_id="call_A", latency_seconds=0.25)]

    timeline = build_trial_timeline(messages, records, None)

    populated = {
        kind: {
            field
            for field in _OPTIONAL_FIELDS
            for event in _of_kind(timeline, kind)
            if getattr(event, field) is not None
        }
        for kind in TraceEventKind
    }
    assert populated == {
        TraceEventKind.ASSISTANT_MESSAGE: {"text"},
        TraceEventKind.USER_MESSAGE: {"text"},
        # ``call_B`` never executed, so it carries no executor — the only field a
        # TOOL_CALL can be missing.
        TraceEventKind.TOOL_CALL: {"call_id", "tool_name", "arguments", "executor"},
        TraceEventKind.TOOL_RESULT: {
            "call_id",
            "tool_name",
            "executor",
            "status",
            "result",
            "latency_seconds",
        },
    }
    assert _of_kind(timeline, TraceEventKind.ASSISTANT_MESSAGE)[0].text == ""


def test_no_event_is_hashable_whatever_its_kind() -> None:
    """``arguments`` is a dict on every ``TOOL_CALL``, so a generated hash would
    raise for that kind and succeed for the others: ``set()`` over results would
    work while the same code over calls raised. Failing uniformly at the first use
    is the only shape a check author can rely on."""
    messages, records = _refund_trial()
    events = build_trial_timeline(messages, records, None).events

    # "Uniformly" is the claim, so every kind has to be in the sample.
    assert {event.kind for event in events} == set(TraceEventKind)
    for event in events:
        with pytest.raises(TypeError, match="unhashable type: 'TraceEvent'"):
            hash(event)

    # Equality is untouched — only hashing is withdrawn.
    assert events[0] == events[0]
