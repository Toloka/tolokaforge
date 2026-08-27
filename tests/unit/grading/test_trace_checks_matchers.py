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
from pydantic import ValidationError

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core.grading.trace_checks import (
    _binding_operator_names,
    _extracted,
    select_events,
)
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import (
    BoundValue,
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
    "not_contains": _OperatorAnswer(
        {"not_contains": "REFUND"}, {"probe": "PAY-1"}, {"probe": "REFUND-1"}
    ),
    "not_equals": _OperatorAnswer({"not_equals": "PAY-1"}, {"probe": "PAY-2"}, {"probe": "PAY-1"}),
    "regex": _OperatorAnswer({"regex": "^PAY-[0-9]+$"}, {"probe": "PAY-1"}, {"probe": "REF-1"}),
    "not_regex": _OperatorAnswer({"not_regex": "^PAY-"}, {"probe": "REF-1"}, {"probe": "PAY-1"}),
    "is_null": _OperatorAnswer({"is_null": True}, {"probe": None}, {"probe": "value"}),
    "omitted": _OperatorAnswer({"omitted": True}, {}, {"probe": "value"}),
    "gt": _OperatorAnswer({"gt": 10.0}, {"probe": 11}, {"probe": 10}),
    "gte": _OperatorAnswer({"gte": 10.0}, {"probe": 10}, {"probe": 9.5}),
    "lt": _OperatorAnswer({"lt": 10.0}, {"probe": 9.5}, {"probe": 10}),
    "lte": _OperatorAnswer({"lte": 10.0}, {"probe": 10}, {"probe": 10.5}),
    "in_": _OperatorAnswer({"in_": ["USD", "EUR"]}, {"probe": "EUR"}, {"probe": "JPY"}),
    "not_in": _OperatorAnswer({"not_in": ["USD", "EUR"]}, {"probe": "JPY"}, {"probe": "EUR"}),
    "len_gt": _OperatorAnswer({"len_gt": 2}, {"probe": "abc"}, {"probe": "ab"}),
    "len_gte": _OperatorAnswer({"len_gte": 2}, {"probe": "ab"}, {"probe": "a"}),
    "exists": _OperatorAnswer({"exists": True}, {"probe": ""}, {}),
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
    assert set(_binding_operator_names()) == TRACE_PREDICATE_BINDING_OPERATORS
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


# --------------------------------------------------------------------------
# The nullness pair: ``is_null`` and ``omitted``
# --------------------------------------------------------------------------

_THREE_STATE_MATRIX: tuple[tuple[str, bool, dict[str, Any], bool], ...] = (
    ("is_null", True, {"key": None}, True),
    ("is_null", True, {}, False),
    ("is_null", True, {"key": "value"}, False),
    ("is_null", False, {"key": None}, False),
    ("is_null", False, {}, True),
    ("is_null", False, {"key": "value"}, True),
    ("omitted", True, {"key": None}, False),
    ("omitted", True, {}, True),
    ("omitted", True, {"key": "value"}, False),
)


@pytest.mark.parametrize(("operator", "expected", "arguments", "holds"), _THREE_STATE_MATRIX)
def test_the_three_state_matrix_holds_per_operator(
    operator: str, expected: bool, arguments: dict[str, Any], holds: bool
) -> None:
    """The whole is_null / omitted semantic in one table.

    Three argument-state axes cross both operators: an explicit JSON ``null`` at
    the key, a key that was never sent, and an ordinary value. The rules the
    matrix locks — ``is_null`` and ``omitted`` are not synonyms, ``omitted`` is
    false on ``{key: None}``, and ``is_null: False`` reads a key that was never
    sent as a hold (no null there) — are all a future refactor could get subtly
    wrong.
    """
    timeline = _timeline(recorded=[recorded_call("probe", arguments=arguments)])
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"key": ValuePredicate(**{operator: expected})},
    )

    outcome = select_events(timeline, matcher, {})

    assert bool(outcome.matched) is holds


def test_a_missing_intermediate_key_reads_as_omitted() -> None:
    """A path whose ancestor is absent or is not a mapping reads as omitted.

    ``args.body.query`` on a call whose ``body`` is ``{}`` — the ``query``
    segment cannot be resolved because its parent carries no such key. The same
    reading holds when ``body`` is not a mapping at all, so the two shapes of
    unresolvability collapse under ``omitted``.
    """
    matcher = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"body.query": ValuePredicate(omitted=True)},
    )

    missing_intermediate = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={"body": {}})]), matcher, {}
    )
    non_mapping_intermediate = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={"body": None})]), matcher, {}
    )

    assert len(missing_intermediate.matched) == 1
    assert len(non_mapping_intermediate.matched) == 1


