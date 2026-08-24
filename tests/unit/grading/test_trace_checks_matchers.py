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
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.trace_checks import _BINDING_OPERATORS, select_events
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
from tolokaforge.runner.models import (
    TRACE_PREDICATE_BINDING_OPERATORS,
    TRACE_PREDICATE_OPERATORS,
)

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

    outcome = select_events(timeline, matcher, {})

    assert _only(timeline, TraceEventKind.ASSISTANT_MESSAGE).status is None
    assert [event.kind for event in outcome.matched] == [TraceEventKind.TOOL_RESULT]
    assert outcome.undecidable == ()


def test_an_assistant_message_matcher_passes_over_the_call_whose_text_is_none():
    timeline = _timeline(recorded=[_payment_lookup()])
    matcher = TraceMatcher(
        kind=TraceEventKind.ASSISTANT_MESSAGE, text=ValuePredicate(contains="PAY-664306")
    )

    outcome = select_events(timeline, matcher, {})

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
        {},
    )
    absent = select_events(
        timeline,
        TraceMatcher(
            kind=TraceEventKind.TOOL_CALL,
            args={"refund_id": ValuePredicate(exists=False)},
        ),
        {},
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

    outcome = select_events(timeline, matcher, {})

    assert outcome.matched == (_only(timeline, TraceEventKind.TOOL_CALL),)
    assert select_events(timeline, other_path, {}).matched == ()


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

    outcome = select_events(timeline, matcher, {})

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

    outcome = select_events(timeline, matcher, {})

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

    outcome = select_events(timeline, matcher, {})

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

    outcome = select_events(timeline, matcher, {})

    assert unexecuted.tool_name == "search_policy"
    assert unexecuted.status is None
    assert outcome.matched == (executed,)
    assert outcome.undecidable == ()
    assert outcome.indeterminate_reason is None


@dataclass(frozen=True)
class _OperatorAnswer:
    """One operator, an argument value it holds for, and one it does not.

    ``bindings`` is the environment the row resolves under, empty for every
    operator whose expected value is written out rather than named.
    """

    predicate: dict[str, Any]
    holds_for: dict[str, Any]
    fails_for: dict[str, Any]
    bindings: dict[str, Any] = field(default_factory=dict)


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
    "not_contains": _OperatorAnswer(
        {"not_contains": "W1"}, {"probe": ["W0"]}, {"probe": ["W0", "W1"]}
    ),
    "regex": _OperatorAnswer({"regex": "^PAY-[0-9]+$"}, {"probe": "PAY-1"}, {"probe": "REF-1"}),
    "not_regex": _OperatorAnswer({"not_regex": "^PAY-"}, {"probe": "REF-1"}, {"probe": "PAY-1"}),
    "gt": _OperatorAnswer({"gt": 10.0}, {"probe": 11}, {"probe": 10}),
    "gte": _OperatorAnswer({"gte": 10.0}, {"probe": 10}, {"probe": 9.5}),
    "lt": _OperatorAnswer({"lt": 10.0}, {"probe": 9.5}, {"probe": 10}),
    "lte": _OperatorAnswer({"lte": 10.0}, {"probe": 10}, {"probe": 10.5}),
    "date_gt": _OperatorAnswer(
        {"date_gt": "2026-03-01"}, {"probe": "2026-03-02"}, {"probe": "2026-03-01"}
    ),
    "date_gte": _OperatorAnswer(
        {"date_gte": "2026-03-01T12:00:00Z"},
        {"probe": "2026-03-01T12:00:00+00:00"},
        {"probe": "2026-03-01T11:59:59Z"},
    ),
    "date_lt": _OperatorAnswer(
        {"date_lt": "2026-03-01"}, {"probe": "2026-02-28T23:59:59Z"}, {"probe": "next week"}
    ),
    "date_lte": _OperatorAnswer(
        {"date_lte": "2026-03-01"},
        {"probe": "2026-03-01T00:00:00+02:00"},
        {"probe": "2026-03-01T00:00:01Z"},
    ),
    "in_": _OperatorAnswer({"in_": ["USD", "EUR"]}, {"probe": "EUR"}, {"probe": "JPY"}),
    "not_in": _OperatorAnswer({"not_in": ["USD", "EUR"]}, {"probe": "JPY"}, {"probe": "EUR"}),
    "len_gt": _OperatorAnswer({"len_gt": 2}, {"probe": "abc"}, {"probe": "ab"}),
    "len_gte": _OperatorAnswer({"len_gte": 2}, {"probe": "ab"}, {"probe": "a"}),
    "exists": _OperatorAnswer({"exists": True}, {"probe": ""}, {}),
    "is_null": _OperatorAnswer({"is_null": True}, {"probe": None}, {"probe": "PAY-1"}),
    "omitted": _OperatorAnswer({"omitted": True}, {}, {"probe": None}),
    "equals_binding": _OperatorAnswer(
        {"equals_binding": "bound"}, {"probe": "PAY-1"}, {"probe": "PAY-2"}, {"bound": "PAY-1"}
    ),
    "contains_binding": _OperatorAnswer(
        {"contains_binding": "bound"},
        {"probe": ["W0", "W1"]},
        {"probe": ["W0"]},
        {"bound": "W1"},
    ),
}


