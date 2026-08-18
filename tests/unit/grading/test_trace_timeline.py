"""The guarantees :func:`build_trial_timeline` makes, one test each.

Every fixture here is shaped after a state a real trial produces: the two
identical parallel calls a provider distinguishes only by id, a trial whose
provider reused one id across two turns, a terminating turn whose calls never
execute, a rejection the executor recorded, a bundle-sourced trajectory with no
records at all, and a hash-only trial with no messages at all.
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


def _answered_calls(timeline) -> list[tuple]:
    """Each ``TOOL_CALL`` beside the ``TOOL_RESULT`` emitted for it — by adjacency.

    A result is appended immediately after the call it answers on every emission
    path, so adjacency identifies the pair without reading ``call_id``. That is
    what makes "the result carries its call's id" a claim a test can falsify
    rather than a restatement of how the pair was found.
    """
    events = timeline.events
    return [
        (call, events[call.position + 1])
        for call in _of_kind(timeline, TraceEventKind.TOOL_CALL)
        if call.position + 1 < len(events)
        and events[call.position + 1].kind is TraceEventKind.TOOL_RESULT
    ]


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


# The trial the issue reported, turn by turn: ``moonshotai/kimi-k3`` names each
# call ``<tool>:<index>``, and the index it emitted for the second turn-4 call
# repeats the one from turn 1. Twelve calls, seven assistant turns, one collision.
_REUSED_ID_TURNS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("search_directory:0", "search_directory"), ("get_employee:1", "get_employee")),
    (("list_cases:2", "list_cases"), ("get_case:3", "get_case")),
    (("get_case_notes:4", "get_case_notes"),),
    (("get_employee:1", "get_employee"), ("get_manager:6", "get_manager")),
    (("list_policies:7", "list_policies"), ("get_policy:8", "get_policy")),
    (("add_case_note:9", "add_case_note"),),
    (("set_case_status:10", "set_case_status"), ("update_case:11", "update_case")),
)

_REUSED_RAW_IDS = tuple(call_id for turn in _REUSED_ID_TURNS for call_id, _ in turn)


def _reused_id_trial() -> tuple[list[Message], list]:
    """The reported trajectory: every call executed, in declaration order.

    Each call carries the trial-wide index of its own execution in ``arguments``
    and its record returns ``out-<that index>``, so a mis-pairing shows up in the
    result text rather than only in an id.
    """
    messages: list[Message] = []
    records = []
    sequence = 0
    for turn in _REUSED_ID_TURNS:
        declared = [
            _call(call_id, name, n=sequence + offset) for offset, (call_id, name) in enumerate(turn)
        ]
        messages.append(_assistant("", *declared))
        for call_id, name in turn:
            messages.append(_tool_message(call_id, f"out-{sequence}"))
            records.append(
                recorded_call(name, sequence=sequence, call_id=call_id, output=f"out-{sequence}")
            )
            sequence += 1
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
        _tool_message("call_A", "Error: already refunded"),
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


def test_a_message_only_failure_carries_the_same_text_a_record_would() -> None:
    """A bundle re-graded without ``tool_log.yaml`` reconstructs the failure text
    identically: the ``Error: `` prefix ``core/loop.py`` writes onto the
    message body is stripped so ``result:`` matchers see one text on both
    substrates (#977).

    Without the strip, ``result: {regex: "^already refunded"}`` would pass on
    a bundle carrying the log and fail on the same bundle re-graded from
    messages alone.
    """
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_A", "Error: already refunded"),
    ]

    timeline = build_trial_timeline(messages, [], None)

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
        _assistant("Refunding the second order too.", _call("call_B", "refund", order_id="43")),
    ]
    records = [recorded_call("refund", sequence=0, call_id="call_A")]

    timeline = build_trial_timeline(messages, records, TerminationReason.STUCK_DETECTED)

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


def test_a_reused_provider_id_declared_twice_becomes_two_calls_with_their_own_keys() -> None:
    """The provider reused one id across two declarations, so the trial's key for the
    second occurrence is derived rather than borrowed. Each call keeps its own
    arguments, which is what pairing by occurrence is for."""
    messages = [
        _assistant(
            "", _call("call_A", "refund", order_id="42"), _call("call_A", "refund", order_id="43")
        )
    ]

    timeline = build_trial_timeline(messages, [], None)

    assert [
        (event.call_id, event.arguments) for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)
    ] == [("call_A", {"order_id": "42"}), ("call_A#2", {"order_id": "43"})]


def test_a_record_holding_more_occurrences_than_the_view_declares_raises() -> None:
    """One declaration, two records: the second record's key names a call the message
    view never asked for, so the two views disagree about what the trial did. Letting
    the later record win silently is the ambiguity the key exists to remove."""
    messages = [_assistant("", _call("call_A", "refund", order_id="42"))]
    records = [
        recorded_call("refund", sequence=0, call_id="call_A", output="first"),
        recorded_call("refund", sequence=1, call_id="call_A", output="second"),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, records, None)

    message = str(raised.value)
    assert "'call_A#2'" in message
    assert "sequence 1" in message
    assert "matches no tool call in the message view" in message


def test_the_reported_trajectory_joins_every_call_to_its_own_result() -> None:
    """The reported defect, end to end: a provider reused one id across two turns and
    the trial could not be graded at all. Twelve calls, twelve results, and exactly
    one key that is not the id the provider minted."""
    messages, records = _reused_id_trial()

    timeline = build_trial_timeline(messages, records, TerminationReason.AGENT_DONE)

    pairs = _answered_calls(timeline)
    assert len(pairs) == 12
    assert all(call.call_id == result.call_id for call, result in pairs)
    assert [result.result for _, result in pairs] == [
        f"out-{call.arguments['n']}" for call, _ in pairs
    ]
    derived = [event.call_id for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)]
    assert [key for key, raw in zip(derived, _REUSED_RAW_IDS, strict=True) if key != raw] == [
        "get_employee:1#2"
    ]


def test_the_reported_trajectory_joins_from_its_message_view_alone() -> None:
    """The same trial as a bundle written before ``tool_log.yaml`` existed, or a
    ``retrace`` of one: the twelve ``role: tool`` texts pair with the same twelve
    calls, each result carrying the key of the call it answers rather than the raw
    id the provider reused."""
    messages, _ = _reused_id_trial()

    timeline = build_trial_timeline(messages, [], None)

    assert timeline.records_present is False
    pairs = _answered_calls(timeline)
    assert len(pairs) == 12
    assert all(call.call_id == result.call_id for call, result in pairs)
    assert [result.result for _, result in pairs] == [
        f"out-{call.arguments['n']}" for call, _ in pairs
    ]


def test_every_call_of_a_duplicating_trial_still_has_a_key_of_its_own() -> None:
    """N7 promises ``call_id`` is unique per call, and every check keyed on it —
    ``attempted_calls``, the trace-check result index — depends on that holding on a
    trial the provider duplicated."""
    messages, records = _reused_id_trial()

    timeline = build_trial_timeline(messages, records, None)

    calls = _of_kind(timeline, TraceEventKind.TOOL_CALL)
    assert len(calls) == 12
    assert len({event.call_id for event in calls}) == 12


def test_a_record_naming_a_different_tool_than_its_declaration_raises() -> None:
    """The order-based join is sound under the suffix invariant, and the tool name is
    the independent corroboration. Where the two views disagree about what ran, the
    pairing they produced is not trustworthy and saying so is the whole point."""
    messages = [_assistant("", _call("call_A", "refund", order_id="42"))]
    records = [recorded_call("wire_transfer", sequence=0, call_id="call_A")]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, records, None)

    message = str(raised.value)
    assert "'call_A'" in message
    assert "'wire_transfer'" in message
    assert "'refund'" in message


def test_the_recordless_declaration_of_a_reused_id_is_the_last_one() -> None:
    """A turn's calls stop executing at the first failure and termination is decided
    before any of them run, so unexecuted calls are a suffix. The two records answer
    the first two declarations, and it is the third that carries no status."""
    messages = [
        _assistant(
            "",
            _call("call_A", "refund", order_id="42"),
            _call("call_A", "refund", order_id="43"),
            _call("call_A", "refund", order_id="44"),
        )
    ]
    records = [
        recorded_call("refund", sequence=0, call_id="call_A", output="first"),
        recorded_call("refund", sequence=1, call_id="call_A", output="second"),
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert [
        (event.call_id, event.arguments["order_id"], event.status)
        for event in _of_kind(timeline, TraceEventKind.TOOL_CALL)
    ] == [("call_A", "42", None), ("call_A#2", "43", None), ("call_A#3", "44", None)]
    assert [
        (event.call_id, event.result) for event in _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    ] == [("call_A", "first"), ("call_A#2", "second")]


def test_a_trial_whose_provider_ids_are_unique_carries_exactly_those_ids() -> None:
    """The no-movement guarantee at the join: every Anthropic / OpenAI trial, and
    every fixture and bundle already on disk, keys on the provider's own id."""
    messages = [
        _user("refund both orders"),
        _assistant("", _call("toolu_01", "refund", order_id="42")),
        _tool_message("toolu_01", '{"ok": true}'),
        _assistant("", _call("toolu_02", "refund", order_id="43"), _call("toolu_03", "notify")),
        _tool_message("toolu_02", '{"ok": true}'),
        _tool_message("toolu_03", '{"ok": true}'),
    ]
    records = [
        recorded_call("refund", sequence=0, call_id="toolu_01"),
        recorded_call("refund", sequence=1, call_id="toolu_02"),
        recorded_call("notify", sequence=2, call_id="toolu_03"),
    ]

    timeline = build_trial_timeline(messages, records, None)

    assert [event.call_id for event in timeline.events if event.kind in _TOOL_EVENT_KINDS] == [
        "toolu_01",
        "toolu_01",
        "toolu_02",
        "toolu_02",
        "toolu_03",
        "toolu_03",
    ]


def test_a_records_only_trial_reusing_an_id_keys_each_result_to_its_own_call() -> None:
    """Hash-only grading supplies no message view, so nothing reconciles the record
    against a declaration and neither G7 nor G6b applies. The keys have to come out
    distinct and the results have to carry them, or two calls share an id in silence."""
    records = [
        recorded_call("get_employee", sequence=0, call_id="get_employee:1", output="first"),
        recorded_call("list_cases", sequence=1, call_id="list_cases:2", output="second"),
        recorded_call("get_employee", sequence=2, call_id="get_employee:1", output="third"),
    ]

    timeline = build_trial_timeline([], records, TerminationReason.MAX_TURNS)

    assert timeline.message_view_present is False
    pairs = _answered_calls(timeline)
    assert [(call.call_id, result.call_id, result.result) for call, result in pairs] == [
        ("get_employee:1", "get_employee:1", "first"),
        ("list_cases:2", "list_cases:2", "second"),
        ("get_employee:1#2", "get_employee:1#2", "third"),
    ]


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


def test_a_bundle_with_no_records_takes_each_result_from_its_tool_message() -> None:
    """The normal state for a timeline rebuilt from a recorded bundle: ``tool_log``
    is not written to ``trajectory.yaml``, but the ``role: tool`` messages are, so
    the tool output is on disk and only the record's own fields are missing.

    ``call_B``'s text is the tool's own failure text — the ``core/loop.py``
    ``Error: `` prefix is stripped on this branch so ``result:`` matchers see
    the same text as a bundle re-graded with a ``tool_log.yaml`` (#977).
    """
    messages, _ = _refund_trial()

    timeline = build_trial_timeline(messages, [], None)

    assert timeline.records_present is False
    assert timeline.message_view_present is True
    results = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert {event.call_id: event.result for event in results} == {
        "call_A": '{"ok": true}',
        "call_B": '{"error": "already refunded"}',
    }
    populated = {
        field
        for field in _OPTIONAL_FIELDS
        for event in results
        if getattr(event, field) is not None
    }
    assert populated == {"call_id", "tool_name", "result"}
    assert all(event.executor is None for event in _of_kind(timeline, TraceEventKind.TOOL_CALL))


def test_a_bundle_pairs_its_tool_messages_by_id_and_not_by_position() -> None:
    """Two calls to one tool with byte-identical arguments differ only in the id,
    and nothing makes a bundle's results arrive in the order the calls were
    declared. Pairing by position would swap these two outcomes."""
    arguments = {"order_id": "42"}
    messages = [
        _assistant(
            "", _call("call_A", "refund", **arguments), _call("call_B", "refund", **arguments)
        ),
        _tool_message("call_B", '{"error": "already refunded"}'),
        _tool_message("call_A", '{"ok": true}'),
    ]

    timeline = build_trial_timeline(messages, [], None)

    results = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert {event.call_id: event.result for event in results} == {
        "call_A": '{"ok": true}',
        "call_B": '{"error": "already refunded"}',
    }


def test_a_bundle_call_with_no_tool_message_stays_unpaired() -> None:
    """A terminating turn's call never ran, so the bundle holds no result for it.
    G4 still emits the call: dropping it makes an ``absent`` or ``count``
    constraint wrong in the agent's favour."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_A", '{"ok": true}'),
        _assistant("Refunding the second order too.", _call("call_B", "refund", order_id="43")),
    ]

    timeline = build_trial_timeline(messages, [], None)

    assert [(event.kind, event.call_id) for event in timeline.events] == [
        (TraceEventKind.ASSISTANT_MESSAGE, None),
        (TraceEventKind.TOOL_CALL, "call_A"),
        (TraceEventKind.TOOL_RESULT, "call_A"),
        (TraceEventKind.ASSISTANT_MESSAGE, None),
        (TraceEventKind.TOOL_CALL, "call_B"),
    ]


def test_a_tool_message_answering_no_declared_call_raises() -> None:
    """Symmetric with G7: the result's text is the only evidence of what the tool
    returned, so a result that names no call can be neither joined nor dropped."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_ghost", '{"ok": true}'),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, [], None)

    assert "'call_ghost'" in str(raised.value)
    assert "index 1" in str(raised.value)


