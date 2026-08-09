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
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_timeline, build_turn_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core.grading import trace_checks
from tolokaforge.core.grading.trace_checks import (
    _FAILURE_DETAIL,
    _HANDLERS,
    _VIEW_KINDS,
    _candidates,
    evaluate_trace_checks,
    select_events,
)
from tolokaforge.core.grading.trace_timeline import TraceEventKind, TrialTimeline
from tolokaforge.core.models import (
    AdjacencyView,
    OnMissing,
    ToolCall,
    ToolExecutionStatus,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraintResult,
    TraceConstraintSeverity,
    TraceMatcher,
)
from tolokaforge.runner.models import (
    _UNBINDABLE_FIELDS,
    TRACE_CONSTRAINT_KINDS,
    TRACE_MATCHABLE_FIELDS_BY_KIND,
    TraceConstraint,
)

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
    assert result.undecided is (cell.verdict == "undecided"), result.message
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
    assert result.undecided is (verdict == "undecided"), result.message
    assert ("cannot be decided" in result.message) is (verdict == "undecided"), result.message


def test_undecided_is_carried_as_a_field_and_is_neither_of_the_facts_beside_it():
    """Two ways to fail, told apart without reading prose — and told apart from both neighbours.

    ``passed=False`` alone cannot say whether the agent failed the constraint or
    whether nobody wrote down what it did, and a reader recovering that from
    ``message`` is matching on a sentence. Three timelines over the same declared
    refund pin the field against the two values it could be mistaken for: the
    executed-and-failed run fails decidably, so ``undecided`` is not ``not
    passed``; and the run that executed one call and left another unanswered is
    undecided *with* a record view, so it is not ``not records_present`` either.
    """
    turns = (("user", "Refund it."), ("assistant", "Refunding."))
    failed = recorded_call(_REFUND, sequence=0, status=ToolExecutionStatus.ERROR)
    declared = ToolCall(id=failed.call_id, name=_REFUND, arguments=failed.arguments)
    unanswered = ToolCall(id="never_ran", name=_REFUND, arguments={})

    executed = build_timeline(turns=turns, recorded=[failed])
    regraded = build_timeline(turns=turns, unexecuted=[declared])
    partly_executed = build_timeline(turns=turns, recorded=[failed], unexecuted=[unanswered])

    verdicts = {
        label: evaluate_constraint(timeline, {"present": {"match": _REFUND_MATCH}})
        for label, timeline in (
            ("executed", executed),
            ("regraded", regraded),
            ("partly_executed", partly_executed),
        )
    }

    assert [timeline.records_present for timeline in (executed, regraded, partly_executed)] == [
        True,
        False,
        True,
    ]
    assert {label: (item.passed, item.undecided) for label, item in verdicts.items()} == {
        "executed": (False, False),
        "regraded": (False, True),
        "partly_executed": (False, True),
    }


def test_an_undecided_pass_is_not_a_verdict_the_host_will_read():
    """The pair the field cannot form: no completion settles it, so none of them passes."""
    with pytest.raises(ValidationError, match="passed and is undecided at once"):
        TraceConstraintResult(
            id="the_refund_succeeded", kind="present", passed=True, weight=1.0, undecided=True
        )


def test_an_unmatched_side_fails_by_name_unless_the_author_opted_out():
    """The vacuity trap: a side that matched nothing never reaches ``all([]) is True``."""
    timeline = _one_turn_timeline(_LOOKUP)
    require = {"before": {"left": _side("all", _LOOKUP), "right": _side("all", _DENIAL)}}

    failing = evaluate_constraint(timeline, require)
    permitted = evaluate_constraint(timeline, require, on_missing="pass")

    assert select_events(timeline, TraceMatcher(**_call_of(_DENIAL)), {}).matched == ()
    assert len(select_events(timeline, TraceMatcher(**_call_of(_LOOKUP)), {}).matched) == 1
    assert failing.passed is False
    assert "right" in failing.message
    assert permitted.passed is True


@pytest.mark.parametrize(
    ("require", "passes"),
    [
        pytest.param({"all_of": [_NEVER_HAPPENED]}, False, id="all_of"),
        pytest.param({"any_of": [_NEVER_HAPPENED]}, False, id="any_of"),
        pytest.param({"negate": _NEVER_HAPPENED}, True, id="negate"),
    ],
)
def test_a_present_that_matched_nothing_is_false_whatever_policy_reaches_it(
    require: dict[str, Any], passes: bool
):
    """``present`` decides its own zero, so no policy can make the check unfailable.

    The load tier refuses ``on_missing`` over any tree holding a ``present``, so
    the only way ``pass`` reaches one is a constraint assembled past the validator
    — which is why the policy is set by assignment here rather than authored. Each
    composite threads it down unchanged and every one of them still reads "the call
    never happened": the two that report that verdict fail, and the one that
    complements it holds.
    """
    timeline = _one_turn_timeline(_LOOKUP)
    with pytest.raises(ValidationError):
        evaluate_constraint(timeline, require, on_missing="pass")

    config = TraceChecksConfig(
        constraints=[
            {"id": "constraint", "description": "the condition under test", "require": require}
        ]
    )
    config.constraints[0].on_missing = OnMissing.PASS
    verdict = evaluate_trace_checks(timeline, config).constraints[0]

    assert verdict.passed is passes
    assert verdict.undecided is False


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


def _fold_result(weights: Sequence[float] | None) -> TraceChecksResult:
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
    return evaluate_trace_checks(timeline, TraceChecksConfig(constraints=constraints))


def _fold(weights: Sequence[float] | None) -> float:
    return _fold_result(weights).score


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


# --------------------------------------------------------------------------
# Alternative routes and gates: the max-over-paths fold, and the degenerate
# shapes a parametrised sweep excludes by construction
# --------------------------------------------------------------------------

_FORBIDDEN = "servicenow_csm_write_status"


def _condition(constraint_id: str, tool: str, **fields: Any) -> dict[str, Any]:
    """A constraint that holds when the trial called ``tool``."""
    return {
        "id": constraint_id,
        "description": "one condition",
        "require": {"present": {"match": _call_of(tool)}},
        **fields,
    }


def _forbidding(constraint_id: str, tool: str) -> dict[str, Any]:
    """A gate that holds while the trial never called ``tool``."""
    return {
        "id": constraint_id,
        "description": "a check that must hold",
        "severity": "gate",
        "require": {"absent": {"match": _call_of(tool)}},
    }


def _route(path_id: str, *constraints: dict[str, Any]) -> dict[str, Any]:
    return {"id": path_id, "description": "one route", "constraints": list(constraints)}