def test_the_answer_table_spans_the_operators_a_predicate_declares():
    """Three sources: the table, the written-out vocabulary, and the model's own fields.

    The binding subset is a fourth pair: the model names which operators take a
    binding name, and the evaluator dispatches them off its own map. A member in one
    and not the other either resolves a name as a literal or raises on a name the
    model admits.
    """
    assert set(_OPERATOR_ANSWERS) == TRACE_PREDICATE_OPERATORS
    assert set(ValuePredicate.model_fields) == TRACE_PREDICATE_OPERATORS
    assert set(_BINDING_OPERATORS) == TRACE_PREDICATE_BINDING_OPERATORS
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
        _timeline(recorded=[recorded_call("probe", arguments=answer.holds_for)]),
        matcher,
        answer.bindings,
    )
    fails = select_events(
        _timeline(recorded=[recorded_call("probe", arguments=answer.fails_for)]),
        matcher,
        answer.bindings,
    )

    assert len(holds.matched) == 1
    assert fails.matched == ()
    assert fails.undecidable == ()


def test_a_status_literal_no_executor_produces_is_rejected_at_load() -> None:
    """A status predicate naming a non-``ToolExecutionStatus`` value fails at load.

    Loading ``status: {equals: "expired"}`` clean and only failing at grading
    time would report the typo as an agent failure. The gate keeps this
    syntactic — closed-vocabulary operators (``equals``, ``not_equals``,
    ``in_``, ``not_in``) validate every literal against the enum.
    """
    with pytest.raises(ValueError, match="expired"):
        TraceMatcher(
            kind=TraceEventKind.TOOL_RESULT,
            status=ValuePredicate(equals="expired"),
        )
    with pytest.raises(ValueError, match="pending"):
        TraceMatcher(
            kind=TraceEventKind.TOOL_RESULT,
            status=ValuePredicate(in_=["success", "pending"]),
        )


def test_a_status_literal_that_is_a_real_enum_member_is_admitted() -> None:
    """Every ``ToolExecutionStatus`` value stays a valid predicate literal."""
    for admitted in ("success", "error", "timeout", "tool_not_found", "invalid_arguments"):
        TraceMatcher(
            kind=TraceEventKind.TOOL_RESULT,
            status=ValuePredicate(equals=admitted),
        )


def _argument_probe(predicate: dict[str, Any], arguments: dict[str, Any]) -> int:
    """How many events a one-call timeline yields under one args predicate."""
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"probe": ValuePredicate(**predicate)},
    )
    outcome = select_events(
        _timeline(recorded=[recorded_call("probe", arguments=arguments)]), matcher, {}
    )
    assert outcome.undecidable == ()
    return len(outcome.matched)


def test_a_naive_datetime_reads_as_utc_on_both_sides() -> None:
    """One normalization policy: no offset means UTC, never the grader's clock.

    A wall-clock-local reading would grade one trajectory differently per host.
    The probe pairs a naive bound with an offset value and the reverse, so a
    drift on either side of the comparison fails here.
    """
    assert _argument_probe({"date_gte": "2026-03-01T12:00"}, {"probe": "2026-03-01T12:00:00Z"}) == 1
    assert (
        _argument_probe({"date_gte": "2026-03-01T12:00"}, {"probe": "2026-03-01T13:00:00+02:00"})
        == 0
    )


def test_a_date_only_value_reads_as_midnight_utc() -> None:
    """The mixed date/datetime comparison coerces the date to midnight UTC."""
    assert _argument_probe({"date_lte": "2026-03-01T00:00:00Z"}, {"probe": "2026-03-01"}) == 1
    assert _argument_probe({"date_lt": "2026-03-01T00:00:01Z"}, {"probe": "2026-03-01"}) == 1
    assert _argument_probe({"date_gt": "2026-03-01T00:00:00Z"}, {"probe": "2026-03-01"}) == 0


def test_an_absent_or_null_argument_satisfies_no_date_comparison() -> None:
    """The ``None`` guard reaches the date operators like every other operator.

    A call that never carried the argument — and one that carried JSON ``null`` —
    met no deadline, so both are unmatched rather than vacuously compared.
    """
    assert _argument_probe({"date_lte": "2026-03-01"}, {}) == 0
    assert _argument_probe({"date_lte": "2026-03-01"}, {"probe": None}) == 0


# The normative three-state matrix from the v2 spec: one operator against a call
# whose probed argument was never sent, one that sent explicit JSON null, and one
# that sent a value. ``exists`` keeps its pre-v2 conflated reading — the two new
# operators subdivide it, they do not move it.
_THREE_STATES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("omitted", {}),
    ("null", {"probe": None}),
    ("valued", {"probe": "v"}),
)

