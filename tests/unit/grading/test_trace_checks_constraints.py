"""What a trace constraint decides over a real trial, and what it refuses to decide.

Every timeline here comes from the real builder over real messages and recorded
calls, so a verdict is read against what a graded trial produces rather than
against a hand-assembled event tuple.

The properties that carry the weight: adjacency is read in the view the author
named, because the timeline interleaves a call's result between it and the next
call; a constraint is decided only when every completion of the undecidable
evidence agrees, so evidence the trial never recorded cannot pass a negative
constraint; an anchor that matched nothing is a named failing sub-check unless the
author opted out; and the component score is the weighted fraction of the
constraints that passed.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_timeline, build_turn_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core.grading import trace_checks
from tolokaforge.core.grading.trace_checks import (
    _FAILURE_DETAIL,
    _HANDLERS,
    _VIEW_KINDS,
    evaluate_trace_checks,
    select_events,
)
from tolokaforge.core.grading.trace_timeline import TraceEventKind, TrialTimeline
from tolokaforge.core.models import (
    AdjacencyView,
    ToolCall,
    TraceChecksConfig,
    TraceMatcher,
)
from tolokaforge.runner.models import TRACE_CONSTRAINT_KINDS

pytestmark = pytest.mark.unit

_LOOKUP = "billing_api_get_payment"
_DENIAL = "servicenow_csm_update_case"


def _call_of(tool: str, **predicates: Any) -> dict[str, Any]:
    return {"kind": "tool_call", "tool": {"equals": tool}} | predicates


def _side(quantifier: str, tool: str, **predicates: Any) -> dict[str, Any]:
    return {"quantifier": quantifier, "match": _call_of(tool, **predicates)}


def _one_turn_timeline(*tools: str) -> TrialTimeline:
    """One assistant turn that made ``tools``, in that order, all of them recorded."""
    return build_timeline(
        turns=(("user", "Handle the refund."), ("assistant", "On it.")),
        recorded=[recorded_call(tool, sequence=index) for index, tool in enumerate(tools)],
    )


def test_adjacency_is_read_in_the_named_view_not_over_raw_positions():
    """The interleaved layout: two calls in one turn with a result between them.

    ``among: events`` is the reading that cannot express two consecutive calls, and
    it is the one a default would have picked. The event sequence is asserted
    kind by kind, so the proof pins what the timeline holds rather than only that
    two verdicts differ.
    """
    timeline = _one_turn_timeline(_LOOKUP, _DENIAL)

    assert [(event.position, event.kind) for event in timeline.events] == [
        (0, TraceEventKind.USER_MESSAGE),
        (1, TraceEventKind.ASSISTANT_MESSAGE),
        (2, TraceEventKind.TOOL_CALL),
        (3, TraceEventKind.TOOL_RESULT),
        (4, TraceEventKind.TOOL_CALL),
        (5, TraceEventKind.TOOL_RESULT),
    ]

    def adjacency(among: str) -> bool:
        return evaluate_constraint(
            timeline,
            {
                "immediately_before": {
                    "left": _side("any", _LOOKUP),
                    "right": _side("any", _DENIAL),
                    "among": among,
                }
            },
        ).passed

    assert adjacency("tool_calls") is True
    assert adjacency("events") is False


@dataclass(frozen=True)
class _KleeneCell:
    """One row of the three-valued table: what the constraint says at ``(d, u)``."""

    require: dict[str, Any]
    definite: int
    undecidable: int
    verdict: str


_REFUND = "issue_refund"
_REFUND_MATCH = {
    "kind": "tool_call",
    "tool": {"equals": _REFUND},
    "status": {"equals": "success"},
}

# The worked consequences of three-valued evaluation, one row per cell. ``present``
# passing at ``d >= 1`` with undecidables present is the over-fail guard: definite
# evidence already answers the question, so unreadable evidence cannot unanswer it.
_KLEENE_TABLE: dict[str, _KleeneCell] = {
    "present_with_a_definite_match": _KleeneCell(
        {"present": {"match": _REFUND_MATCH}}, 1, 0, "pass"
    ),
    "present_with_a_definite_match_and_an_undecidable": _KleeneCell(
        {"present": {"match": _REFUND_MATCH}}, 1, 1, "pass"
    ),
    "present_with_nothing_at_all": _KleeneCell({"present": {"match": _REFUND_MATCH}}, 0, 0, "fail"),
    "present_with_only_undecidables": _KleeneCell(
        {"present": {"match": _REFUND_MATCH}}, 0, 1, "undecided"
    ),
    "absent_with_a_definite_match": _KleeneCell({"absent": {"match": _REFUND_MATCH}}, 1, 0, "fail"),
    "absent_with_nothing_at_all": _KleeneCell({"absent": {"match": _REFUND_MATCH}}, 0, 0, "pass"),
    "absent_with_only_undecidables": _KleeneCell(
        {"absent": {"match": _REFUND_MATCH}}, 0, 1, "undecided"
    ),
    "count_at_most_zero_over_nothing": _KleeneCell(
        {"count": {"match": _REFUND_MATCH, "max": 0}}, 0, 0, "pass"
    ),
    "count_at_most_one_reachable_only_below_the_bound": _KleeneCell(
        {"count": {"match": _REFUND_MATCH, "max": 1}}, 0, 1, "pass"
    ),
    "count_at_most_one_definitely_above_the_bound": _KleeneCell(
        {"count": {"match": _REFUND_MATCH, "max": 1}}, 2, 0, "fail"
    ),
    "count_at_most_one_straddling_the_bound": _KleeneCell(
        {"count": {"match": _REFUND_MATCH, "max": 1}}, 1, 1, "undecided"
    ),
    "count_exactly_one_straddled_from_both_sides": _KleeneCell(
        {"count": {"match": _REFUND_MATCH, "min": 1, "max": 1}}, 0, 2, "undecided"
    ),
}


def _refund_timeline(definite: int, undecidable: int) -> TrialTimeline:
    """A trial that executed ``definite`` refunds and left ``undecidable`` unanswered."""
    return build_timeline(
        turns=(("user", "Refund it."), ("assistant", "Refunding.")),
        recorded=[recorded_call(_REFUND, sequence=index) for index in range(definite)],
        unexecuted=[
            ToolCall(id=f"never_ran_{index}", name=_REFUND, arguments={})
            for index in range(undecidable)
        ],
    )


@pytest.mark.parametrize("label", sorted(_KLEENE_TABLE))
def test_a_constraint_is_decided_only_where_every_completion_agrees(label: str):
    cell = _KLEENE_TABLE[label]
    timeline = _refund_timeline(cell.definite, cell.undecidable)
    calls = [event for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL]

    result = evaluate_constraint(timeline, cell.require)

    assert len(calls) == cell.definite + cell.undecidable
    assert [call.status for call in calls] == [None] * len(calls), (
        "a tool_call event carries no status of its own, so the row would prove nothing "
        "about the pairing if one did"
    )
    assert result.passed is (cell.verdict == "pass"), result.message
    assert ("cannot be decided" in result.message) is (cell.verdict == "undecided"), result.message
    if cell.verdict == "undecided":
        assert "status" in result.message
        assert result.kind in result.message


def test_the_three_valued_table_covers_every_kind_whose_verdict_is_a_match_count():
    """The rows span the three counting kinds and both sides of every boundary."""
    covered = {tuple(cell.require)[0] for cell in _KLEENE_TABLE.values()}
    assert covered == {"present", "absent", "count"}
    verdicts = {cell.verdict for cell in _KLEENE_TABLE.values()}
    assert verdicts == {"pass", "fail", "undecided"}


_SAID_IT = {"present": {"match": {"kind": "assistant_message", "text": {"contains": "Refunding"}}}}
_NEVER_HAPPENED = {"present": {"match": _call_of("audit_log_write")}}
_UNDECIDABLE = {"present": {"match": _REFUND_MATCH}}

# A composite is combined three-valued, so a branch nobody can decide is neither a
# branch that passed nor one that failed. The rows where the decided branch already
# settles the answer are the ones a two-valued fold would get wrong in the agent's
# favour.
_COMPOSITE_TABLE: dict[str, tuple[dict[str, Any], str]] = {
    "a_conjunction_whose_other_branch_holds": (
        {"all_of": [_SAID_IT, _UNDECIDABLE]},
        "undecided",
    ),
    "a_conjunction_whose_other_branch_fails": (
        {"all_of": [_NEVER_HAPPENED, _UNDECIDABLE]},
        "fail",
    ),
    "a_disjunction_whose_other_branch_holds": (
        {"any_of": [_SAID_IT, _UNDECIDABLE]},
        "pass",
    ),
    "a_disjunction_whose_other_branch_fails": (
        {"any_of": [_NEVER_HAPPENED, _UNDECIDABLE]},
        "undecided",
    ),
    "a_negation_of_something_undecidable": ({"negate": _UNDECIDABLE}, "undecided"),
    "a_negation_of_something_decided": ({"negate": _SAID_IT}, "fail"),
}


@pytest.mark.parametrize("label", sorted(_COMPOSITE_TABLE))
def test_a_composite_combines_its_branches_three_valued(label: str):
    require, verdict = _COMPOSITE_TABLE[label]
    timeline = _refund_timeline(0, 1)

    result = evaluate_constraint(timeline, require)

    assert result.passed is (verdict == "pass"), result.message
    assert ("cannot be decided" in result.message) is (verdict == "undecided"), result.message


def test_an_unmatched_side_fails_by_name_unless_the_author_opted_out():
    """The vacuity trap: a side that matched nothing never reaches ``all([]) is True``."""
    timeline = _one_turn_timeline(_LOOKUP)
    require = {"before": {"left": _side("all", _LOOKUP), "right": _side("all", _DENIAL)}}

    failing = evaluate_constraint(timeline, require)
    permitted = evaluate_constraint(timeline, require, on_missing="pass")

    assert select_events(timeline, TraceMatcher(**_call_of(_DENIAL))).matched == ()
    assert len(select_events(timeline, TraceMatcher(**_call_of(_LOOKUP))).matched) == 1
    assert failing.passed is False
    assert "right" in failing.message
    assert permitted.passed is True


def test_present_reads_its_own_match_as_the_anchor_on_missing_decides():
    """``present``'s anchor is the match itself, which is what the vocabulary says.

    So ``on_missing: pass`` makes a ``present`` constraint hold however the agent
    behaved — nothing matched is the one case it could have failed on. The
    vocabulary rejects ``on_missing`` on ``absent`` and ``count`` for the mirror
    reason and admits it here, so the field is honoured rather than silently
    ignored; the docs warn against writing it.
    """
    timeline = _one_turn_timeline(_LOOKUP)
    require = {"present": {"match": _call_of(_DENIAL)}}

    assert evaluate_constraint(timeline, require).passed is False
    assert evaluate_constraint(timeline, require, on_missing="pass").passed is True


def test_an_inverted_window_is_unmatched_rather_than_vacuously_satisfied():
    """The anchors did not occur in the declared order, so ``on_missing`` decides."""
    timeline = _one_turn_timeline(_DENIAL, _LOOKUP)
    require = {
        "absent_between": {
            "forbidden": _call_of("audit_log_write"),
            "start": _side("first", _LOOKUP),
            "end": _side("first", _DENIAL),
        }
    }

    assert evaluate_constraint(timeline, require).passed is False
    assert evaluate_constraint(timeline, require, on_missing="pass").passed is True


def test_a_turn_window_restricts_every_matcher_in_the_constraint():
    timeline = build_turn_timeline(
        [
            Turn("user", "Handle the refund."),
            Turn("assistant", "Looking it up.", recorded=[recorded_call(_LOOKUP, sequence=0)]),
            Turn("assistant", "Denying it.", recorded=[recorded_call(_DENIAL, sequence=1)]),
        ]
    )
    require = {"present": {"match": _call_of(_DENIAL)}}

    assert [event.turn_index for event in timeline.events] == [0, 0, 0, 0, 1, 1, 1]
    assert evaluate_constraint(timeline, require, within={"first_turn": 1}).passed is True
    assert evaluate_constraint(timeline, require, within={"last_turn": 0}).passed is False


def test_turn_zero_holds_the_opening_prompt_and_the_first_assistant_turn():
    """The measured quirk the docs state: ``first_turn: 0`` includes the user's prompt."""
    timeline = build_turn_timeline(
        [
            Turn("user", "Handle the refund."),
            Turn("assistant", "Looking it up.", recorded=[recorded_call(_LOOKUP, sequence=0)]),
        ]
    )
    window = {"first_turn": 0, "last_turn": 0}

    assert (
        evaluate_constraint(
            timeline,
            {"present": {"match": {"kind": "user_message", "text": {"contains": "refund"}}}},
            within=window,
        ).passed
        is True
    )
    assert (
        evaluate_constraint(
            timeline, {"present": {"match": _call_of(_LOOKUP)}}, within=window
        ).passed
        is True
    )