def test_a_block_declaring_no_alternatives_scores_what_the_flat_fold_scores():
    """Boundary: zero routes. The shipped single-set fold, and no route grown around it.

    Both numbers are the ones the weighted-fraction lock above pins, so a fold that
    ran the route machinery over a synthesised default route could still reproduce
    them — the empty winner and the empty ``paths`` are what give it away.
    """
    weighted = _fold_result([1.0, 1.0, 2.0])
    unweighted = _fold_result(None)

    assert weighted.score == 0.75
    assert unweighted.score == pytest.approx(2 / 3)
    for result in (weighted, unweighted):
        assert result.winning_path == ""
        assert result.paths == []
        assert result.gate_failed is False
        assert result.failed_gate_ids == []
        assert [item.severity for item in result.constraints] == [
            TraceConstraintSeverity.SCORED
        ] * 3


def test_a_route_of_nothing_but_gates_scores_the_gates_own_verdict():
    """Boundary: a route whose non-gate set is empty, beside no shared scored check.

    There is no weighted average to take, so the score is the gates' verdict. A build
    routing the empty set through the weighted fraction divides by the empty sum and
    raises; one that answers it with a guard *inside* that function reddens the AST
    lock above instead.
    """
    config = TraceChecksConfig(
        alternatives=[
            _route("by_lookup", _condition("g_lookup", _LOOKUP, severity="gate")),
            _route("by_denial", _condition("g_denial", _DENIAL, severity="gate")),
        ]
    )

    one_open = evaluate_trace_checks(_one_turn_timeline(_LOOKUP), config)
    both_shut = evaluate_trace_checks(_one_turn_timeline("audit_log_write"), config)

    assert [(item.id, item.score, item.gate_failed) for item in one_open.paths] == [
        ("by_lookup", 1.0, False),
        ("by_denial", 0.0, True),
    ]
    assert (one_open.score, one_open.winning_path, one_open.gate_failed) == (
        1.0,
        "by_lookup",
        False,
    )
    assert (both_shut.score, both_shut.winning_path, both_shut.gate_failed) == (
        0.0,
        "by_lookup",
        True,
    )
    assert both_shut.failed_gate_ids == ["g_lookup"]


def test_a_flat_block_of_nothing_but_gates_is_defined_and_grows_no_route():
    """Boundary: ``alternatives`` absent and every shared constraint a gate.

    The collapse is not conditional on a pack declaring alternatives. Gating it on
    ``alternatives is not None`` is what leaves these two cases dividing by the empty
    sum while every route-shaped test above stays green.
    """
    config = TraceChecksConfig(
        constraints=[
            _condition("z_the_payment_was_looked_up", _LOOKUP, severity="gate"),
            _condition("a_the_case_was_denied", _DENIAL, severity="gate"),
        ]
    )

    both_open = evaluate_trace_checks(_one_turn_timeline(_LOOKUP, _DENIAL), config)
    both_shut = evaluate_trace_checks(_one_turn_timeline("audit_log_write"), config)

    assert (both_open.score, both_open.gate_failed, both_open.failed_gate_ids) == (1.0, False, [])
    assert (both_shut.score, both_shut.gate_failed) == (0.0, True)
    assert both_shut.failed_gate_ids == [
        "z_the_payment_was_looked_up",
        "a_the_case_was_denied",
    ], "the tripped gates are named in declaration order, not sorted"
    for result in (both_open, both_shut):
        assert result.winning_path == ""
        assert result.paths == []


def test_every_route_being_undecidable_scores_nothing_and_names_the_evidence():
    """Boundary: no route reaches a verdict, because the record answers no call.

    The winner is still the first-declared, so the grade names a route rather than
    leaving the reader to guess which set the empty score came from.
    """
    undecidable = {"present": {"match": _REFUND_MATCH}}
    config = TraceChecksConfig(
        alternatives=[
            _route(
                "by_refund", {"id": "r1", "description": "one condition", "require": undecidable}
            ),
            _route(
                "by_reversal", {"id": "r2", "description": "one condition", "require": undecidable}
            ),
        ]
    )

    result = evaluate_trace_checks(_refund_timeline(0, 1), config)

    assert [item.score for item in result.paths] == [0.0, 0.0]
    assert result.score == 0.0
    assert result.winning_path == "by_refund"
    assert result.gate_failed is False
    assert [item.id for item in result.constraints] == ["r1"]
    assert "cannot be decided" in result.constraints[0].message
    assert "no status at position" in result.constraints[0].message


def test_a_gate_no_completion_of_the_record_can_decide_trips_the_trial():
    """R2: undecided is not a pass in the agent's favour, and a gate is no exception.

    The scored check passes, so an undecidable gate that opened would score the trial
    ``1.0`` — a silent pass on the one check the author said must hold, and a gate
    weaker than the scored constraint it replaced.
    """
    config = TraceChecksConfig(
        constraints=[
            {
                "id": "the_refund_went_through",
                "description": "a check that must hold",
                "severity": "gate",
                "require": {"present": {"match": _REFUND_MATCH}},
            },
            {"id": "the_agent_said_so", "description": "one condition", "require": _SAID_IT},
        ]
    )

    result = evaluate_trace_checks(_refund_timeline(0, 1), config)

    assert [item.passed for item in result.constraints] == [False, True]
    assert "cannot be decided" in result.constraints[0].message
    assert result.gate_failed is True
    assert result.failed_gate_ids == ["the_refund_went_through"]
    assert result.score == 0.0


def test_routes_scoring_the_same_are_won_by_the_first_declared():
    """Boundary: a tie at a non-zero score, broken by declaration order.

    Three routes, because two cannot separate the two ways an id-ordered tie-break
    is written: the winner is declared first and is neither the lexicographic
    largest nor the smallest, so a max over ``(score, id)`` picks ``z_third_route``
    and a sort by id before the max picks ``a_second_route``. Both name a route the
    assertion does not.
    """
    config = TraceChecksConfig(
        alternatives=[
            _route("m_first_route", _condition("m1", _LOOKUP), _condition("m2", "audit_log_write")),
            _route(
                "a_second_route", _condition("a1", _LOOKUP), _condition("a2", "audit_log_write")
            ),
            _route("z_third_route", _condition("z1", _LOOKUP), _condition("z2", "audit_log_write")),
        ]
    )

    result = evaluate_trace_checks(_one_turn_timeline(_LOOKUP), config)

    assert [item.score for item in result.paths] == [0.5, 0.5, 0.5]
    assert result.score == 0.5
    assert result.winning_path == "m_first_route"
    assert [item.id for item in result.constraints] == ["m1", "m2"]