_NULLNESS_MATRIX: dict[str, dict[str, bool]] = {
    "exists:true": dict(zip(("omitted", "null", "valued"), (False, False, True))),
    "exists:false": dict(zip(("omitted", "null", "valued"), (True, True, False))),
    "omitted:true": dict(zip(("omitted", "null", "valued"), (True, False, False))),
    "omitted:false": dict(zip(("omitted", "null", "valued"), (False, True, True))),
    "is_null:true": dict(zip(("omitted", "null", "valued"), (False, True, False))),
    "is_null:false": dict(zip(("omitted", "null", "valued"), (True, False, True))),
    "equals:v": dict(zip(("omitted", "null", "valued"), (False, False, True))),
    "not_equals:x": dict(zip(("omitted", "null", "valued"), (False, False, True))),
    "not_contains:x": dict(zip(("omitted", "null", "valued"), (False, False, True))),
    "date_lte:2026-03-01": dict(zip(("omitted", "null", "valued"), (False, False, False))),
}

_MATRIX_PREDICATES: dict[str, dict[str, Any]] = {
    "exists:true": {"exists": True},
    "exists:false": {"exists": False},
    "omitted:true": {"omitted": True},
    "omitted:false": {"omitted": False},
    "is_null:true": {"is_null": True},
    "is_null:false": {"is_null": False},
    "equals:v": {"equals": "v"},
    "not_equals:x": {"not_equals": "x"},
    "not_contains:x": {"not_contains": "x"},
    "date_lte:2026-03-01": {"date_lte": "2026-03-01"},
}


@pytest.mark.parametrize("row", sorted(_NULLNESS_MATRIX))
def test_the_three_state_matrix_holds_per_operator(row: str) -> None:
    """Omitted, explicit null, and valued are three states, not two.

    The row for every non-nullness operator is identical to its pre-v2 row —
    ``None`` and a missing key are both unmatched — which is the compatibility
    claim: the sentinel that separates the first two states is readable only
    through ``is_null`` / ``omitted``.
    """
    for state, arguments in _THREE_STATES:
        expected = _NULLNESS_MATRIX[row][state]
        got = _argument_probe(_MATRIX_PREDICATES[row], arguments) == 1
        assert got is expected, f"{row} over {state}: expected {expected}, got {got}"


def test_a_missing_intermediate_key_reads_as_omitted() -> None:
    """A dotted path that dies mid-walk is an omitted tail, not an error."""
    assert _argument_probe({"omitted": True}, {"other": 1}) == 1
    nested = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"body.penalty": ValuePredicate(omitted=True)},
    )
    outcome = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={"body": {"kept": 1}})]), nested, {}
    )
    assert len(outcome.matched) == 1
    hit = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"body.penalty": ValuePredicate(is_null=True)},
    )
    null_at_tail = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={"body": {"penalty": None}})]),
        hit,
        {},
    )
    assert len(null_at_tail.matched) == 1


def test_a_nullness_probe_on_recorded_evidence_is_rejected_at_load() -> None:
    """On ``status``/``executor``/``result`` a ``None`` is missing evidence.

    The evaluator holds those readings undecidable rather than reads them, so a
    nullness probe there would answer a question the record cannot ask.
    """
    with pytest.raises(ValueError, match="missing evidence"):
        TraceMatcher(kind=TraceEventKind.TOOL_RESULT, status=ValuePredicate(is_null=True))
    with pytest.raises(ValueError, match="missing evidence"):
        TraceMatcher(kind=TraceEventKind.TOOL_RESULT, result=ValuePredicate(omitted=True))


def test_a_negative_text_predicate_composes_with_its_positive_form() -> None:
    """One predicate, both polarities: "mentions the item, not the wrong one".

    The conjunction is the point of the feature — a selection like "a successful
    search whose result shows the range and no shift on the target date" is one
    matcher, not a ``negate`` over the whole constraint.
    """
    both = {"contains": "item", "not_contains": "W2"}
    assert _argument_probe(both, {"probe": "item W1"}) == 1
    assert _argument_probe(both, {"probe": "item W2"}) == 0
    assert _argument_probe(both, {"probe": "unrelated"}) == 0


def test_an_absent_or_null_argument_satisfies_no_negative_text_predicate() -> None:
    """ "Does not contain X" is a claim about a value the event carries.

    A call that never carried the argument must not satisfy ``not_contains`` — the
    vacuous reading would pass a failed lookup as "no duplicate found".
    """
    assert _argument_probe({"not_contains": "W1"}, {}) == 0
    assert _argument_probe({"not_contains": "W1"}, {"probe": None}) == 0
    assert _argument_probe({"not_regex": "duplicate"}, {}) == 0


def test_a_negative_text_predicate_reads_an_empty_string_as_a_value() -> None:
    """An empty string is present evidence, and it contains nothing."""
    assert _argument_probe({"not_contains": "W1"}, {"probe": ""}) == 1
    assert _argument_probe({"not_regex": "duplicate"}, {"probe": ""}) == 1


def test_a_numeric_comparison_still_refuses_a_date_string() -> None:
    """``gt`` reads a real number and nothing else — the date operators are the
    spelling for chronology, not a widening of the numeric ones."""
    assert _argument_probe({"gt": 0}, {"probe": "2026-03-02"}) == 0