def test_the_clients_ordering_criterion_holds_only_in_the_right_order():
    """Criterion 1: the payment is looked up before the case is denied.

    The violating trial differs from the satisfying one in call order alone — same
    tools, same arguments — so nothing but the ordering can decide it.
    """
    require = {
        "before": {
            "left": _side("any", _LOOKUP, args={"payment_id": {"equals": "PAY-664306"}}),
            "right": _side(
                "first", _DENIAL, args={"u_resolution_code": {"equals": "denied_ineligible"}}
            ),
        }
    }
    lookup = recorded_call(_LOOKUP, sequence=0, arguments={"payment_id": "PAY-664306"})
    denial = recorded_call(
        _DENIAL, sequence=1, arguments={"u_resolution_code": "denied_ineligible"}
    )

    def verdict(calls: Sequence[Any]) -> tuple[list[str | None], bool]:
        timeline = build_timeline(
            turns=(("user", "Refund PAY-664306."), ("assistant", "Reviewing.")),
            recorded=calls,
        )
        made = [
            event.tool_name for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL
        ]
        return made, evaluate_constraint(timeline, require).passed

    assert verdict([lookup, denial]) == ([_LOOKUP, _DENIAL], True)
    assert verdict(
        [denial.model_copy(update={"sequence": 0}), lookup.model_copy(update={"sequence": 1})]
    ) == ([_DENIAL, _LOOKUP], False)