_TWO_ROUTES = TraceChecksConfig(
    alternatives=[
        _route("route_a", _condition("a1", "a_first"), _condition("a2", "a_second")),
        _route("route_b", _condition("b1", "b_first"), _condition("b2", "b_second")),
    ]
)


def test_a_route_walked_in_full_beats_half_of_each_route():
    """The issue's criterion: cherry-picking one step of each route scores neither.

    The comparison is against both full-route runs rather than a literal, so it is
    the ordering that is pinned and not this fixture's arithmetic.
    """
    by_a = evaluate_trace_checks(_one_turn_timeline("a_first", "a_second"), _TWO_ROUTES)
    by_b = evaluate_trace_checks(_one_turn_timeline("b_first", "b_second"), _TWO_ROUTES)
    cherry_picked = evaluate_trace_checks(_one_turn_timeline("a_first", "b_second"), _TWO_ROUTES)

    assert (by_a.score, by_a.winning_path) == (1.0, "route_a")
    assert (by_b.score, by_b.winning_path) == (1.0, "route_b")
    assert cherry_picked.score < by_a.score
    assert cherry_picked.score < by_b.score


def test_the_shared_constraints_are_folded_into_every_route_before_the_max():
    """Authored so normalising over a route's own constraints alone picks the loser.

    The short route is perfect on its own checks and the long one is not, yet the
    heavy shared check both carry — and both failed — leaves the long route ahead
    once each is normalised over the whole set it was scored on.
    """
    config = TraceChecksConfig(
        constraints=[_condition("the_case_was_denied", _DENIAL, weight=3.0)],
        alternatives=[
            _route("shorter_route", _condition("a1", "a_first")),
            _route(
                "longer_route",
                _condition("b1", "b_first"),
                _condition("b2", "b_second"),
                _condition("b3", "b_third"),
            ),
        ],
    )

    result = evaluate_trace_checks(_one_turn_timeline("a_first", "b_first", "b_second"), config)

    assert [(item.id, item.score) for item in result.paths] == [
        ("shorter_route", 0.25),
        ("longer_route", pytest.approx(1 / 3)),
    ]
    assert result.winning_path == "longer_route"
    assert [item.id for item in result.constraints] == ["the_case_was_denied", "b1", "b2", "b3"]


def test_a_gate_that_holds_contributes_nothing_to_the_weighted_average():
    """Three checks, one a gate that passes; the score is over the other two alone.

    Counting the gate in the fold moves the score from ``1/2`` to ``2/3`` — it would
    then be scored as well as gating, which is the double-counting severity exists
    to avoid.
    """
    config = TraceChecksConfig(
        constraints=[
            _condition("the_payment_was_looked_up", _LOOKUP, severity="gate"),
            _condition("the_case_was_denied", _DENIAL),
            _condition("the_action_was_logged", "audit_log_write"),
        ]
    )

    result = evaluate_trace_checks(_one_turn_timeline(_LOOKUP, _DENIAL), config)

    assert [item.passed for item in result.constraints] == [True, True, False]
    assert result.score == 0.5
    assert result.gate_failed is False


def test_a_gate_on_the_winning_route_fails_the_trial_rather_than_falling_back():
    """R3, and the reason the argmax runs over every route including gated ones.

    The gated route scores highest, so it wins and its gate shuts the trial. Dropping
    a route whose gate failed from contention would hand the win to the clean route
    below it and pass an agent that did the forbidden thing.
    """
    config = TraceChecksConfig(
        alternatives=[
            _route(
                "route_a",
                _forbidding("no_status_was_written", _FORBIDDEN),
                _condition("a1", "a_first"),
            ),
            _route("route_b", _condition("b1", "b_first"), _condition("b2", "b_second")),
        ]
    )

    result = evaluate_trace_checks(_one_turn_timeline("a_first", "b_first", _FORBIDDEN), config)

    assert [(item.id, item.score, item.gate_failed) for item in result.paths] == [
        ("route_a", 1.0, True),
        ("route_b", 0.5, False),
    ]
    assert result.winning_path == "route_a"
    assert result.score == 0.0
    assert result.gate_failed is True
    assert result.failed_gate_ids == ["no_status_was_written"]


def test_a_gate_on_the_route_that_lost_leaves_the_trial_passing():
    """R3 stated honestly, and the escape it does not close, pinned as it stands.

    A path gate is a **process** gate: it constrains how *that* route must be walked
    and is consulted only on the route the agent took. The consequence is an escape —
    trip route A's gate and sandbag A so a clean route B outscores it, and the trial
    passes with the forbidden action performed. It is asserted rather than fixed:
    no rule for path gates is hole-free, and a future change to the argmax must not
    move this silently.

    **A gate that must hold whatever route the agent took belongs in shared
    ``constraints``**, where it is in every decision set and has no escape. See
    ``docs/GRADING.md`` § "Shared gates and path gates: when each is appropriate".
    """
    config = TraceChecksConfig(
        alternatives=[
            _route(
                "route_a",
                _forbidding("no_status_was_written", _FORBIDDEN),
                _condition("a1", "a_first"),
            ),
            _route("route_b", _condition("b1", "b_first")),
        ]
    )

    result = evaluate_trace_checks(_one_turn_timeline("b_first", _FORBIDDEN), config)

    assert [(item.id, item.score, item.gate_failed) for item in result.paths] == [
        ("route_a", 0.0, True),
        ("route_b", 1.0, False),
    ]
    assert result.winning_path == "route_b"
    assert result.score == 1.0
    assert result.gate_failed is False
    assert result.failed_gate_ids == []


def test_a_tie_is_won_by_the_route_whose_gate_shut_whichever_order_they_are_written():
    """The tied cell of the argmax, driven at both authoring orders.

    Broken by declaration order alone, this cell decides the trial by where in the
    file the two routes were written: the gated route first and its gate fails the
    trial, the gated route second and a clean sibling of equal score carries it.
    The gate-failing route therefore wins a tie, which can only ever shut a
    component and never rescue one — so it does not reopen the escape the
    argmax-over-every-route rule closes.
    """
    gated = _route(
        "route_a", _forbidding("no_status_was_written", _FORBIDDEN), _condition("a1", "a_first")
    )
    clean = _route("route_b", _condition("b1", "b_first"))
    timeline = _one_turn_timeline("a_first", "b_first", _FORBIDDEN)

    gated_first = evaluate_trace_checks(timeline, TraceChecksConfig(alternatives=[gated, clean]))
    gated_second = evaluate_trace_checks(timeline, TraceChecksConfig(alternatives=[clean, gated]))

    assert [item.score for item in gated_first.paths] == [1.0, 1.0], (
        "the two routes do not tie on this trajectory, so the tie-break is not what "
        "the assertions below measure"
    )
    for result in (gated_first, gated_second):
        assert result.winning_path == "route_a"
        assert (result.score, result.gate_failed) == (0.0, True)
        assert result.failed_gate_ids == ["no_status_was_written"]


