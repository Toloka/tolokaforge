"""What a matcher selects from a trial's timeline, and when it cannot say.

Every timeline here comes from the real :func:`build_trial_timeline` over real
messages and recorded calls, so a matcher is read against what a graded trial
produces rather than against a hand-assembled event tuple.

Three rules carry the weight, and each has its own tests below: a predicate over a
``None`` field is unmatched rather than vacuously true; a ``tool_call`` matcher
reads ``status`` and ``result`` through the result paired to it, the call event
itself carrying neither; and evidence only the tool-call record could have supplied
makes an event **undecidable** — scoped to the matcher, so an unexecuted call the
matcher could never have selected decides nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.trace_checks import select_events
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import (
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    TraceMatcher,
    ValuePredicate,
)
from tolokaforge.runner.models import TRACE_PREDICATE_OPERATORS

pytestmark = pytest.mark.unit

# Both turns name the payment, so a matcher selecting on that text is held apart
# from the user's turn by ``kind`` alone.
_TURNS = (("user", "Refund PAY-664306."), ("assistant", "Looking up PAY-664306."))


def _timeline(
    recorded: Sequence[RecordedToolCall] = (),
    unexecuted: Sequence[ToolCall] = (),
) -> TrialTimeline:
    return build_timeline(turns=_TURNS, recorded=recorded, unexecuted=unexecuted)


def _only(timeline: TrialTimeline, kind: TraceEventKind) -> TraceEvent:
    events = [event for event in timeline.events if event.kind is kind]
    assert len(events) == 1, f"expected one {kind.value}, got {[event.kind for event in events]}"
    return events[0]


def _payment_lookup(**arguments: Any) -> RecordedToolCall:
    return recorded_call(
        "billing_api_get_payment",
        arguments=arguments or {"payment_id": "PAY-664306"},
        output='{"amount": 10}',
    )


def test_a_tool_result_matcher_passes_over_the_message_whose_status_is_none():
    timeline = _timeline(recorded=[_payment_lookup()])
    matcher = TraceMatcher(kind=TraceEventKind.TOOL_RESULT, status=ValuePredicate(equals="success"))

    outcome = select_events(timeline, matcher)

    assert _only(timeline, TraceEventKind.ASSISTANT_MESSAGE).status is None
    assert [event.kind for event in outcome.matched] == [TraceEventKind.TOOL_RESULT]
    assert outcome.undecidable == ()


def test_an_assistant_message_matcher_passes_over_the_call_whose_text_is_none():
    timeline = _timeline(recorded=[_payment_lookup()])
    matcher = TraceMatcher(
        kind=TraceEventKind.ASSISTANT_MESSAGE, text=ValuePredicate(contains="PAY-664306")
    )

    outcome = select_events(timeline, matcher)

    assert _only(timeline, TraceEventKind.TOOL_CALL).text is None
    assert _only(timeline, TraceEventKind.USER_MESSAGE).text == "Refund PAY-664306."
    assert [event.kind for event in outcome.matched] == [TraceEventKind.ASSISTANT_MESSAGE]
    assert outcome.undecidable == ()


def test_an_absent_argument_is_unmatched_rather_than_vacuously_true():
    """``not_equals`` over an argument the call never carried must not hold.

    The whole point of the rule: every operator but ``exists`` is false on a field
    the trial does not have, so an author's negative predicate cannot be satisfied
    by absence.
    """
    timeline = _timeline(recorded=[_payment_lookup()])
    call = _only(timeline, TraceEventKind.TOOL_CALL)
    assert call.arguments == {"payment_id": "PAY-664306"}

    negative = select_events(
        timeline,
        TraceMatcher(
            kind=TraceEventKind.TOOL_CALL,
            args={"refund_id": ValuePredicate(not_equals="R-1")},
        ),
    )
    absent = select_events(
        timeline,
        TraceMatcher(
            kind=TraceEventKind.TOOL_CALL,
            args={"refund_id": ValuePredicate(exists=False)},
        ),
    )

    assert negative.matched == ()
    assert negative.undecidable == ()
    assert absent.matched == (call,)


def test_a_nested_argument_path_reaches_inside_a_request_body():
    timeline = _timeline(
        recorded=[
            recorded_call(
                "servicenow_csm_search",
                arguments={"body": {"resolution_path": "duplicate_refund", "limit": 5}},
            )
        ]
    )
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"body.resolution_path": ValuePredicate(equals="duplicate_refund")},
    )

    other_path = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"body.resolution_path": ValuePredicate(equals="policy_exception")},
    )

    outcome = select_events(timeline, matcher)

    assert outcome.matched == (_only(timeline, TraceEventKind.TOOL_CALL),)
    assert select_events(timeline, other_path).matched == ()


@pytest.mark.parametrize(
    ("status", "selects_the_call"),
    [(ToolExecutionStatus.SUCCESS, True), (ToolExecutionStatus.ERROR, False)],
)
def test_a_tool_call_matcher_reads_its_status_from_the_paired_result(
    status: ToolExecutionStatus, selects_the_call: bool
):
    """The call event carries no status of its own — the pairing is what decides it."""
    timeline = build_timeline(
        turns=_TURNS,
        recorded=[
            recorded_call(
                "billing_api_get_payment",
                arguments={"payment_id": "PAY-664306"},
                status=status,
            )
        ],
    )
    call = _only(timeline, TraceEventKind.TOOL_CALL)
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"payment_id": ValuePredicate(equals="PAY-664306")},
        status=ValuePredicate(equals="success"),
    )

    outcome = select_events(timeline, matcher)

    assert call.status is None
    assert _only(timeline, TraceEventKind.TOOL_RESULT).status is status
    assert outcome.matched == ((call,) if selects_the_call else ())
    assert outcome.undecidable == ()
    assert outcome.indeterminate_reason is None


def test_a_status_predicate_cannot_be_decided_where_nothing_recorded_the_call():
    """A bundle re-graded without its tool-call record: the call is there, its outcome is not."""
    timeline = _timeline(
        unexecuted=[ToolCall(id="call_1", name="issue_refund", arguments={"amount": 10})]
    )
    call = _only(timeline, TraceEventKind.TOOL_CALL)
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        tool=ValuePredicate(equals="issue_refund"),
        status=ValuePredicate(equals="success"),
    )

    outcome = select_events(timeline, matcher)

    assert timeline.records_present is False
    assert call.status is None
    assert outcome.matched == ()
    assert outcome.undecidable == (call,)
    assert outcome.unreadable_fields == ("status",)
    assert "status" in str(outcome.indeterminate_reason)
    assert str(call.position) in str(outcome.indeterminate_reason)


def test_an_unexecuted_call_to_the_named_tool_cannot_be_decided():
    """The G2 decision: a declared call that never ran could have acted, so nobody can say."""
    timeline = _timeline(
        recorded=[recorded_call("issue_refund", arguments={"amount": 10})],
        unexecuted=[ToolCall(id="call_never_ran", name="issue_refund", arguments={"amount": 20})],
    )
    executed, unexecuted = [
        event for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL
    ]
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        tool=ValuePredicate(equals="issue_refund"),
        status=ValuePredicate(equals="success"),
    )

    outcome = select_events(timeline, matcher)

    assert timeline.records_present is True
    assert unexecuted.arguments == {"amount": 20}
    assert unexecuted.status is None
    assert outcome.matched == (executed,)
    assert outcome.undecidable == (unexecuted,)


def test_an_unexecuted_call_to_another_tool_leaves_the_matcher_decided():
    """Undecidability is scoped to the matcher: this call fails on ``tool`` at any status."""
    timeline = _timeline(
        recorded=[recorded_call("issue_refund", arguments={"amount": 10})],
        unexecuted=[ToolCall(id="call_never_ran", name="search_policy", arguments={})],
    )
    executed, unexecuted = [
        event for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL
    ]
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        tool=ValuePredicate(equals="issue_refund"),
        status=ValuePredicate(equals="success"),
    )

    outcome = select_events(timeline, matcher)

    assert unexecuted.tool_name == "search_policy"
    assert unexecuted.status is None
    assert outcome.matched == (executed,)
    assert outcome.undecidable == ()
    assert outcome.indeterminate_reason is None


@dataclass(frozen=True)
class _OperatorAnswer:
    """One operator, an argument value it holds for, and one it does not."""

    predicate: dict[str, Any]
    holds_for: dict[str, Any]
    fails_for: dict[str, Any]


# One row per declared operator, the arguments written as a real call carries them.
# ``exists`` reads presence rather than truth, so its passing row is an empty
# string — a truthiness reading would drop it.
_OPERATOR_ANSWERS: dict[str, _OperatorAnswer] = {
    "equals": _OperatorAnswer({"equals": "PAY-1"}, {"probe": "PAY-1"}, {"probe": "PAY-2"}),
    "equals_ci": _OperatorAnswer({"equals_ci": "pay-1"}, {"probe": "PAY-1"}, {"probe": "PAY-2"}),
    "contains": _OperatorAnswer({"contains": "W1"}, {"probe": ["W0", "W1"]}, {"probe": ["W0"]}),
    "contains_ci": _OperatorAnswer(
        {"contains_ci": "w1"}, {"probe": "item W1"}, {"probe": "item W2"}
    ),
    "not_equals": _OperatorAnswer({"not_equals": "PAY-1"}, {"probe": "PAY-2"}, {"probe": "PAY-1"}),
    "regex": _OperatorAnswer({"regex": "^PAY-[0-9]+$"}, {"probe": "PAY-1"}, {"probe": "REF-1"}),
    "gt": _OperatorAnswer({"gt": 10.0}, {"probe": 11}, {"probe": 10}),
    "gte": _OperatorAnswer({"gte": 10.0}, {"probe": 10}, {"probe": 9.5}),
    "lt": _OperatorAnswer({"lt": 10.0}, {"probe": 9.5}, {"probe": 10}),
    "lte": _OperatorAnswer({"lte": 10.0}, {"probe": 10}, {"probe": 10.5}),
    "in_": _OperatorAnswer({"in_": ["USD", "EUR"]}, {"probe": "EUR"}, {"probe": "JPY"}),
    "not_in": _OperatorAnswer({"not_in": ["USD", "EUR"]}, {"probe": "JPY"}, {"probe": "EUR"}),
    "len_gt": _OperatorAnswer({"len_gt": 2}, {"probe": "abc"}, {"probe": "ab"}),
    "len_gte": _OperatorAnswer({"len_gte": 2}, {"probe": "ab"}, {"probe": "a"}),
    "exists": _OperatorAnswer({"exists": True}, {"probe": ""}, {}),
}


def test_the_answer_table_spans_the_operators_a_predicate_declares():
    """Three sources: the table, the written-out vocabulary, and the model's own fields."""
    assert set(_OPERATOR_ANSWERS) == TRACE_PREDICATE_OPERATORS
    assert set(ValuePredicate.model_fields) == TRACE_PREDICATE_OPERATORS
    misrowed = {
        name: sorted(answer.predicate)
        for name, answer in _OPERATOR_ANSWERS.items()
        if set(answer.predicate) != {name}
    }
    assert misrowed == {}, f"a row must declare the operator it is keyed by, got {misrowed}"


@pytest.mark.parametrize("operator_name", sorted(_OPERATOR_ANSWERS))
def test_an_operator_selects_the_call_whose_argument_it_holds_for(operator_name: str):
    answer = _OPERATOR_ANSWERS[operator_name]
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"probe": ValuePredicate(**answer.predicate)},
    )

    holds = select_events(
        _timeline(recorded=[recorded_call("probe", arguments=answer.holds_for)]), matcher
    )
    fails = select_events(
        _timeline(recorded=[recorded_call("probe", arguments=answer.fails_for)]), matcher
    )

    assert len(holds.matched) == 1
    assert fails.matched == ()
    assert fails.undecidable == ()