def test_a_tool_message_carrying_no_call_id_raises() -> None:
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        Message(role=MessageRole.TOOL, content='{"ok": true}'),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, [], None)

    assert "answers tool-call id None" in str(raised.value)


def test_two_tool_messages_answering_one_call_raise() -> None:
    """The view declares one occurrence of the id and answers two, so the second
    result's key names a call no turn asked for. Its text is the only surviving
    evidence of what that call returned, so it can be neither joined nor dropped."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_A", '{"ok": true}'),
        _tool_message("call_A", '{"error": "already refunded"}'),
    ]

    with pytest.raises(TimelineInconsistencyError) as raised:
        build_trial_timeline(messages, [], None)

    assert "'call_A#2'" in str(raised.value)
    assert "index 2" in str(raised.value)


def test_a_records_present_timeline_neither_joins_nor_validates_its_tool_messages() -> None:
    """G5 is precedence, and precedence only bites where both views exist: with a
    record present the message view's copies are not read at all, so the join's
    loudness does not extend to them either. Making it would fail a live grading
    run over evidence nothing reads."""
    messages = [
        _assistant("", _call("call_A", "refund", order_id="42")),
        _tool_message("call_A", "Error: already refunded"),
        _tool_message("call_ghost", "answers a call this trial never made"),
    ]
    records = [recorded_call("refund", call_id="call_A", output="already refunded")]

    timeline = build_trial_timeline(messages, records, None)

    (result,) = _of_kind(timeline, TraceEventKind.TOOL_RESULT)
    assert result.result == "already refunded"


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