def test_a_shared_gate_applies_whichever_route_won():
    """Driven on both routes, so it cannot pass by the gate sitting in the only set."""
    config = TraceChecksConfig(
        constraints=[_forbidding("no_status_was_written", _FORBIDDEN)],
        alternatives=[
            _route("route_a", _condition("a1", "a_first")),
            _route("route_b", _condition("b1", "b_first")),
        ],
    )

    by_a = evaluate_trace_checks(_one_turn_timeline("a_first", _FORBIDDEN), config)
    by_b = evaluate_trace_checks(_one_turn_timeline("b_first", _FORBIDDEN), config)
    clean = evaluate_trace_checks(_one_turn_timeline("b_first"), config)

    assert (by_a.winning_path, by_b.winning_path) == ("route_a", "route_b")
    for result in (by_a, by_b):
        assert result.score == 0.0
        assert result.gate_failed is True
        assert result.failed_gate_ids == ["no_status_was_written"]
    assert (clean.winning_path, clean.score, clean.gate_failed) == ("route_b", 1.0, False)


# --------------------------------------------------------------------------
# Correlating one argument against another
# --------------------------------------------------------------------------

_READ = "read_record"
_WRITE = "write_record"


def _record_timeline(*calls: tuple[str, Any]) -> TrialTimeline:
    """One assistant turn calling ``(tool, record_id)`` in order, all recorded."""
    return build_timeline(
        turns=(("user", "Update the records."), ("assistant", "On it.")),
        recorded=[
            recorded_call(tool, sequence=index, arguments={"record_id": record})
            for index, (tool, record) in enumerate(calls)
        ],
    )


def _read_before_write(**bind_fields: Any) -> dict[str, Any]:
    """Every record the agent wrote, it read first — the correlation, not a literal."""
    return {
        "bind": {
            "match": _call_of(_WRITE),
            "values": {"rec": {"field": "args.record_id"}},
            **bind_fields,
        },
        "require": {
            "before": {
                "left": _side("any", _READ, args={"record_id": {"equals_binding": "rec"}}),
                "right": _side("any", _WRITE, args={"record_id": {"equals_binding": "rec"}}),
            }
        },
    }


def test_a_correlated_ordering_reads_the_record_the_agent_wrote():
    """AC1: the same two constraints, decided by which record the write names."""
    correlated = _read_before_write()

    matching = evaluate_constraint(
        _record_timeline((_READ, "X"), (_WRITE, "X")),
        correlated["require"],
        bind=correlated["bind"],
    )
    crossed = evaluate_constraint(
        _record_timeline((_READ, "X"), (_WRITE, "Y")),
        correlated["require"],
        bind=correlated["bind"],
    )

    assert matching.passed is True, matching.message
    assert crossed.passed is False
    assert crossed.matched_positions == [4], "the write is selected; the read of X is not"


@pytest.mark.parametrize(
    ("bound", "other"), [("REC-1", "REC-2"), (42, 43)], ids=["string_record", "integer_record"]
)
def test_one_candidate_scores_exactly_as_the_value_written_out(bound: Any, other: Any):
    """A reference substitutes; it does not reinterpret — the one-candidate boundary.

    Driven over an integer record as well as a string one, because the whole content
    of "substitutes" is that the bound value reaches ``operator.eq`` as itself — a
    reference stringifying its value would still decide the string row correctly.

    The verdict, the weight and the matched positions are the literal's exactly. The
    message is the literal's plus the assignment it failed under, which the fold
    names at every cardinality including this one: suppressing it here would be a
    special case for ``n=1`` inside the fold, and the value is the same thing an
    author needs to read whether one record failed to correlate or four did.
    """
    correlated = _read_before_write()
    literal = {
        "before": {
            "left": _side("any", _READ, args={"record_id": {"equals": bound}}),
            "right": _side("any", _WRITE, args={"record_id": {"equals": bound}}),
        }
    }

    for label, timeline, naming in [
        ("correlated", _record_timeline((_READ, bound), (_WRITE, bound)), ""),
        (
            "crossed",
            _record_timeline((_READ, other), (_WRITE, bound)),
            f"; failed under (rec={bound!r})",
        ),
    ]:
        by_binding = evaluate_constraint(timeline, correlated["require"], bind=correlated["bind"])
        by_literal = evaluate_constraint(timeline, literal)
        assert (
            by_binding.passed,
            by_binding.weight,
            by_binding.message,
            by_binding.matched_positions,
        ) == (
            by_literal.passed,
            by_literal.weight,
            by_literal.message + naming,
            by_literal.matched_positions,
        ), f"the {label} trajectory scores differently bound than written out"


def test_a_binder_that_selects_nothing_fails_by_name_unless_the_author_opted_out():
    """The universal reading is vacuous over no candidates, so ``on_unbound`` decides."""
    read_only = _record_timeline((_READ, "X"))
    failing = _read_before_write()
    permitted = _read_before_write(on_unbound="pass")

    unbound = evaluate_constraint(read_only, failing["require"], bind=failing["bind"])
    opted_out = evaluate_constraint(read_only, permitted["require"], bind=permitted["bind"])

    assert unbound.passed is False
    assert "the binding selected no event" in unbound.message
    assert opted_out.passed is True
    assert opted_out.message == ""


def test_many_binder_events_carrying_one_value_are_one_candidate():
    """Candidates are distinct values, not events — the many-identical boundary.

    Asserted against the one-write trajectory rather than against a literal count,
    so the lock is that three repeats of a value decide and *report* exactly as one
    occurrence of it: an author reading a grade must not have to work out that the
    three names in front of them are the same record written three times.
    """
    correlated = _read_before_write()
    once = evaluate_constraint(
        _record_timeline((_WRITE, "X")), correlated["require"], bind=correlated["bind"]
    )
    three_times = evaluate_constraint(
        _record_timeline((_WRITE, "X"), (_WRITE, "X"), (_WRITE, "X")),
        correlated["require"],
        bind=correlated["bind"],
    )

    assert (three_times.passed, three_times.message) == (once.passed, once.message)
    assert three_times.message.count("(rec=") == 1, three_times.message
    assert three_times.matched_positions == [2, 4, 6], "the union over the one candidate's reading"