_AMOUNT = "The payment is $42.00."
_NO_PREFILL = {
    "all_of": [
        {
            "absent_before": {
                "forbidden": {"kind": "assistant_message", "text": {"contains": "$42.00"}},
                "anchor": {"quantifier": "first", "match": _call_of(_LOOKUP)},
            }
        },
        {"present": {"match": _call_of(_LOOKUP)}},
    ]
}


def _no_prefill_timeline(*, states_before_looking_up: bool, looks_up: bool) -> TrialTimeline:
    """Three turns: the ask, what the agent said and did, and its answer."""
    lookup = [recorded_call(_LOOKUP, sequence=0)] if looks_up else []
    first_reply = _AMOUNT if states_before_looking_up else "Let me look that up."
    return build_turn_timeline(
        [
            Turn("user", "How much was the payment?"),
            Turn("assistant", first_reply, recorded=lookup),
            Turn("assistant", _AMOUNT),
        ]
    )


def test_the_clients_no_prefill_conjunction_needs_both_branches():
    """``all_of`` over ``absent_before`` and ``present``, one branch at a time.

    The third trial violates exactly one branch, and the branches are evaluated
    on their own beside the conjunction, so "the conjunction failed" is pinned to
    the branch that failed rather than to the pair agreeing.
    """
    satisfying = _no_prefill_timeline(states_before_looking_up=False, looks_up=True)
    prefilled = _no_prefill_timeline(states_before_looking_up=True, looks_up=True)
    silent = _no_prefill_timeline(states_before_looking_up=False, looks_up=False)

    def branches(timeline: TrialTimeline) -> tuple[bool, bool]:
        return (
            evaluate_constraint(timeline, _NO_PREFILL["all_of"][0]).passed,
            evaluate_constraint(timeline, _NO_PREFILL["all_of"][1]).passed,
        )

    assert branches(satisfying) == (True, True)
    assert evaluate_constraint(satisfying, _NO_PREFILL).passed is True

    assert branches(prefilled) == (False, True)
    assert evaluate_constraint(prefilled, _NO_PREFILL).passed is False

    assert branches(silent) == (False, False)
    assert evaluate_constraint(silent, _NO_PREFILL).passed is False


