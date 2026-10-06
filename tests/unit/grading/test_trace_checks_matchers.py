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

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core import plugin_registry
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.regex_engine import RegexEngineKind, UncompilablePattern
from tolokaforge.core.grading.trace_checks import (
    _binding_operator_names,
    _extracted,
    evaluate_trace_checks,
    select_events,
)
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import (
    BoundValue,
    GradingConfig,
    Message,
    MessageRole,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    TraceChecksConfig,
    TraceChecksResult,
    TraceMatcher,
    Trajectory,
    ValuePredicate,
)
from tolokaforge.runner.models import (
    TRACE_PREDICATE_BINDING_OPERATORS,
    TRACE_PREDICATE_MODIFIERS,
    TRACE_PREDICATE_OPERATORS,
)

pytestmark = pytest.mark.unit

_BLOCK_ENGINE = TraceChecksConfig.model_fields["regex_engine"].default
"""What a ``trace_checks`` block declaring no ``regex_engine`` resolves its matchers under."""

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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert _only(timeline, TraceEventKind.ASSISTANT_MESSAGE).status is None
    assert [event.kind for event in outcome.matched] == [TraceEventKind.TOOL_RESULT]
    assert outcome.undecidable == ()


def test_an_assistant_message_matcher_passes_over_the_call_whose_text_is_none():
    timeline = _timeline(recorded=[_payment_lookup()])
    matcher = TraceMatcher(
        kind=TraceEventKind.ASSISTANT_MESSAGE, text=ValuePredicate(contains="PAY-664306")
    )

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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
        regex_engine=_BLOCK_ENGINE,
    )
    absent = select_events(
        timeline,
        TraceMatcher(
            kind=TraceEventKind.TOOL_CALL,
            args={"refund_id": ValuePredicate(exists=False)},
        ),
        {},
        regex_engine=_BLOCK_ENGINE,
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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert outcome.matched == (_only(timeline, TraceEventKind.TOOL_CALL),)
    assert select_events(timeline, other_path, {}, regex_engine=_BLOCK_ENGINE).matched == ()


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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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
    "date_gt": _OperatorAnswer(
        {"date_gt": "2026-03-01"},
        {"probe": "2026-04-01"},
        {"probe": "2026-02-01"},
    ),
    "date_gte": _OperatorAnswer(
        {"date_gte": "2026-03-01"},
        {"probe": "2026-03-01"},
        {"probe": "2026-02-01"},
    ),
    "date_lt": _OperatorAnswer(
        {"date_lt": "2026-03-01"},
        {"probe": "2026-02-01"},
        {"probe": "2026-04-01"},
    ),
    "date_lte": _OperatorAnswer(
        {"date_lte": "2026-03-01"},
        {"probe": "2026-03-01"},
        {"probe": "2026-04-01"},
    ),
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

    The model's fields are the operators plus the modifiers, which change how an
    operator reads and are never dispatched themselves — a modifier counted as an
    operator would let ``{regex_engine: linear}`` alone read as a declared predicate.

    The binding subset is a fourth pair: the model names which operators take a
    binding name, and the evaluator dispatches them off its own map. A member in one
    and not the other either resolves a name as a literal or raises on a name the
    model admits.
    """
    assert set(_OPERATOR_ANSWERS) == TRACE_PREDICATE_OPERATORS
    assert set(ValuePredicate.model_fields) - TRACE_PREDICATE_MODIFIERS == TRACE_PREDICATE_OPERATORS
    assert TRACE_PREDICATE_MODIFIERS.isdisjoint(TRACE_PREDICATE_OPERATORS)
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
        regex_engine=_BLOCK_ENGINE,
    )
    fails = select_events(
        _timeline(recorded=[recorded_call("probe", arguments=answer.fails_for)]),
        matcher,
        answer.bindings,
        regex_engine=_BLOCK_ENGINE,
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

    outcome = select_events(timeline, matcher, {}, regex_engine=_BLOCK_ENGINE)

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
        _timeline(recorded=[recorded_call("probe", arguments={"body": {}})]),
        matcher,
        {},
        regex_engine=_BLOCK_ENGINE,
    )
    non_mapping_intermediate = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={"body": None})]),
        matcher,
        {},
        regex_engine=_BLOCK_ENGINE,
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
        assert _extracted(bound, None, event, None) == []


def test_omitted_composes_with_withhold() -> None:
    """``on_missing: withhold`` composes with a nullness-anchor selection.

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


# --------------------------------------------------------------------------
# The chronological pair: ``date_gt`` / ``date_gte`` / ``date_lt`` / ``date_lte``
# --------------------------------------------------------------------------


def _date_matcher(**predicate: Any) -> TraceMatcher:
    return TraceMatcher(
        kind=TraceEventKind.TOOL_CALL,
        args={"issued_at": ValuePredicate(**predicate)},
    )


def _at(issued_at: Any) -> TrialTimeline:
    return _timeline(recorded=[recorded_call("probe", arguments={"issued_at": issued_at})])


def test_a_date_only_value_reads_as_midnight_utc() -> None:
    """The one anti-flake behind the shared normalization.

    ``date_gt: "2026-03-01"`` names a bound at midnight UTC of March 1. A
    value one millisecond into that day holds; the day's own midnight does
    not — the strict comparison the operator name promises reads the two
    equal.
    """
    matcher = _date_matcher(date_gt="2026-03-01")

    just_after = select_events(
        _at("2026-03-01T00:00:00.001Z"), matcher, {}, regex_engine=_BLOCK_ENGINE
    )
    at_midnight = select_events(_at("2026-03-01"), matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert len(just_after.matched) == 1
    assert at_midnight.matched == ()


def test_a_naive_datetime_reads_as_utc_on_both_sides() -> None:
    """The load-time policy the anti-flake the whole shape gate exists for turns on.

    A datetime carrying no offset — ``2026-03-01T12:00:00`` — reads as UTC on
    both sides of the comparison. Reading it as the grader's wall clock
    would make one trajectory grade differently per host, so the three cells
    below span the boundary the operator promises to hold: the naive bound
    equal to a ``+00:00``-tagged value (no hold, they are the same
    instant); one hour earlier (no hold); one hour later (hold).
    """
    matcher = _date_matcher(date_gt="2026-03-01T12:00:00")

    equal = select_events(_at("2026-03-01T12:00:00+00:00"), matcher, {}, regex_engine=_BLOCK_ENGINE)
    earlier = select_events(_at("2026-03-01T11:00:00Z"), matcher, {}, regex_engine=_BLOCK_ENGINE)
    later = select_events(_at("2026-03-01T13:00:00Z"), matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert equal.matched == ()
    assert earlier.matched == ()
    assert len(later.matched) == 1


@pytest.mark.parametrize("operator", ["date_gt", "date_gte", "date_lt", "date_lte"])
def test_an_absent_or_null_argument_satisfies_no_date_comparison(operator: str) -> None:
    """The timeline contract: a predicate on a missing or null field is unmatched.

    ``exists`` is the one operator that reads presence, and the four date
    operators are not it. A call that never sent ``issued_at`` and a call
    that sent it as JSON ``null`` both drop the matcher.
    """
    matcher = _date_matcher(**{operator: "2026-03-01"})

    missing = select_events(
        _timeline(recorded=[recorded_call("probe", arguments={})]),
        matcher,
        {},
        regex_engine=_BLOCK_ENGINE,
    )
    null = select_events(_at(None), matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert missing.matched == ()
    assert null.matched == ()


def test_a_numeric_comparison_still_refuses_a_date_string() -> None:
    """The four date operators do not widen the numeric ones.

    A ``gt: 0`` predicate over a value ``"2026-03-01"`` remains False — a
    date string is not a number, and vice versa. The two comparison
    vocabularies stay disjoint at the value tier.
    """
    numeric_matcher = _date_matcher(gt=0.0)
    date_matcher = _date_matcher(date_gt="2026-03-01")

    numeric_over_date = select_events(
        _at("2026-03-01"), numeric_matcher, {}, regex_engine=_BLOCK_ENGINE
    )
    date_over_number = select_events(_at(5), date_matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert numeric_over_date.matched == ()
    assert date_over_number.matched == ()


def test_a_range_predicate_composes_the_two_ends() -> None:
    """One matcher, both bounds. Ranges are expressible without a ``date_between``.

    ``{date_gte: "2026-03-01", date_lt: "2026-04-01"}`` selects March 2026,
    exclusive of April 1 midnight UTC — the conventional half-open reading
    of a month, spelled by the operators the codebase already ships.
    """
    matcher = _date_matcher(date_gte="2026-03-01", date_lt="2026-04-01")

    mid_march = select_events(_at("2026-03-15"), matcher, {}, regex_engine=_BLOCK_ENGINE)
    april_first_midnight = select_events(
        _at("2026-04-01T00:00:00Z"), matcher, {}, regex_engine=_BLOCK_ENGINE
    )
    late_february = select_events(_at("2026-02-28"), matcher, {}, regex_engine=_BLOCK_ENGINE)

    assert len(mid_march.matched) == 1
    assert april_first_midnight.matched == ()
    assert late_february.matched == ()


def test_a_valid_date_literal_is_admitted_at_load() -> None:
    """The accepted-shape surface, spelled by every author-writable literal.

    An ISO-8601 date, a Z-suffixed datetime, an offset-tagged datetime, and
    a fractional-second variant all construct cleanly — the shape gate
    admits every author who wrote what the docstring names.
    """
    for literal in (
        "2026-03-01",
        "2026-03-01T12:00:00Z",
        "2026-03-01T12:00:00+02:00",
        "2026-03-01T12:00:00.123456Z",
    ):
        loaded = ValuePredicate(date_gte=literal)
        assert loaded.date_gte == literal


# --------------------------------------------------------------------------
# The engine a pattern runs on: the block's, unless its predicate or bound value
# names its own. ``\d`` against an Arabic-Indic digit is the probe, because both
# engines compile it and only ``backtracking`` reads that digit as one.

_LINEAR = RegexEngineKind.LINEAR
_BACKTRACKING = RegexEngineKind.BACKTRACKING
_ARABIC_INDIC_THREE = "٣"

_EFFECTIVE_ENGINES = [
    pytest.param(None, None, _LINEAR, id="block-undeclared"),
    pytest.param(None, _BACKTRACKING, _BACKTRACKING, id="override-backtracking-in-undeclared"),
    pytest.param(_LINEAR, None, _LINEAR, id="block-linear"),
    pytest.param(_BACKTRACKING, None, _BACKTRACKING, id="block-backtracking"),
    pytest.param(_LINEAR, _BACKTRACKING, _BACKTRACKING, id="override-backtracking-in-linear"),
    pytest.param(_BACKTRACKING, _LINEAR, _LINEAR, id="override-linear-in-backtracking"),
]


def _graded(
    turns: Sequence[tuple[str, str]],
    constraint: dict[str, Any],
    block_engine: RegexEngineKind | None,
    recorded: Sequence[RecordedToolCall] = (),
) -> TraceChecksResult:
    """Graded under a block declaring ``block_engine``, or declaring none for ``None``."""
    declared = {} if block_engine is None else {"regex_engine": block_engine}
    config = TraceChecksConfig(
        constraints=[{"id": "probe", "description": "the engine probe", **constraint}],
        **declared,
    )
    return evaluate_trace_checks(build_timeline(turns=turns, recorded=recorded), config)


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize(("block_engine", "override", "effective"), _EFFECTIVE_ENGINES)
def test_a_matcher_pattern_runs_on_its_effective_engine(
    operator: str,
    block_engine: RegexEngineKind | None,
    override: RegexEngineKind | None,
    effective: RegexEngineKind,
) -> None:
    predicate: dict[str, Any] = {operator: r"\d"}
    if override is not None:
        predicate["regex_engine"] = override
    constraint = {
        "require": {"present": {"match": {"kind": "assistant_message", "text": predicate}}}
    }

    result = _graded(
        [("assistant", f"Your code is {_ARABIC_INDIC_THREE}.")], constraint, block_engine
    )

    reads_a_digit = effective is _BACKTRACKING
    assert result.passed is (reads_a_digit if operator == "regex" else not reads_a_digit)


@pytest.mark.parametrize(("block_engine", "override", "effective"), _EFFECTIVE_ENGINES)
def test_a_capture_pattern_runs_on_its_effective_engine(
    block_engine: RegexEngineKind | None,
    override: RegexEngineKind | None,
    effective: RegexEngineKind,
) -> None:
    """Only ``backtracking`` captures the digit, so only there does the binder bind."""
    bound: dict[str, Any] = {"field": "text", "pattern": r"code (\d)"}
    if override is not None:
        bound["regex_engine"] = override
    constraint = {
        "bind": {"match": {"kind": "user_message"}, "values": {"code": bound}},
        "require": {
            "present": {
                "match": {"kind": "assistant_message", "text": {"contains_binding": "code"}}
            }
        },
    }

    result = _graded(
        [
            ("user", f"My code {_ARABIC_INDIC_THREE} is lost."),
            ("assistant", f"Resetting code {_ARABIC_INDIC_THREE}."),
        ],
        constraint,
        block_engine,
    )

    assert result.passed is (effective is _BACKTRACKING)


_REFUSED_PATTERNS = [
    pytest.param(_LINEAR, "(?=a)", id="linear-lookahead"),
    pytest.param(_BACKTRACKING, "unterminated([", id="backtracking-unterminated"),
]


@pytest.mark.parametrize("as_list", [False, True], ids=["string", "second-list-item"])
@pytest.mark.parametrize(("block_engine", "pattern"), _REFUSED_PATTERNS)
def test_a_refused_matcher_pattern_raises_on_a_timeline_it_never_reaches(
    block_engine: RegexEngineKind, pattern: str, as_list: bool
) -> None:
    """Compiled before any event is read, so the timeline carrying no tool call at all
    does not let it through — and a list compiles every item, not only the first."""
    authored: str | list[str] = ["^http", pattern] if as_list else pattern
    constraint = {
        "require": {"absent": {"match": {"kind": "tool_call", "tool": {"regex": authored}}}}
    }

    with pytest.raises(UncompilablePattern) as excinfo:
        _graded([("user", "hi"), ("assistant", "hello")], constraint, block_engine)

    assert excinfo.value.engine is block_engine
    assert excinfo.value.pattern == pattern


@pytest.mark.parametrize(("block_engine", "pattern"), _REFUSED_PATTERNS)
def test_a_refused_capture_pattern_raises_on_a_timeline_it_never_reaches(
    block_engine: RegexEngineKind, pattern: str
) -> None:
    constraint = {
        "bind": {
            "match": {"kind": "tool_call", "tool": {"equals": "open_case"}},
            "values": {"case": {"field": "args.note", "pattern": f"{pattern}(x)"}},
        },
        "require": {
            "present": {
                "match": {"kind": "assistant_message", "text": {"contains_binding": "case"}}
            }
        },
    }

    with pytest.raises(UncompilablePattern) as excinfo:
        _graded([("user", "hi"), ("assistant", "hello")], constraint, block_engine)

    assert excinfo.value.engine is block_engine


def _unbindable_constraint(pattern: str) -> dict[str, Any]:
    """A bound constraint whose ``require`` tree alone declares ``pattern``."""
    return {
        "id": "refused",
        "description": "a pattern under a binder that never fires",
        "bind": {
            "match": {"kind": "tool_call", "tool": {"equals": "open_case"}},
            "values": {"case": {"field": "args.case_id"}},
        },
        "require": {
            "present": {
                "match": {
                    "kind": "assistant_message",
                    "text": {"regex": pattern, "contains_binding": "case"},
                }
            }
        },
    }


_ROUTE_FILLER = {
    "id": "filler",
    "description": "a constraint naming no pattern",
    "require": {"present": {"match": {"kind": "assistant_message"}}},
}


@pytest.mark.parametrize(
    "turns",
    [
        pytest.param((("user", "hi"), ("assistant", "hello")), id="binder-selects-nothing"),
        pytest.param((), id="timeline-without-events"),
    ],
)
@pytest.mark.parametrize("where", ["shared", "route"])
@pytest.mark.parametrize(("block_engine", "pattern"), _REFUSED_PATTERNS)
def test_a_refused_require_pattern_raises_where_its_tree_is_never_entered(
    block_engine: RegexEngineKind,
    pattern: str,
    where: str,
    turns: Sequence[tuple[str, str]],
) -> None:
    """A binder that selects nothing leaves its ``require`` tree unresolved, and a
    timeline without events leaves every tree unresolved — the block's patterns are
    compiled before either is read, so neither lets a refused one through."""
    refused = _unbindable_constraint(pattern)
    block: dict[str, Any] = (
        {"constraints": [refused]}
        if where == "shared"
        else {
            "alternatives": [
                {"id": "a", "description": "the route holding it", "constraints": [refused]},
                {"id": "b", "description": "a clean route", "constraints": [_ROUTE_FILLER]},
            ]
        }
    )
    config = TraceChecksConfig(**block, regex_engine=block_engine)

    with pytest.raises(UncompilablePattern) as excinfo:
        evaluate_trace_checks(build_timeline(turns=turns), config)

    assert excinfo.value.engine is block_engine
    assert excinfo.value.pattern == pattern


# --------------------------------------------------------------------------
# A pattern list: every pattern of a ``regex`` list must search the value and no
# pattern of a ``not_regex`` list may. The issue's lookahead conjunction, split
# into its two halves, is the probe.

_ACCOUNT_ID = r'"account_id":\s*"ACC-00000006"'
_EMAIL = r'"email":\s*"x@y.z"'
_ACCOUNT_LOOKAHEADS = rf"(?=[\s\S]*{_ACCOUNT_ID})(?=[\s\S]*{_EMAIL})"

_BOTH = '{"account_id": "ACC-00000006", "name": "Ada", "email": "x@y.z"}'
_ACCOUNT_ID_ONLY = '{"account_id": "ACC-00000006", "name": "Ada", "email": "a@b.c"}'
_EMAIL_ONLY = '{"account_id": "ACC-00000007", "name": "Ada", "email": "x@y.z"}'
_NEITHER = '{"account_id": "ACC-00000007", "name": "Ada", "email": "a@b.c"}'

_BOTH_ENGINES = [pytest.param(_LINEAR, id="linear"), pytest.param(_BACKTRACKING, id="backtracking")]


def _account_lookup_passes(
    result: dict[str, Any], output: str, block_engine: RegexEngineKind
) -> bool:
    """Whether a ``get_account`` call whose result reads ``result`` is present."""
    constraint = {
        "require": {
            "present": {
                "match": {"kind": "tool_call", "tool": {"equals": "get_account"}, "result": result}
            }
        }
    }
    recorded = [recorded_call("get_account", output=output)]
    return _graded(
        [("user", "hi"), ("assistant", "done")], constraint, block_engine, recorded
    ).passed


@pytest.mark.parametrize("block_engine", _BOTH_ENGINES)
@pytest.mark.parametrize(
    ("output", "every_searches", "none_searches"),
    [
        pytest.param(_BOTH, True, False, id="both"),
        pytest.param(_ACCOUNT_ID_ONLY, False, False, id="account-id-only"),
        pytest.param(_EMAIL_ONLY, False, False, id="email-only"),
        pytest.param(_NEITHER, False, True, id="neither"),
    ],
)
def test_a_regex_list_needs_every_pattern_and_a_not_regex_list_refuses_any(
    block_engine: RegexEngineKind, output: str, every_searches: bool, none_searches: bool
) -> None:
    patterns = [_ACCOUNT_ID, _EMAIL]

    assert _account_lookup_passes({"regex": patterns}, output, block_engine) is every_searches
    assert _account_lookup_passes({"not_regex": patterns}, output, block_engine) is none_searches


@pytest.mark.parametrize("block_engine", _BOTH_ENGINES)
@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("output", [_BOTH, _NEITHER], ids=["searched", "not-searched"])
def test_a_one_item_list_reads_as_its_string(
    block_engine: RegexEngineKind, operator: str, output: str
) -> None:
    as_string = _account_lookup_passes({operator: _ACCOUNT_ID}, output, block_engine)
    as_list = _account_lookup_passes({operator: [_ACCOUNT_ID]}, output, block_engine)

    assert as_list is as_string


@pytest.mark.parametrize(
    ("output", "passes"),
    [
        pytest.param(_BOTH, True, id="both"),
        pytest.param(_ACCOUNT_ID_ONLY, False, id="account-id-only"),
        pytest.param(_EMAIL_ONLY, False, id="email-only"),
        pytest.param(_NEITHER, False, id="neither"),
    ],
)
def test_a_lookahead_conjunction_and_its_list_form_grade_alike(output: str, passes: bool) -> None:
    """The list is the replacement the linear engine's refusal of lookahead names."""
    lookaheads = _account_lookup_passes({"regex": _ACCOUNT_LOOKAHEADS}, output, _BACKTRACKING)
    split = _account_lookup_passes({"regex": [_ACCOUNT_ID, _EMAIL]}, output, _BACKTRACKING)

    assert lookaheads is passes
    assert split is passes


# --------------------------------------------------------------------------
# A matcher evaluates only the predicates that can change what it selects or
# reports: on a call ``tool`` already rejects, its ``result`` pattern never runs.
# The issue's lookahead conjunction under ``backtracking`` costs time quadratic in
# the text it searches, so every result it is spared is the grade's whole budget.

_OTHER_TOOL_CALLS = 6
_TRAJECTORY_TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _policy_result(size: int) -> str:
    """A ``search_policies`` result of about ``size`` characters naming neither field."""
    return '{"policies": "' + "refunds within thirty days " * (size // 27) + '"}'


def _account_lookup_after_policy_searches(
    size: int, account: str = _BOTH
) -> list[RecordedToolCall]:
    """Six large ``search_policies`` results, then the ``get_account`` call returning ``account``."""
    return [
        *(
            recorded_call("search_policies", sequence=index, output=_policy_result(size))
            for index in range(_OTHER_TOOL_CALLS)
        ),
        recorded_call(
            "get_account",
            sequence=_OTHER_TOOL_CALLS,
            arguments={"account_id": "ACC-00000006"},
            output=account,
        ),
    ]


@pytest.mark.parametrize(
    "admitting",
    [
        pytest.param({"tool": {"equals": "get_account"}}, id="tool"),
        pytest.param({"args": {"account_id": {"equals": "ACC-00000006"}}}, id="args-path"),
    ],
)
@pytest.mark.parametrize(
    ("block_engine", "pattern"),
    [
        pytest.param(_LINEAR, _ACCOUNT_ID, id="linear"),
        pytest.param(_BACKTRACKING, _ACCOUNT_LOOKAHEADS, id="backtracking-lookahead"),
    ],
)
def test_a_result_pattern_runs_only_on_the_call_a_cheaper_predicate_admits(
    monkeypatch: pytest.MonkeyPatch,
    block_engine: RegexEngineKind,
    pattern: str,
    admitting: dict[str, Any],
) -> None:
    """Counted at the real operator: an ``args`` path is read before ``result``
    although a matcher declares ``result`` first."""
    searched: list[Any] = []
    load = plugin_registry.load_trace_check_operator

    def counting_load(name: str) -> Any:
        operator = load(name)
        if name != "regex":
            return operator

        def counted(value: Any, expected: Any, bindings: Any) -> bool:
            searched.append(value)
            return operator(value, expected, bindings)

        return counted

    monkeypatch.setattr(plugin_registry, "load_trace_check_operator", counting_load)
    constraint = {
        "require": {
            "present": {"match": {"kind": "tool_call", "result": {"regex": pattern}} | admitting}
        }
    }

    result = _graded(
        [("user", "Find the account."), ("assistant", "Found it.")],
        constraint,
        block_engine,
        _account_lookup_after_policy_searches(4_000),
    )

    assert result.passed
    assert searched == [_BOTH]


def _account_lookup_trajectory(lookup: Sequence[RecordedToolCall]) -> Trajectory:
    """A trajectory whose one assistant turn declares every call ``lookup`` records."""
    declared = [
        ToolCall(id=call.call_id, name=call.tool_name, arguments=call.arguments) for call in lookup
    ]
    return Trajectory(
        task_id="account-lookup",
        trial_index=0,
        start_ts=_TRAJECTORY_TIMESTAMP,
        end_ts=_TRAJECTORY_TIMESTAMP,
        messages=[
            Message(role=MessageRole.USER, content="Find the account."),
            Message(role=MessageRole.ASSISTANT, content="Looking.", tool_calls=declared),
            Message(role=MessageRole.ASSISTANT, content="Found it."),
        ],
        tool_log=lookup,
    )


_NAMED_ACCOUNT_LOOKUP = {"kind": "tool_call", "tool": {"equals": "get_account"}}


def _account_lookup_grading(
    looked_up: str | list[str], other_account: str | list[str], **block: Any
) -> GradingConfig:
    """``get_account`` returned the account ``looked_up`` names and none ``other_account`` does."""
    return GradingConfig(
        combine={"method": "weighted", "weights": {"trace_checks": 1.0}},
        trace_checks=TraceChecksConfig(
            **block,
            constraints=[
                {
                    "id": "looked-up",
                    "description": "the account was looked up",
                    "require": {
                        "present": {
                            "match": _NAMED_ACCOUNT_LOOKUP | {"result": {"regex": looked_up}}
                        }
                    },
                },
                {
                    "id": "not-another",
                    "description": "no other account was looked up",
                    "require": {
                        "absent": {
                            "match": _NAMED_ACCOUNT_LOOKUP | {"result": {"regex": other_account}}
                        }
                    },
                },
            ],
        ),
    )


def test_a_lookahead_under_backtracking_grades_large_results_in_bounded_time() -> None:
    """Through the real engine, over results a lookahead would take minutes to search."""
    trajectory = _account_lookup_trajectory(_account_lookup_after_policy_searches(100_000))
    other_account = r'(?=[\s\S]*"account_id":\s*"ACC-00000007")(?=[\s\S]*"email")'
    config = _account_lookup_grading(_ACCOUNT_LOOKAHEADS, other_account, regex_engine=_BACKTRACKING)

    started = time.perf_counter()
    grade = GradingEngine(config).grade_trajectory(trajectory, {})
    elapsed = time.perf_counter() - started

    assert [(item.id, item.passed) for item in grade.trace_check_results] == [
        ("looked-up", True),
        ("not-another", True),
    ]
    assert grade.components.trace_checks == 1.0
    assert elapsed < 2.0


def test_the_list_form_under_the_default_engine_grades_large_results_in_bounded_time() -> None:
    """The issue's scenario as an author now writes it, declaring no engine: every
    result is large, the named call's own result included, and none of them matches."""
    size = 100_000
    unmatched_account = (
        '{"account_id": "ACC-00000006", "email": "a@b.c", "history": "'
        + "renewed the annual plan " * (size // 24)
        + '"}'
    )
    trajectory = _account_lookup_trajectory(
        _account_lookup_after_policy_searches(size, account=unmatched_account)
    )
    config = _account_lookup_grading(
        [_ACCOUNT_ID, _EMAIL], [r'"account_id":\s*"ACC-00000007"', r'"email"']
    )
    assert config.trace_checks.regex_engine is _LINEAR
    assert len(unmatched_account) >= size

    started = time.perf_counter()
    grade = GradingEngine(config).grade_trajectory(trajectory, {})
    elapsed = time.perf_counter() - started

    assert [(item.id, item.passed) for item in grade.trace_check_results] == [
        ("looked-up", False),
        ("not-another", True),
    ]
    assert elapsed < 2.0