def test_one_uncorrelated_record_among_several_fails_and_the_message_names_only_it():
    """The universal fold — the many-distinct boundary, one candidate failing.

    ``Z`` was written and never read, so the constraint fails however well the agent
    handled ``X``. An existential fold passes this trajectory on ``X`` alone, and a
    first-match fold answers on whichever candidate the binder happened to yield
    first.
    """
    result = evaluate_constraint(
        _record_timeline((_READ, "X"), (_READ, "Y"), (_WRITE, "X"), (_WRITE, "Z")),
        _read_before_write()["require"],
        bind=_read_before_write()["bind"],
    )

    assert result.passed is False
    assert "failed under (rec='Z')" in result.message
    assert "'X'" not in result.message, "the correlated record is not what failed"
    assert result.matched_positions == [2, 6, 8], "the read and write of X, and the write of Z"


def test_the_verdict_does_not_turn_on_the_order_the_binder_yields_candidates():
    """The property that rejected first-match: two reads the constraint says nothing about.

    The same two records read in either order, one of them written. Binding at the
    read makes both reads candidates, and ``Y`` was never written under either
    ordering — so a rule reading only the first candidate calls the same set of
    actions correlated in one order and uncorrelated in the other.
    """
    read_bound = {
        "bind": {"match": _call_of(_READ), "values": {"rec": {"field": "args.record_id"}}},
        "require": _read_before_write()["require"],
    }

    x_first = evaluate_constraint(
        _record_timeline((_READ, "X"), (_READ, "Y"), (_WRITE, "X")),
        read_bound["require"],
        bind=read_bound["bind"],
    )
    y_first = evaluate_constraint(
        _record_timeline((_READ, "Y"), (_READ, "X"), (_WRITE, "X")),
        read_bound["require"],
        bind=read_bound["bind"],
    )

    assert (x_first.passed, x_first.message) == (y_first.passed, y_first.message)
    assert x_first.passed is False, "Y was read and never written, whichever read came first"
    assert "failed under (rec='Y')" in x_first.message


def test_a_negated_bound_constraint_reads_no_candidate_satisfies_it():
    """Quantification is outermost, so ``negate`` is ``∀v ¬P(v)`` and not ``¬∀v P(v)``.

    Driven against both trajectories that separate the two readings: exactly one
    candidate satisfying the negated expression fails the constraint, where ``¬∀``
    would pass it on the other candidate not satisfying it.
    """
    forbidden_read = {
        "bind": {"match": _call_of(_WRITE), "values": {"rec": {"field": "args.record_id"}}},
        "require": {
            "negate": {
                "present": {"match": _call_of(_READ, args={"record_id": {"equals_binding": "rec"}})}
            }
        },
    }

    one_satisfies = evaluate_constraint(
        _record_timeline((_READ, "X"), (_WRITE, "X"), (_WRITE, "Y")),
        forbidden_read["require"],
        bind=forbidden_read["bind"],
    )
    none_satisfies = evaluate_constraint(
        _record_timeline((_READ, "Z"), (_WRITE, "X"), (_WRITE, "Y")),
        forbidden_read["require"],
        bind=forbidden_read["bind"],
    )

    assert one_satisfies.passed is False
    assert "failed under (rec='X')" in one_satisfies.message
    assert none_satisfies.passed is True, "no candidate satisfies it, which is what it asks"


def test_a_turn_window_restricts_the_binder_and_so_the_candidate_set():
    """``within`` reaches the binder because it resolves through the same resolver.

    The window changes the verdict here by changing which records are candidates at
    all: ``Y`` is written in the excluded turn, and it is the only reason the
    unwindowed constraint fails.
    """
    timeline = build_turn_timeline(
        [
            Turn("user", "Update the records."),
            Turn(
                "assistant",
                "Reading and writing X.",
                recorded=[
                    recorded_call(_READ, sequence=0, arguments={"record_id": "X"}),
                    recorded_call(_WRITE, sequence=1, arguments={"record_id": "X"}),
                ],
            ),
            Turn(
                "assistant",
                "Writing Y as well.",
                recorded=[recorded_call(_WRITE, sequence=2, arguments={"record_id": "Y"})],
            ),
        ]
    )
    correlated = _read_before_write()

    unwindowed = evaluate_constraint(timeline, correlated["require"], bind=correlated["bind"])
    windowed = evaluate_constraint(
        timeline, correlated["require"], bind=correlated["bind"], within={"last_turn": 0}
    )

    assert [event.turn_index for event in timeline.events] == [0, 0, 0, 0, 0, 0, 1, 1, 1]
    assert unwindowed.passed is False
    assert "failed under (rec='Y')" in unwindowed.message
    assert windowed.passed is True, "the write of Y is outside the window, so it binds nothing"


_INTEGER_DELIVERY_BINDING = {
    "match": _call_of("open_delivery"),
    "values": {"delivery": {"field": "args.delivery_id"}},
}
"""A binding whose value is an ``int``, referenced below from a text field."""


def _integer_delivery_timeline() -> TrialTimeline:
    return build_timeline(
        turns=(("user", "Open it."), ("assistant", "I opened delivery 4021 for you.")),
        recorded=[recorded_call("open_delivery", arguments={"delivery_id": 4021})],
    )


def _assert_names_the_unmakeable_comparison(message: str, operator: str) -> None:
    assert "the text comparison was not made" in message
    assert "'delivery'" in message
    assert "4021" in message
    assert "of type int" in message
    assert f"{operator} reads a text field as text" in message
    assert "args predicate" in message
    assert "regex capture" in message


def test_a_type_mismatched_contains_binding_says_the_comparison_was_not_made():
    """``contains`` falls back to equality off the string pair, so this is never true.

    The verdict is ``False`` either way, which is why the assertion is on the
    message: a bare fold would be indistinguishable from the agent having failed,
    and the author would read their own type mistake as the agent's.
    """
    require = {
        "present": {
            "match": {"kind": "assistant_message", "text": {"contains_binding": "delivery"}}
        }
    }

    result = evaluate_constraint(
        _integer_delivery_timeline(), require, bind=_INTEGER_DELIVERY_BINDING
    )

    assert result.passed is False
    _assert_names_the_unmakeable_comparison(result.message, "contains_binding")