def _fold(weights: Sequence[float] | None) -> float:
    """Three constraints, the middle one failing, scored under ``weights``."""
    timeline = _one_turn_timeline(_LOOKUP, _DENIAL)
    conditions = [
        {"present": {"match": _call_of(_LOOKUP)}},
        {"present": {"match": _call_of("audit_log_write")}},
        {"present": {"match": _call_of(_DENIAL)}},
    ]
    constraints: list[dict[str, Any]] = []
    for index, require in enumerate(conditions):
        constraint = {"id": f"c{index}", "description": "one condition", "require": require}
        if weights is not None:
            constraint["weight"] = weights[index]
        constraints.append(constraint)
    return evaluate_trace_checks(timeline, TraceChecksConfig(constraints=constraints)).score


def test_the_component_score_is_the_weighted_fraction_of_the_constraints_that_passed():
    assert _fold([1.0, 1.0, 2.0]) == 0.75
    assert _fold(None) == pytest.approx(2 / 3)


def test_a_trial_that_left_no_trace_of_itself_evaluates_nothing():
    timeline = build_timeline()
    config = TraceChecksConfig(
        constraints=[
            {"id": "c", "description": "d", "require": {"present": {"match": _call_of(_LOOKUP)}}}
        ]
    )

    result = evaluate_trace_checks(timeline, config)

    assert timeline.events == ()
    assert result.constraints == []
    assert result.passed is False
    assert result.score == 0.0