@pytest.mark.parametrize("field", ["status", "executor", "result"])
@pytest.mark.parametrize("operator", ["is_null", "omitted"])
def test_a_nullness_probe_on_recorded_evidence_is_rejected_at_load(
    field: str, operator: str
) -> None:
    """``None`` on those three fields is missing evidence, not authored null.

    A bundle re-graded without its tool-call record has all three read as
    ``None``; a matcher that could not tell that gap apart from an author's
    explicit assertion would surface the gap as agent failure. The gate reports
    the offending field so the fix reads directly.
    """
    kind = TraceEventKind.TOOL_RESULT if field in ("status", "result") else TraceEventKind.TOOL_CALL
    with pytest.raises(ValidationError) as raised:
        TraceMatcher(kind=kind, **{field: ValuePredicate(**{operator: True})})

    message = str(raised.value)
    assert field in message
    assert "is_null" in message and "omitted" in message
    assert "exists" in message


def test_a_nullness_probe_on_an_args_predicate_is_admitted() -> None:
    """``args`` and ``text`` carry no missing-evidence ambiguity, so nullness there loads.

    The gate refuses ``status`` / ``executor`` / ``result`` and no field beyond
    them; a matcher probing arguments loads cleanly under both operators.
    """
    on_args = TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"note": ValuePredicate(is_null=True), "trace_id": ValuePredicate(omitted=True)},
    )
    on_text = TraceMatcher(kind=TraceEventKind.ASSISTANT_MESSAGE, text=ValuePredicate(is_null=True))

    assert on_args.args is not None
    assert on_text.text is not None


def test_a_binder_extraction_reads_absent_and_null_as_one_condition() -> None:
    """The ``_MISSING`` sentinel does not leak into a bound value.

    A binding reading ``args.body.query`` off a call that carries no ``body``,
    a call that carries ``body: None``, or a call that carries ``body: {}``
    (missing the ``query`` key) extracts nothing in every case. If the sentinel
    leaked, ``_extracted`` would return ``[_MISSING]`` and every reference in
    the constraint would resolve against an in-band object no operator answers
    for.
    """
    bound = BoundValue(field="args.body.query")

    for arguments in ({}, {"body": None}, {"body": {}}):
        event = _only(
            _timeline(recorded=[recorded_call("probe", arguments=arguments)]),
            TraceEventKind.TOOL_CALL,
        )
        assert _extracted(bound, event, None) == []


def test_omitted_composes_with_withhold() -> None:
    """The canonical boundary between #1292 and #1293.

    A constraint whose anchor selects on ``omitted: true`` looks for a call
    that never sent ``body.query``. Against a timeline where the call did send
    it, the anchor yields no candidate; ``on_missing: withhold`` opts the
    constraint out of scoring rather than surfacing an agent failure.
    """
    timeline = _timeline(recorded=[recorded_call("probe", arguments={"body": {"query": "found"}})])
    require = {
        "present": {
            "match": {
                "kind": "tool_call",
                "args": {"body.query": {"omitted": True}},
            }
        }
    }

    verdict = evaluate_constraint(timeline, require, on_missing="withhold")

    assert verdict.withheld is True
    assert verdict.passed is False
    assert verdict.undecided is False


def test_omitted_composes_with_withhold_fails_without_the_opt_out() -> None:
    """The default ``on_missing: fail`` reads an omitted anchor as an agent failure.

    Same timeline, same anchor, but no ``on_missing`` on the constraint. The
    withhold verdict is the opt-in behaviour, not the default: an author who
    did not name it sees the constraint fail definitively.
    """
    timeline = _timeline(recorded=[recorded_call("probe", arguments={"body": {"query": "found"}})])
    require = {
        "present": {
            "match": {
                "kind": "tool_call",
                "args": {"body.query": {"omitted": True}},
            }
        }
    }

    verdict = evaluate_constraint(timeline, require)

    assert verdict.withheld is False
    assert verdict.passed is False
    assert verdict.undecided is False