def test_a_type_mismatched_equals_binding_says_the_comparison_was_not_made():
    """``eq`` over a string and an int is false outright, so this is never true either.

    The gate rejects this shape wherever the tool's schema types the extraction, and
    the residue it cannot type — no schema resolved, a path below its first segment, a
    property the schema gives no single type — is exactly what this backstop covers.
    It reads the value the predicate holds, so it answers only where that value is
    text, which is why a text field is where it can be provoked.
    """
    require = {
        "present": {"match": {"kind": "assistant_message", "text": {"equals_binding": "delivery"}}}
    }

    result = evaluate_constraint(
        _integer_delivery_timeline(), require, bind=_INTEGER_DELIVERY_BINDING
    )

    assert result.passed is False
    _assert_names_the_unmakeable_comparison(result.message, "equals_binding")


# Every extraction the load rules admit, and the value it reads off the trajectory
# below. A second source for the extraction table: the load rule admits a head
# segment off ``TRACE_MATCHABLE_FIELDS_BY_KIND`` while the evaluator dispatches it
# off its own map, so a field admitted by one and unknown to the other is a
# ``KeyError`` at grade time — and the expected values here are what stops a
# dispatch wired to the wrong attribute from passing that check.
_EVERY_BINDABLE_EXTRACTION = (
    pytest.param("tool_call", "tool", "get_payment", id="a_call_names_its_tool"),
    pytest.param("tool_call", "args", {"payment_id": "4021"}, id="a_bare_args_binds_the_object"),
    pytest.param("tool_call", "args.payment_id", "4021", id="a_dotted_path_binds_the_argument"),
    pytest.param("tool_call", "result", "paid in full", id="a_call_reads_its_paired_outcome"),
    pytest.param("tool_result", "tool", "get_payment", id="an_outcome_names_its_tool"),
    pytest.param("tool_result", "result", "paid in full", id="an_outcome_reads_itself"),
    pytest.param("assistant_message", "text", "reading it", id="an_assistant_turn_reads_its_text"),
    pytest.param("user_message", "text", "look up payment 4021", id="a_user_turn_reads_its_text"),
)


def _extraction_probe() -> TrialTimeline:
    """One trajectory carrying a distinct value for every bindable field."""
    return build_timeline(
        turns=(("user", "look up payment 4021"), ("assistant", "reading it")),
        recorded=[
            recorded_call("get_payment", arguments={"payment_id": "4021"}, output="paid in full")
        ],
    )


def test_the_extraction_cells_span_every_field_a_binder_may_read() -> None:
    """The sweep below reads a hand-written table, so a field missing from it is unswept.

    Bare ``args`` and a dotted path under it are one admissible head segment written
    twice, because the evaluator answers them on different branches.
    """
    swept = {
        (kind, field.partition(".")[0])
        for kind, field, _ in (param.values for param in _EVERY_BINDABLE_EXTRACTION)
    }

    assert swept == {
        (kind.value, field)
        for kind, fields in TRACE_MATCHABLE_FIELDS_BY_KIND.items()
        for field in fields - _UNBINDABLE_FIELDS
    }


@pytest.mark.parametrize(("kind", "field", "expected"), _EVERY_BINDABLE_EXTRACTION)
def test_a_binder_may_extract_every_field_its_kind_carries(
    kind: str, field: str, expected: object
) -> None:
    """The rejection is scoped to the fields a kind lacks, not to the ones it has."""
    match: dict[str, Any] = {"kind": kind}
    if field == "result":
        match["status"] = {"equals": "success"}
    constraint = TraceConstraint.model_validate(
        {
            "id": "probe",
            "description": "one extraction, read off the event kind that carries it",
            "bind": {"match": match, "values": {"read": {"field": field}}},
            "require": {
                "present": {"match": _call_of("audit", args={"seen": {"equals_binding": "read"}})}
            },
        }
    )

    assert _candidates(_extraction_probe(), constraint).definite == [{"read": expected}]


# --------------------------------------------------------------------------
# The candidate set is itself three-valued
# --------------------------------------------------------------------------

# The binder selects successful writes, so a write the trial recorded no outcome
# for is an event whose membership of the candidate set the trial cannot settle.
_BOUND_PRESENT = {
    "present": {
        "match": _call_of(
            _READ, status={"equals": "success"}, args={"record_id": {"equals_binding": "rec"}}
        )
    }
}
_SETTLED_BINDER = {
    "match": _call_of(_WRITE, status={"equals": "success"}),
    "values": {"rec": {"field": "args.record_id"}},
}


def _candidate_cell(definite: Sequence[str], undecidable: Sequence[str]) -> TrialTimeline:
    """A trajectory binding one candidate per verdict asked for, each with that verdict.

    A write makes a candidate — recorded for a definite one, unexecuted for an
    undecidable one, since the binder reads a status the trial then does not carry.
    A read of the same record decides that candidate's ``present``: recorded is a
    definite match, unexecuted is undecidable, and none at all is a definite miss.
    """
    sequence = itertools.count()
    recorded = [
        recorded_call(_WRITE, sequence=next(sequence), arguments={"record_id": f"D{index}"})
        for index in range(len(definite))
    ]
    unexecuted = [
        ToolCall(id=f"unrun_write_{index}", name=_WRITE, arguments={"record_id": f"U{index}"})
        for index in range(len(undecidable))
    ]
    for prefix, verdicts in (("D", definite), ("U", undecidable)):
        for index, verdict in enumerate(verdicts):
            record = f"{prefix}{index}"
            if verdict == "pass":
                recorded.append(
                    recorded_call(_READ, sequence=next(sequence), arguments={"record_id": record})
                )
            elif verdict == "undecided":
                unexecuted.append(
                    ToolCall(id=f"unrun_read_{record}", name=_READ, arguments={"record_id": record})
                )
    return build_timeline(
        turns=(("user", "Update the records."), ("assistant", "Working.")),
        recorded=recorded,
        unexecuted=unexecuted,
    )


def _cell_verdict(definite: Sequence[str], undecidable: Sequence[str], on_unbound: str) -> str:
    """What the shipped evaluator decides on one cell, as one of the three verdicts."""
    result = evaluate_constraint(
        _candidate_cell(definite, undecidable),
        _BOUND_PRESENT,
        bind=_SETTLED_BINDER | {"on_unbound": on_unbound},
    )
    if result.passed:
        return "pass"
    return "undecided" if "cannot be decided" in result.message else "fail"


_KLEENE_ORDER = ("fail", "undecided", "pass")