# One satisfying condition per constraint kind, written against a trial that looked
# a payment up and then denied the case. The keys are the vocabulary's own members,
# so a kind the evaluator cannot reach fails here rather than at an author's pack.
_SATISFIED_BY_KIND: dict[str, dict[str, Any]] = {
    "present": {"present": {"match": _call_of(_LOOKUP)}},
    "absent": {"absent": {"match": _call_of("audit_log_write")}},
    "count": {"count": {"match": {"kind": "tool_call"}, "min": 2, "max": 2}},
    "before": {"before": {"left": _side("any", _LOOKUP), "right": _side("any", _DENIAL)}},
    "immediately_before": {
        "immediately_before": {
            "left": _side("any", _LOOKUP),
            "right": _side("any", _DENIAL),
            "among": "tool_calls",
        }
    },
    "absent_before": {
        "absent_before": {
            "forbidden": _call_of(_DENIAL),
            "anchor": {"quantifier": "first", "match": _call_of(_LOOKUP)},
        }
    },
    "absent_between": {
        "absent_between": {
            "forbidden": _call_of("audit_log_write"),
            "start": {"quantifier": "first", "match": _call_of(_LOOKUP)},
            "end": {"quantifier": "last", "match": _call_of(_DENIAL)},
        }
    },
    "all_of": {
        "all_of": [
            {"present": {"match": _call_of(_LOOKUP)}},
            {"present": {"match": _call_of(_DENIAL)}},
        ]
    },
    "any_of": {
        "any_of": [
            {"present": {"match": _call_of("audit_log_write")}},
            {"present": {"match": _call_of(_DENIAL)}},
        ]
    },
    "negate": {"negate": {"present": {"match": _call_of("audit_log_write")}}},
}


def test_the_vocabulary_table_covers_every_declared_kind():
    """Three sources: this table, the written-out vocabulary, and the dispatch."""
    assert set(_SATISFIED_BY_KIND) == TRACE_CONSTRAINT_KINDS
    assert set(_HANDLERS) == TRACE_CONSTRAINT_KINDS
    assert set(_FAILURE_DETAIL) == TRACE_CONSTRAINT_KINDS


@pytest.mark.parametrize("kind", sorted(_SATISFIED_BY_KIND))
def test_every_kind_reaches_a_verdict_on_a_real_trial(kind: str):
    result = evaluate_constraint(_one_turn_timeline(_LOOKUP, _DENIAL), _SATISFIED_BY_KIND[kind])

    assert result.kind == kind
    assert result.passed is True, result.message


def test_every_adjacency_view_names_the_event_kinds_it_holds():
    assert set(_VIEW_KINDS) == set(AdjacencyView)
    assert _VIEW_KINDS[AdjacencyView.EVENTS] == frozenset(TraceEventKind)


def test_a_result_reports_the_positions_that_matched():
    result = evaluate_constraint(
        _one_turn_timeline(_LOOKUP, _DENIAL),
        {"before": {"left": _side("any", _LOOKUP), "right": _side("any", _DENIAL)}},
    )

    assert result.matched_positions == [2, 4]
    assert result.weight == 1.0
    assert result.message == ""


def _fold_function() -> ast.FunctionDef:
    source = Path(trace_checks.__file__).read_text()
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_weighted_fraction"
    ]
    assert len(functions) == 1, "the fold this audit reads is gone, so it would audit nothing"
    return functions[0]


def test_the_weighted_fold_divides_without_a_zero_denominator_branch():
    """``Σ(weight) > 0`` is a load-time invariant, so the fold may rely on it.

    A weight is rejected at load unless it is positive, which is what makes an
    all-zero weight set unreachable. Reintroducing ``if total == 0`` here would
    ship a score for that set that no author chose, so the branch is asserted
    absent rather than tested for.
    """
    fold = _fold_function()
    divisions = [
        node
        for node in ast.walk(fold)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    ]
    branches = [node for node in ast.walk(fold) if isinstance(node, (ast.If, ast.IfExp))]

    assert len(divisions) == 1
    assert isinstance(divisions[0].right, ast.Name)
    assert branches == [], (
        "the fold guards its denominator, which can only be defending against an "
        "all-zero weight set the load-time bound already rejects"
    )