def _every_completion(definite: Sequence[str], undecidable: Sequence[str], on_unbound: str) -> str:
    """The brute force: the verdict over `D ∪ S` for **every** `S ⊆ U`, or undecided.

    Kleene AND over a set is its minimum under ``fail < undecided < pass``, and the
    empty set is the vacuous truth ``on_unbound`` overrides. Written out over all
    ``2**|U|`` subsets rather than over any shortcut, because the shortcut is what
    is under test.
    """

    def conjunction(members: Sequence[str]) -> str:
        return min(members, key=_KLEENE_ORDER.index) if members else "pass"

    unbound = "pass" if on_unbound == "pass" else "fail"
    readings = set()
    for size in range(len(undecidable) + 1):
        for chosen in itertools.combinations(range(len(undecidable)), size):
            members = [*definite, *(undecidable[index] for index in chosen)]
            readings.add(conjunction(members) if members else unbound)
    return readings.pop() if len(readings) == 1 else "undecided"


def test_the_candidate_set_is_decided_over_every_completion_and_not_its_two_ends():
    """The reading enumeration agrees with brute force over every subset of ``U``.

    ``D ∪ {u}`` per undecidable ``u`` is not redundant beside ``D`` and ``D ∪ U``:
    with ``D`` empty the empty reading is ``on_unbound`` rather than a vacuous pass,
    so both ends can read ``fail`` where a completion binding one satisfied candidate
    holds. Comparing the two ends alone reports a definite failure there, on evidence
    the trial does not carry — the over-fail ``_reachable_counts`` guards against one
    level down, met again over the candidate set.

    Driven against the real evaluator over a real trajectory per cell: the verdict
    each candidate reaches is supplied as recorded and unexecuted calls rather than
    stubbed, so the agreement is between two readings of the shipped fold.
    """
    verdicts = ("pass", "fail", "undecided")
    cells = [
        (list(definite), list(undecidable), on_unbound)
        for definite_size in range(3)
        for definite in itertools.product(verdicts, repeat=definite_size)
        for undecidable_size in range(4)
        for undecidable in itertools.product(verdicts, repeat=undecidable_size)
        for on_unbound in ("fail", "pass")
    ]

    disagreements = [
        (definite, undecidable, on_unbound, observed, expected)
        for definite, undecidable, on_unbound in cells
        for observed in [_cell_verdict(definite, undecidable, on_unbound)]
        for expected in [_every_completion(definite, undecidable, on_unbound)]
        if observed != expected
    ]

    reachable = {_every_completion(*cell) for cell in cells}

    assert len(cells) == 1040, "|D| <= 2 and |U| <= 3 over three verdicts, both policies"
    assert disagreements == []
    assert reachable == {"pass", "fail", "undecided"}, "agreement on one verdict is not agreement"
    assert ([], ["pass", "fail"], "fail") in cells, "the cell the two ends get wrong is swept"
    assert _every_completion([], ["pass", "fail"], "fail") == "undecided", "both ends read fail"


def test_a_definite_candidate_absorbs_an_undecidable_reading_of_the_same_value():
    """Dedup spans the undecidable set, because a definite binding subsumes a doubtful one.

    The value is in the candidate set whatever the missing status would have said, so
    the undecidable copy enumerates a reading already read. Left in, it would name
    the value twice and — where the definite reading fails — report the constraint as
    undecided beside a failure every completion agrees on.
    """
    both = _candidate_cell(["fail"], [])
    duplicated = build_timeline(
        turns=(("user", "Update the records."), ("assistant", "Working.")),
        recorded=[recorded_call(_WRITE, sequence=0, arguments={"record_id": "D0"})],
        unexecuted=[ToolCall(id="unrun_write_0", name=_WRITE, arguments={"record_id": "D0"})],
    )

    definite_only = evaluate_constraint(both, _BOUND_PRESENT, bind=_SETTLED_BINDER)
    with_a_doubtful_copy = evaluate_constraint(duplicated, _BOUND_PRESENT, bind=_SETTLED_BINDER)

    assert definite_only.passed is False
    assert "failed under (rec='D0')" in definite_only.message
    assert with_a_doubtful_copy.passed is False
    assert "cannot be decided" not in with_a_doubtful_copy.message, "every completion fails"
    assert with_a_doubtful_copy.message.count("(rec=") == 1, with_a_doubtful_copy.message


def test_a_result_extraction_with_no_outcome_event_leaves_the_candidate_unnamed():
    """A ``result`` read on a call the trial recorded no outcome for is missing evidence.

    The extraction reads nothing and the reason is that nobody wrote down what the
    call returned, so some completion of the record binds a value here and this trial
    cannot say which. The constraint is indeterminate and names ``result`` — silently
    resolving it to "no candidate" would score a records-less bundle as though the
    binder had genuinely fired on nothing.
    """
    timeline = build_turn_timeline(
        [
            Turn("user", "What is the invoice total?"),
            Turn(
                "assistant",
                "The invoice total is $42.00.",
                unexecuted=[ToolCall(id="unrun-1", name=_LOOKUP, arguments={})],
            ),
        ]
    )
    require = {
        "present": {"match": {"kind": "assistant_message", "text": {"contains_binding": "total"}}}
    }
    binding = {
        "match": _call_of(_LOOKUP, status={"equals": "success"}),
        "values": {"total": {"field": "result"}},
    }

    result = evaluate_constraint(timeline, require, bind=binding)

    call = [event for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL]
    assert timeline.records_present is False
    assert [event.status for event in call] == [None], "the call is there and its outcome is not"
    assert result.passed is False
    assert "cannot be decided" in result.message
    assert "result" in result.message, result.message


def test_an_extraction_reading_nothing_off_a_recorded_call_binds_no_candidate():
    """The rule is keyed on the evidence being absent, not on the value being ``None``.

    Here the trial recorded the call in full and the argument the binding addresses
    is simply not among its arguments — an absent value, which binds nothing and
    leaves the candidate set determined. Reading any empty extraction as missing
    evidence instead would make every records-present trial with an absent argument
    ungradeable.
    """
    timeline = build_timeline(
        turns=(("user", "Update the records."), ("assistant", "Working.")),
        recorded=[recorded_call(_WRITE, sequence=0, arguments={"other_key": "X"})],
    )
    two_such_calls = build_timeline(
        turns=(("user", "Update the records."), ("assistant", "Working.")),
        recorded=[
            recorded_call(_WRITE, sequence=0, arguments={"other_key": "X"}),
            recorded_call(_WRITE, sequence=1, arguments={"other_key": "Y"}),
        ],
    )

    result = evaluate_constraint(timeline, _BOUND_PRESENT, bind=_SETTLED_BINDER)
    two = evaluate_constraint(two_such_calls, _BOUND_PRESENT, bind=_SETTLED_BINDER)

    outcomes = [event for event in timeline.events if event.kind is TraceEventKind.TOOL_RESULT]
    recorded = [event.status for event in outcomes]

    assert recorded == [ToolExecutionStatus.SUCCESS], "the outcome is there to be read"
    assert result.passed is False
    assert "cannot be decided" not in result.message
    assert "the binding selected 1 event, none of which carried every value it extracts" in (
        result.message
    )
    assert "the binding selected 2 events, none of which carried every value it extracts" in (
        two.message
    )


def test_the_fold_reports_the_reading_whose_truth_is_the_folded_truth():
    """A definite failure beside an undecided candidate reports the failure.

    Mixed FALSE/UNKNOWN candidate sets are reachable only once the candidate set is
    itself three-valued, and the fold must pick the reading that *reached* the folded
    verdict rather than the first that is not a pass: picking the first would print
    "cannot be decided" beside a verdict every completion agrees is a failure.
    """
    result = evaluate_constraint(
        _candidate_cell(["undecided", "fail"], []), _BOUND_PRESENT, bind=_SETTLED_BINDER
    )

    assert result.passed is False
    assert "cannot be decided" not in result.message, "D1 fails under every completion"
    assert "failed under (rec='D1')" in result.message
    assert "(rec='D0')" not in result.message, "the undecided candidate did not reach the verdict"


def test_a_bound_gate_that_cannot_be_decided_trips_the_trial():
    """An undecided gate is not a pass in the agent's favour, bound or not."""
    config = TraceChecksConfig(
        constraints=[
            {
                "id": "every_write_was_read",
                "description": "the agent read each record before it wrote to it",
                "severity": "gate",
                "bind": _SETTLED_BINDER,
                "require": _BOUND_PRESENT,
            }
        ]
    )

    result = evaluate_trace_checks(_candidate_cell([], ["pass", "fail"]), config)

    assert result.constraints[0].passed is False
    assert "cannot be decided" in result.constraints[0].message
    assert result.gate_failed is True
    assert result.failed_gate_ids == ["every_write_was_read"]
    assert result.score == 0.0


def _denied_record_timeline(*calls: tuple[str, Any]) -> TrialTimeline:
    """``calls`` as in :func:`_record_timeline`, with a denial the second route asks for."""
    return build_timeline(
        turns=(("user", "Update the records."), ("assistant", "On it.")),
        recorded=[
            recorded_call(tool, sequence=index, arguments={"record_id": record})
            for index, (tool, record) in enumerate(calls)
        ]
        + [recorded_call(_DENIAL, sequence=len(calls))],
    )


_CORRELATED_ROUTES = TraceChecksConfig(
    alternatives=[
        {
            "id": "read_first",
            "description": "the agent read each record before writing it",
            "constraints": [
                {
                    "id": "correlated",
                    "description": "every record written was read first",
                    **_read_before_write(),
                }
            ],
        },
        {
            "id": "denied",
            "description": "the agent declined the request instead",
            "constraints": [
                {
                    "id": "declined",
                    "description": "the agent denied the case",
                    "require": {"present": {"match": _call_of(_DENIAL)}},
                }
            ],
        },
    ]
)


def test_a_bound_constraint_scores_inside_the_route_that_declares_it():
    """Correlation composes with routes: the binder is resolved per route member.

    The same two routes over two trajectories that differ only in which record the
    write names, so the correlation is the whole reason the route scores what it does
    and the argmax between the routes turns on it.
    """
    correlated = evaluate_trace_checks(
        _denied_record_timeline((_READ, "X"), (_WRITE, "X")), _CORRELATED_ROUTES
    )
    crossed = evaluate_trace_checks(
        _denied_record_timeline((_READ, "X"), (_WRITE, "Y")), _CORRELATED_ROUTES
    )

    assert [(path.id, path.score) for path in correlated.paths] == [
        ("read_first", 1.0),
        ("denied", 1.0),
    ]
    assert correlated.winning_path == "read_first", "a tie is won by the first declared route"
    assert [(path.id, path.score) for path in crossed.paths] == [
        ("read_first", 0.0),
        ("denied", 1.0),
    ]
    assert crossed.winning_path == "denied"
    assert [item.id for item in crossed.constraints] == ["declined"]


def test_a_bound_shared_gate_applies_to_every_route():
    """A shared gate is folded into each route's decision set before the argmax.

    So a correlation that fails shuts the component whichever route the agent
    walked — asserted per route rather than only on the winner, because a gate the
    fold reached on one route alone would still zero that route's component.
    """
    config = TraceChecksConfig(
        constraints=[
            {
                "id": "never_wrote_blind",
                "description": "the agent read each record before writing it",
                "severity": "gate",
                **_read_before_write(),
            }
        ],
        alternatives=_CORRELATED_ROUTES.alternatives,
    )

    result = evaluate_trace_checks(_denied_record_timeline((_READ, "X"), (_WRITE, "Y")), config)

    assert [(path.id, path.gate_failed) for path in result.paths] == [
        ("read_first", True),
        ("denied", True),
    ]
    assert result.failed_gate_ids == ["never_wrote_blind"]
    assert result.gate_failed is True
    assert result.score == 0.0
    assert "failed under (rec='Y')" in result.constraints[0].message


@pytest.mark.parametrize(
    ("recorded_text", "passes"),
    [("order 42 is already refunded", True), ("the account is closed", False)],
)
def test_a_result_predicate_over_a_failed_call_is_decided_by_the_recorded_text(
    recorded_text: str, passes: bool
):
    """An author may assert *why* a call failed, not only that it did.

    The two rows share the ``error`` status and differ only in the text the tool
    stated, so a constraint that passed on the status alone would pass both. Which
    verdict this reaches is substrate-independent: both substrates record one text
    for one failure, held byte-equal by the timeline parity suite, and the key
    manifest names one evaluator for both.
    """
    timeline = build_timeline(
        turns=(("user", "Refund the order."), ("assistant", "Refunding.")),
        recorded=[recorded_call(_LOOKUP, status=ToolExecutionStatus.ERROR, output=recorded_text)],
    )
    config = TraceChecksConfig(
        constraints=[
            {
                "id": "refused_as_already_refunded",
                "description": "the refund failed saying the order was already refunded",
                "require": {
                    "present": {
                        "match": {
                            "kind": "tool_result",
                            "status": {"equals": "error"},
                            "result": {"contains": "already refunded"},
                        }
                    }
                },
            }
        ]
    )

    verdict = evaluate_trace_checks(timeline, config).constraints[0]

    assert verdict.passed is passes
    assert verdict.undecided is False
