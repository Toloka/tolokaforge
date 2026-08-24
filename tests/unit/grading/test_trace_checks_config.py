"""What a ``trace_checks`` block may say, and what it is rejected for saying.

The vocabulary is type-level: no task context. So every shape an author can get
wrong here is answerable at load, and answering it there is what makes the
evaluator's "a predicate on a ``None`` field is unmatched" rule safe — without
these rejections a typo produces a matcher that selects nothing, which the default
``on_missing`` reports as the agent's failure rather than the author's. What needs
the task's tools is in ``tests/unit/grading/test_grading_authoring_gate.py``.

Each row of :data:`_REJECTIONS` is one malformed block and the remediation text its
message must carry. :func:`test_the_rejection_table_names_every_validator_that_raises`
holds the table against the module: a validator that raises a ``ValueError`` no row
provokes is a rejection nothing pins the wording of.

The self-referential shapes are the one family a timeline is read for: whether an
ordering over one matcher is a constant is a claim about trajectories, so the ten
survivors are each driven against the trajectory that satisfies them and the one
that refutes them, and admitting a constant under a different spelling reds there.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_turn_timeline
from tests.utils.trace_checks_configs import (
    EVERY_CONSTRAINT_KIND,
    EVERY_OPERATOR_MATCHER,
    every_kind_block,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TraceEventKind
from tolokaforge.core.models import OnMissing, TraceChecksConfig
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    TRACE_MATCHABLE_FIELDS_BY_KIND,
    TRACE_PREDICATE_OPERATORS,
    TraceConstraint,
    TraceConstraintExpr,
    TraceConstraintSeverity,
    ValuePredicate,
)

pytestmark = pytest.mark.unit

_RUNNER_MODELS = Path(__file__).resolve().parents[3] / "tolokaforge" / "runner" / "models.py"

# The classes the vocabulary is declared in. The AST audit reads its validators out
# of these and nothing else, so a raise elsewhere in the module is not this table's
# to cover.
_VOCABULARY_CLASSES = frozenset(
    {
        "ValuePredicate",
        "BoundValue",
        "TraceBinding",
        "TraceMatcher",
        "AnchorSide",
        "CountConstraint",
        "ImmediatelyBeforeConstraint",
        "TraceConstraintExpr",
        "TurnWindow",
        "TraceConstraint",
        "TracePath",
        "TraceChecksConfig",
    }
)


def _constraint(require: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """One well-formed constraint, so a rejection below is the one under test."""
    return {
        "id": "probe",
        "description": "a probe constraint",
        "require": require,
        **overrides,
    }


def _block(*constraints: dict[str, Any]) -> dict[str, Any]:
    return {"constraints": list(constraints)}


def _path(path_id: str, *constraints: dict[str, Any]) -> dict[str, Any]:
    """One well-formed alternative route."""
    return {
        "id": path_id,
        "description": f"the {path_id} route",
        "constraints": list(constraints),
    }


_TOOL_CALL = {"kind": "tool_call", "tool": {"equals": "write_file"}}

# An ordering over two distinct matchers, so a row driving it is rejected for what
# the row is about rather than for ordering one matcher against itself.
_AN_ORDERING = {
    "before": {
        "left": {"quantifier": "first", "match": _TOOL_CALL},
        "right": {
            "quantifier": "first",
            "match": {"kind": "tool_call", "tool": {"equals": "close_widget"}},
        },
    }
}

# A correlation over one name: the binder draws a case id off one call, and the
# require tree asserts another call carried the same one. The rows below malform one
# part of it at a time, so each is rejected for the part it malforms.
_BOUND_CASE = {"field": "args.case_id"}


def _binder(**overrides: Any) -> dict[str, Any]:
    return {
        "match": {"kind": "tool_call", "tool": {"equals": "open_case"}},
        "values": {"case": _BOUND_CASE},
        **overrides,
    }


def _references_the_case(**predicates: Any) -> dict[str, Any]:
    return {
        "present": {
            "match": {
                "kind": "tool_call",
                "tool": {"equals": "close_case"},
                "args": {"case_id": predicates or {"equals_binding": "case"}},
            }
        }
    }


# One value per operator, in the shape that operator reads. ``exists: False`` and
# ``gt: 0`` are the rows that matter: both are falsy, and a declaredness rule
# reading truthiness rather than presence would drop them.
_OPERATOR_SAMPLES: dict[str, Any] = {
    "equals": "written",
    "equals_ci": "WRITTEN",
    "contains": "writ",
    "contains_ci": "WRIT",
    "not_equals": "deleted",
    "not_contains": "deleted",
    "regex": "^writ",
    "not_regex": "^del",
    "gt": 0.0,
    "gte": 1.5,
    "lt": 10.0,
    "lte": 9.5,
    "date_gt": "2026-01-01",
    "date_gte": "2026-01-01T00:00:00Z",
    "date_lt": "2027-01-01",
    "date_lte": "2026-12-31T23:59:59+01:00",
    "in_": ["written", "queued"],
    "not_in": ["deleted"],
    "len_gt": 0,
    "len_gte": 2,
    "exists": False,
    "is_null": True,
    "omitted": False,
    "equals_binding": "quoted_case",
    "contains_binding": "quoted_case",
}


# The self-referential shapes: one matcher on both sides of an ordering, or a
# forbidden matcher that is also the anchor its window is measured from. Written as
# builders because the whole quantifier cross-product is swept below, and the
# rejection rows above are four cells of that same sweep.
_SELF_REFERENTIAL_MATCH = {"kind": "tool_call", "tool": {"equals": "http_request"}}


def _self_referential_order(kind: str, left: str, right: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "left": {"quantifier": left, "match": _SELF_REFERENTIAL_MATCH},
        "right": {"quantifier": right, "match": _SELF_REFERENTIAL_MATCH},
    }
    if kind == "immediately_before":
        payload["among"] = "tool_calls"
    return {kind: payload}


def _self_referential_prefix(anchor: str) -> dict[str, Any]:
    return {
        "absent_before": {
            "forbidden": _SELF_REFERENTIAL_MATCH,
            "anchor": {"quantifier": anchor, "match": _SELF_REFERENTIAL_MATCH},
        }
    }


def _self_referential_window(start: str, end: str) -> dict[str, Any]:
    return {
        "absent_between": {
            "forbidden": _SELF_REFERENTIAL_MATCH,
            "start": {"quantifier": start, "match": _SELF_REFERENTIAL_MATCH},
            "end": {"quantifier": end, "match": _SELF_REFERENTIAL_MATCH},
        }
    }


@dataclass(frozen=True)
class _Rejection:
    """One malformed block, and the fix its error message must name."""

    label: str
    block: dict[str, Any]
    message: str
    validator: str | None = field(default=None)
    """The model validator that raises it. ``None`` when pydantic itself rejects the
    shape — an unknown key, a missing field, a bound — and the module declares no
    raise of its own."""


_REJECTIONS: tuple[_Rejection, ...] = (
    _Rejection(
        label="unknown_operator",
        # Beside a valid operator, so the predicate is not *also* rejected for
        # asserting nothing — whose message lists every operator name and would let
        # this row pass with extra="forbid" dropped.
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_call",
                            "tool": {"equals": "write_file", "kontains": "writ"},
                        }
                    }
                }
            )
        ),
        message="kontains",
    ),
    _Rejection(
        label="unknown_constraint_kind",
        block=_block(_constraint({"after": {"match": _TOOL_CALL}})),
        message="after",
    ),
    _Rejection(
        label="unknown_matcher_field",
        block=_block(
            _constraint({"present": {"match": {"kind": "tool_call", "latency_seconds": {"gt": 1}}}})
        ),
        message="latency_seconds",
    ),
    _Rejection(
        label="no_constraint_kind",
        block=_block(_constraint({})),
        message="exactly one",
        validator="_require_exactly_one_kind",
    ),
    _Rejection(
        label="two_constraint_kinds",
        block=_block(
            _constraint(
                {
                    "present": {"match": _TOOL_CALL},
                    "absent": {"match": _TOOL_CALL},
                }
            )
        ),
        message="exactly one",
        validator="_require_exactly_one_kind",
    ),
    _Rejection(
        label="predicate_with_no_operators",
        block=_block(_constraint({"present": {"match": {"kind": "tool_call", "tool": {}}}})),
        message="declares no operator",
        validator="_reject_a_predicate_asserting_nothing",
    ),
    _Rejection(
        label="field_the_kind_never_carries",
        block=_block(
            _constraint(
                {"present": {"match": {"kind": "assistant_message", "status": {"equals": "error"}}}}
            )
        ),
        message="carries no ['status']",
        validator="_reject_fields_the_kind_never_carries",
    ),
    _Rejection(
        label="status_literal_no_execution_produces",
        block=_block(
            _constraint(
                {"present": {"match": {"kind": "tool_result", "status": {"equals": "expired"}}}}
            )
        ),
        message="no tool executor produces",
        validator="_reject_a_status_literal_no_execution_produces",
    ),
    _Rejection(
        label="immediately_before_without_among",
        block=_block(
            _constraint(
                {
                    "immediately_before": {
                        "left": {"quantifier": "any", "match": _TOOL_CALL},
                        "right": {"quantifier": "any", "match": _TOOL_CALL},
                    }
                }
            )
        ),
        message="needs an explicit among",
        validator="_require_the_view_adjacency_is_read_in",
    ),
    _Rejection(
        label="anchor_quantified_any",
        block=_block(
            _constraint(
                {
                    "absent_before": {
                        "forbidden": _TOOL_CALL,
                        "anchor": {"quantifier": "any", "match": _TOOL_CALL},
                    }
                }
            )
        ),
        message="Write 'first'",
        validator="_reject_the_quantifiers_a_window_cannot_read",
    ),
    _Rejection(
        label="anchor_quantified_all",
        block=_block(
            _constraint(
                {
                    "absent_between": {
                        "forbidden": _TOOL_CALL,
                        "start": {"quantifier": "all", "match": _TOOL_CALL},
                        "end": {"quantifier": "last", "match": _TOOL_CALL},
                    }
                }
            )
        ),
        message="Write 'last'",
        validator="_reject_the_quantifiers_a_window_cannot_read",
    ),
    _Rejection(
        label="on_missing_on_present",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, on_missing="pass")),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="on_missing_on_absent",
        block=_block(_constraint({"absent": {"match": _TOOL_CALL}}, on_missing="pass")),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="on_missing_on_count",
        block=_block(
            _constraint({"count": {"match": _TOOL_CALL, "max": 2}}, on_missing="fail"),
        ),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    # Each composite passes ``on_missing`` down unchanged, so the four spellings
    # below reach the same anchorless kind the three rows above reject at the top —
    # ``all_of`` and ``any_of`` returning the pass the policy invented, ``negate``
    # returning its complement. All four are the check that cannot decide the thing
    # it asserts, written one level lower.
    _Rejection(
        label="on_missing_nested_in_all_of",
        block=_block(
            _constraint({"all_of": [{"present": {"match": _TOOL_CALL}}]}, on_missing="pass")
        ),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="on_missing_nested_in_any_of",
        block=_block(
            _constraint({"any_of": [{"absent": {"match": _TOOL_CALL}}]}, on_missing="pass")
        ),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="on_missing_nested_in_negate",
        block=_block(
            _constraint({"negate": {"count": {"match": _TOOL_CALL, "max": 2}}}, on_missing="fail")
        ),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="on_missing_nested_under_three_composites",
        block=_block(
            _constraint(
                {
                    "all_of": [
                        _AN_ORDERING,
                        {"any_of": [{"negate": {"present": {"match": _TOOL_CALL}}}]},
                    ]
                },
                on_missing="pass",
            )
        ),
        message="nothing to decide",
        validator="_reject_an_unmatched_anchor_policy_where_nothing_is_anchored",
    ),
    _Rejection(
        label="count_with_no_bound",
        block=_block(_constraint({"count": {"match": _TOOL_CALL}})),
        message="neither min nor max",
        validator="_require_a_bound_that_can_fail",
    ),
    _Rejection(
        label="count_bounds_admit_nothing",
        block=_block(_constraint({"count": {"match": _TOOL_CALL, "min": 4, "max": 2}})),
        message="admit no match count",
        validator="_require_a_bound_that_can_fail",
    ),
    _Rejection(
        label="duplicate_constraint_id",
        block=_block(
            _constraint({"present": {"match": _TOOL_CALL}}),
            _constraint({"absent": {"match": _TOOL_CALL}}),
        ),
        message="declared more than once",
        validator="_require_distinct_ids",
    ),
    _Rejection(
        label="path_id_colliding_with_a_shared_constraint_id",
        # The collision a per-list uniqueness rule admits: the shared constraint and
        # the path are each unique inside their own list, and both answer to
        # ``trace_checks.probe``.
        block={
            "constraints": [_constraint({"present": {"match": _TOOL_CALL}})],
            "alternatives": [
                _path("probe", _constraint({"absent": {"match": _TOOL_CALL}}, id="via_absent")),
                _path(
                    "via_count",
                    _constraint({"count": {"match": _TOOL_CALL, "min": 1}}, id="counted"),
                ),
            ],
        },
        message="['probe']",
        validator="_require_distinct_ids",
    ),
    _Rejection(
        label="constraint_id_repeated_across_two_paths",
        block={
            "constraints": [_constraint({"present": {"match": _TOOL_CALL}}, id="shared")],
            "alternatives": [
                _path("first_route", _constraint({"absent": {"match": _TOOL_CALL}}, id="step")),
                _path("second_route", _constraint({"present": {"match": _TOOL_CALL}}, id="step")),
            ],
        },
        message="['step']",
        validator="_require_distinct_ids",
    ),
    _Rejection(
        label="neither_constraints_nor_alternatives",
        block={},
        message="declares neither constraints nor alternatives",
        validator="_require_something_to_score",
    ),
    _Rejection(
        label="an_empty_constraints_list_alone",
        block=_block(),
        message="declares neither constraints nor alternatives",
        validator="_require_something_to_score",
    ),
    _Rejection(
        label="one_alternative_path",
        block={
            "alternatives": [_path("only_route", _constraint({"present": {"match": _TOOL_CALL}}))]
        },
        message="the flat form written the long way round",
        validator="_require_alternatives_to_be_alternative",
    ),
    _Rejection(
        label="zero_alternative_paths",
        block={"alternatives": []},
        message="the flat form written the long way round",
        validator="_require_alternatives_to_be_alternative",
    ),
    _Rejection(
        label="a_path_with_no_constraints",
        block={
            "alternatives": [
                _path("empty_route"),
                _path("other_route", _constraint({"present": {"match": _TOOL_CALL}})),
            ]
        },
        message="at least 1 item",
    ),
    _Rejection(
        label="a_route_of_nothing_but_gates_beside_a_scored_one",
        block={
            "alternatives": [
                _path(
                    "gate_only_route",
                    _constraint({"absent": {"match": _TOOL_CALL}}, id="g", severity="gate"),
                ),
                _path("scored_route", _constraint({"present": {"match": _TOOL_CALL}}, id="s")),
            ]
        },
        message="collapses to its gates' verdict",
        validator="_require_every_route_to_be_scored_where_any_route_is",
    ),
    _Rejection(
        label="inverted_turn_window",
        block=_block(
            _constraint(
                {"present": {"match": _TOOL_CALL}}, within={"first_turn": 5, "last_turn": 2}
            )
        ),
        message="no turn falls inside it",
        validator="_require_a_window_some_turn_falls_in",
    ),
    _Rejection(
        label="turn_window_restricting_nothing",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, within={})),
        message="restricts nothing",
        validator="_require_a_window_some_turn_falls_in",
    ),
    _Rejection(
        label="negative_weight",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, weight=-1.0)),
        message="inverts the fold",
        validator="_require_a_weight_that_scores",
    ),
    _Rejection(
        label="zero_weight",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, weight=0.0)),
        message="severity: gate",
        validator="_require_a_weight_that_scores",
    ),
    _Rejection(
        label="weight_on_a_gate",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, severity="gate", weight=2.0)),
        message="excluded from the weighted average",
        validator="_reject_a_weight_on_a_check_nothing_scores",
    ),
    _Rejection(
        label="an_unmatched_anchor_opening_a_gate",
        block=_block(_constraint(_AN_ORDERING, severity="gate", on_missing="pass")),
        message="holds vacuously",
        validator="_reject_an_anchor_policy_that_opens_a_gate_vacuously",
    ),
    _Rejection(
        label="misspelled_severity",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, severity="gated")),
        message="'scored' or 'gate'",
    ),
    _Rejection(
        label="weight_that_is_not_a_number",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, weight=float("nan"))),
        message="is not a finite number",
        validator="_require_a_weight_that_scores",
    ),
    _Rejection(
        label="empty_conjunction",
        block=_block(_constraint({"all_of": []})),
        message="at least 1 item",
    ),
    _Rejection(
        label="matcher_without_a_kind",
        block=_block(_constraint({"present": {"match": {"tool": {"equals": "x"}}}})),
        message="match.kind",
    ),
    _Rejection(
        label="before_over_one_matcher",
        block=_block(_constraint(_self_referential_order("before", "last", "first"))),
        message="orders one matcher against itself",
        validator="_reject_an_order_over_one_matcher_that_no_trial_decides",
    ),
    _Rejection(
        label="immediately_before_over_one_matcher",
        block=_block(
            _constraint(_self_referential_order("immediately_before", "all", "all")),
        ),
        message="orders one matcher against itself",
        validator="_reject_an_order_over_one_matcher_that_no_trial_decides",
    ),
    _Rejection(
        label="absent_before_its_own_first_match",
        block=_block(_constraint(_self_referential_prefix("first"))),
        message="nothing precedes the first of them",
        validator="_reject_an_order_over_one_matcher_that_no_trial_decides",
    ),
    _Rejection(
        label="absent_between_its_own_matches",
        block=_block(_constraint(_self_referential_window("last", "first"))),
        message="leaves no interval any trajectory opens",
        validator="_require_a_self_referential_window_some_trial_opens",
    ),
    _Rejection(
        label="binding_reference_to_an_unbound_name",
        block=_block(_constraint(_references_the_case())),
        message="references ['case'] name no value this constraint binds",
        validator="_reject_a_reference_to_a_name_nothing_binds",
    ),
    _Rejection(
        label="binder_referencing_what_it_binds",
        block=_block(
            _constraint(
                _references_the_case(),
                bind=_binder(
                    match={
                        "kind": "tool_call",
                        "tool": {"equals": "open_case"},
                        "args": {"case_id": {"equals_binding": "case"}},
                    }
                ),
            )
        ),
        message="the binder's own match references ['case']",
        validator="_reject_a_binder_reading_the_names_it_binds",
    ),
    _Rejection(
        label="bound_value_nothing_references",
        block=_block(
            _constraint(
                {"present": {"match": {"kind": "tool_call", "tool": {"equals": "close_case"}}}},
                bind=_binder(),
            )
        ),
        message="bindings ['case'] are extracted and never referenced",
        validator="_reject_a_bound_value_nothing_reads",
    ),
    _Rejection(
        label="binder_binding_nothing",
        block=_block(_constraint(_references_the_case(), bind=_binder(values={}))),
        message="a binding declares no values",
        validator="_require_a_binding_that_binds_something",
    ),
    _Rejection(
        label="extraction_the_kind_cannot_read",
        block=_block(
            _constraint(_references_the_case(), bind=_binder(values={"case": {"field": "text"}}))
        ),
        message="addressing a field the kind does not carry",
        validator="_reject_an_extraction_the_binder_cannot_read",
    ),
    _Rejection(
        label="extraction_over_a_closed_vocabulary",
        block=_block(
            _constraint(_references_the_case(), bind=_binder(values={"case": {"field": "status"}}))
        ),
        message="whose domain is a handful of members",
        validator="_reject_an_extraction_over_a_closed_vocabulary",
    ),
    _Rejection(
        label="capture_pattern_with_two_groups",
        block=_block(
            _constraint(
                _references_the_case(),
                bind=_binder(values={"case": {"field": "args.note", "pattern": "(a)(b)"}}),
            )
        ),
        message="captures 2 groups, and a binding reads exactly one",
        validator="_require_a_pattern_that_captures_exactly_one_value",
    ),
    _Rejection(
        label="on_unbound_pass_on_a_gate",
        block=_block(
            _constraint(
                _references_the_case(),
                bind=_binder(on_unbound="pass"),
                severity="gate",
            )
        ),
        message="on_unbound: pass opens a severity: gate constraint",
        validator="_reject_a_binding_policy_that_opens_a_gate_vacuously",
    ),
    _Rejection(
        label="date_bound_no_calendar_holds",
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_call",
                            "args": {"departure_date": {"date_gte": "next week"}},
                        }
                    }
                }
            )
        ),
        message="ISO-8601",
        validator="_require_a_date_literal_some_calendar_holds",
    ),
    _Rejection(
        label="nullness_probe_on_recorded_evidence",
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_result",
                            "status": {"is_null": True},
                        }
                    }
                }
            )
        ),
        message="missing evidence",
        validator="_reject_a_nullness_probe_on_recorded_evidence",
    ),
    _Rejection(
        label="status_literal_no_execution_produces",
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_result",
                            "status": {"equals": "expired"},
                        }
                    }
                }
            )
        ),
        message="expired",
        validator="_reject_a_status_literal_no_execution_produces",
    ),
)


@pytest.mark.parametrize("rejection", _REJECTIONS, ids=[row.label for row in _REJECTIONS])
def test_a_malformed_block_is_rejected_naming_the_fix(rejection: _Rejection):
    with pytest.raises(ValidationError) as excinfo:
        TraceChecksConfig(**rejection.block)

    assert rejection.message in str(excinfo.value), str(excinfo.value)


# The three binding boundaries below stand on tests of their own rather than on
# rows of the sweep above. The sweep asserts the wording of one rejection; these
# assert that the *neighbouring* well-formed shape still loads, which is what a
# rule widened into rejecting every binding would fail.


def test_a_reference_to_a_name_nothing_binds_is_a_load_error():
    with pytest.raises(ValidationError) as excinfo:
        TraceChecksConfig(**_block(_constraint(_references_the_case())))

    assert "case" in str(excinfo.value)
    TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=_binder())))


def test_a_binder_referencing_the_name_it_binds_is_a_load_error():
    self_referential = _binder(
        match={
            "kind": "tool_call",
            "tool": {"equals": "open_case"},
            "args": {"case_id": {"equals_binding": "case"}},
        }
    )

    with pytest.raises(ValidationError) as excinfo:
        TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=self_referential)))

    assert "case" in str(excinfo.value)
    TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=_binder())))


def test_a_bound_value_no_reference_reads_is_a_load_error():
    unread = _binder(values={"case": _BOUND_CASE, "opened_by": {"field": "args.actor"}})

    with pytest.raises(ValidationError) as excinfo:
        TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=unread)))

    assert "opened_by" in str(excinfo.value)
    assert "'case'" not in str(excinfo.value), "the referenced name is not the offender"
    TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=_binder())))


# A ``result`` read is admissible whatever status the call it selects ended with.
# Both substrates record one text for one failure — the tool's own message — so
# nothing about the status decides whether the text is portable. The two sweeps
# below span the status shapes an author writes, including the ones a scoping rule
# would have had to refuse.

_STATUSES_A_RESULT_READ_LOADS_BESIDE: tuple[dict[str, Any] | None, ...] = (
    None,
    {"equals": "error"},
    {"equals": "success"},
    {"not_equals": "error"},
    {"equals": "success", "exists": True},
)


@pytest.mark.parametrize("status", _STATUSES_A_RESULT_READ_LOADS_BESIDE)
def test_a_result_predicate_loads_beside_any_status_predicate(status: dict[str, Any] | None):
    matcher: dict[str, Any] = {"kind": "tool_result", "result": {"contains": "already refunded"}}
    if status is not None:
        matcher["status"] = status

    config = TraceChecksConfig(**_block(_constraint({"present": {"match": matcher}})))

    loaded = config.constraints[0].require.present.match
    assert loaded.result.declared_operators() == {"contains"}
    assert (loaded.status is None) is (status is None)


@pytest.mark.parametrize("status", _STATUSES_A_RESULT_READ_LOADS_BESIDE)
def test_a_binder_reading_result_loads_beside_any_status_predicate(status: dict[str, Any] | None):
    match: dict[str, Any] = {"kind": "tool_call", "tool": {"equals": "open_case"}}
    if status is not None:
        match["status"] = status
    binding = {"match": match, "values": {"case": {"field": "result", "pattern": r"case ([0-9]+)"}}}

    config = TraceChecksConfig(**_block(_constraint(_references_the_case(), bind=binding)))

    assert config.constraints[0].bound_names() == {"case"}


def _vocabulary_class_nodes() -> dict[str, ast.ClassDef]:
    """The vocabulary's class definitions, by name.

    Read from the source rather than the imported classes: a pydantic validator is
    wrapped by the time it is an attribute, and what this needs to know is where a
    raise is *written*.
    """
    found = {
        node.name: node
        for node in ast.walk(ast.parse(_RUNNER_MODELS.read_text()))
        if isinstance(node, ast.ClassDef) and node.name in _VOCABULARY_CLASSES
    }
    missing = sorted(_VOCABULARY_CLASSES - set(found))
    assert not missing, (
        f"the audit names {missing}, which {_RUNNER_MODELS.name} no longer declares, so it "
        "would read no raises out of them and pass on an empty audit"
    )
    return found


def _validators_that_raise() -> frozenset[str]:
    """Every function inside the vocabulary's classes that raises a ``ValueError``."""
    raising: set[str] = set()
    for node in _vocabulary_class_nodes().values():
        for function in node.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for statement in ast.walk(function):
                if (
                    isinstance(statement, ast.Raise)
                    and isinstance(statement.exc, ast.Call)
                    and isinstance(statement.exc.func, ast.Name)
                    and statement.exc.func.id == "ValueError"
                ):
                    raising.add(function.name)
    return frozenset(raising)


def test_the_rejection_table_names_every_validator_that_raises():
    """A rejection the table does not provoke is a message nothing pins.

    The two sources are independent: the table's labels are hand-written above, and
    this reads the raises out of the module. A validator added without a row fails
    here rather than shipping with wording no author has ever been shown.
    """
    declared = {row.validator for row in _REJECTIONS if row.validator is not None}
    written = _validators_that_raise()

    assert declared == written, (
        f"the rejection table covers {sorted(declared)} but the vocabulary's models raise "
        f"from {sorted(written)}. Every load rejection needs a row asserting the text an "
        "author is shown"
    )


def test_the_whole_vocabulary_loads():
    """The positive half: every kind and every operator in one accepted block."""
    config = TraceChecksConfig(**every_kind_block())

    assert len(config.constraints) == len(TRACE_CONSTRAINT_KINDS)
    assert {item.require.declared_kind() for item in config.constraints} == TRACE_CONSTRAINT_KINDS


@pytest.mark.parametrize("severity", sorted(item.value for item in TraceConstraintSeverity))
def test_either_severity_loads_and_a_gate_needs_no_weight(severity: str):
    """The enum is writable on both sides, and a gate carries the default share.

    A gate is excluded from the weighted average, so the weight it holds is read by
    nothing — which is why it may not be *written*, and why the default must load.
    """
    config = TraceChecksConfig(
        **_block(_constraint({"present": {"match": _TOOL_CALL}}, severity=severity))
    )

    assert config.constraints[0].severity is TraceConstraintSeverity(severity)
    assert config.constraints[0].weight == 1.0


def test_a_constraint_that_writes_no_severity_is_scored():
    """The unwritten value is the enum member, not an absence a reader coalesces.

    Two readers would each have to supply the same default otherwise, and the
    result-side twin already answers ``scored`` — a tri-state over a two-valued
    domain is the shape from which the two can disagree.
    """
    config = TraceChecksConfig(**_block(_constraint({"present": {"match": _TOOL_CALL}})))

    assert config.constraints[0].severity is TraceConstraintSeverity.SCORED


def test_a_gate_may_decide_an_unmatched_anchor_by_failing():
    """The complement of the vacuous-gate rejection: ``on_missing: fail`` is writable.

    Without this the rejection could be widened to every ``on_missing`` beside a
    gate and nothing would notice — which would take the default policy away from
    the gates that state it explicitly.
    """
    config = TraceChecksConfig(
        **_block(_constraint(_AN_ORDERING, severity="gate", on_missing="fail"))
    )

    assert (config.constraints[0].severity, config.constraints[0].on_missing) == (
        TraceConstraintSeverity.GATE,
        OnMissing.FAIL,
    )


def test_a_block_whose_every_route_is_unscoreable_loads():
    """The all-gates collapse is a defined verdict, and the rejection does not reach it.

    A route with nothing scored is rejected for tying or beating a *scored*
    sibling. With no scored sibling anywhere there is nothing for it to beat: the
    block asks the gates' own question of whichever route the agent walked, which
    is the collapse ``docs/GRADING.md`` § "severity — a check that must hold"
    documents. A rejection stated over one path in isolation reds here.
    """
    config = TraceChecksConfig(
        alternatives=[
            _path(
                "by_lookup",
                _constraint({"present": {"match": _TOOL_CALL}}, id="g", severity="gate"),
            ),
            _path(
                "by_denial", _constraint({"absent": {"match": _TOOL_CALL}}, id="h", severity="gate")
            ),
        ]
    )

    assert [item.severity for path in config.alternatives for item in path.constraints] == [
        TraceConstraintSeverity.GATE
    ] * 2


def test_a_shared_scored_check_is_what_makes_a_gate_only_route_scoreable():
    """The decision set is the shared constraints plus the route's own, at load too.

    One route carries a gate and nothing else while its sibling carries a scored
    check, which is the pair the rejection is about — and the block is admitted,
    because the shared check both routes carry is scored, so neither decision set
    collapses. A rule reading ``path.constraints`` alone rejects this block, which
    is a pack the evaluator grades correctly.
    """
    config = TraceChecksConfig(
        constraints=[_constraint({"present": {"match": _TOOL_CALL}}, id="shared_step")],
        alternatives=[
            _path(
                "by_lookup",
                _constraint({"absent": {"match": _TOOL_CALL}}, id="g", severity="gate"),
            ),
            _path("by_denial", _constraint({"present": {"match": _TOOL_CALL}}, id="h")),
        ],
    )

    assert [path.id for path in config.alternatives] == ["by_lookup", "by_denial"]


def test_a_gate_survives_the_trial_spec_round_trip():
    """The runner revalidates the config it is handed, and must reach the same verdict.

    Dumping a model writes every field, so the delivered constraint arrives with
    ``weight`` in ``model_fields_set`` however the author wrote it. A rejection
    keyed on declaredness rather than on the value would therefore admit this gate
    on the engine and reject it inside the runner at ``RegisterTrial``.
    """
    authored = TraceChecksConfig(
        **_block(_constraint({"absent": {"match": _TOOL_CALL}}, severity="gate"))
    )

    delivered = TraceChecksConfig.model_validate_json(authored.model_dump_json())

    assert "weight" in delivered.constraints[0].model_fields_set
    assert delivered == authored


def test_a_block_declaring_no_alternatives_loads_and_dumps_as_a_flat_block():
    """The zero-paths runtime boundary: today's packs must not move a byte.

    ``alternatives`` unset is what every shipped pack writes, so its dumped value is
    pinned rather than merely absent — defaulting the field to ``[]`` would leave
    the block loading and its dump carrying an empty list where it carried a null.
    """
    config = TraceChecksConfig(**_block(_constraint({"present": {"match": _TOOL_CALL}})))

    assert config.alternatives is None
    dumped = config.model_dump()
    assert set(dumped) == {"constraints", "alternatives"}
    assert dumped["alternatives"] is None
    assert "alternatives" not in config.model_dump(exclude_defaults=True)


def test_a_purely_multi_path_block_omits_the_shared_constraints_entirely():
    """A pack whose every check belongs to a route declares no top-level list.

    Written as an omission rather than as ``constraints: []`` because that is the
    shape the differential fixture packs must take: a key written empty is still a
    key declared.
    """
    config = TraceChecksConfig(
        alternatives=[
            _path("served_vs_source", _constraint({"present": {"match": _TOOL_CALL}}, id="a")),
            _path("cache_inspector", _constraint({"absent": {"match": _TOOL_CALL}}, id="b")),
        ]
    )

    assert config.constraints == []
    assert [path.id for path in config.alternatives] == ["served_vs_source", "cache_inspector"]


def test_an_explicitly_empty_constraints_list_loads_beside_alternatives():
    """The relaxation is about what the block scores, not about how it is spelled."""
    config = TraceChecksConfig(
        constraints=[],
        alternatives=[
            _path("first_route", _constraint({"present": {"match": _TOOL_CALL}}, id="a")),
            _path("second_route", _constraint({"absent": {"match": _TOOL_CALL}}, id="b")),
        ],
    )

    assert config.constraints == []


def test_one_id_may_be_reused_by_nothing_anywhere_in_the_block():
    """The positive half of the id-space rule: distinct ids across the paths load.

    The rejection is scoped to repeats, not to path constraints sharing a namespace
    with the shared ones — which a rule reading the shared list alone would leave
    unproven in the other direction.
    """
    config = TraceChecksConfig(
        constraints=[_constraint({"present": {"match": _TOOL_CALL}}, id="shared_step")],
        alternatives=[
            _path("route_a", _constraint({"absent": {"match": _TOOL_CALL}}, id="a_step")),
            _path("route_b", _constraint({"present": {"match": _TOOL_CALL}}, id="b_step")),
        ],
    )

    declared = [item.id for item in config.constraints]
    for path in config.alternatives:
        declared += [path.id, *(item.id for item in path.constraints)]
    assert declared == ["shared_step", "route_a", "a_step", "route_b", "b_step"]


_KINDS_THAT_ANCHOR_NOTHING = frozenset({"present", "absent", "count"})

# A composite belongs to neither set: it anchors whatever it holds, so whether the
# policy has something to decide beside one is a question about the tree beneath it.
_COMPOSITE_KINDS = frozenset({"all_of", "any_of", "negate"})

_ANCHORING_LEAF_KINDS = sorted(
    set(EVERY_CONSTRAINT_KIND) - _KINDS_THAT_ANCHOR_NOTHING - _COMPOSITE_KINDS
)


@pytest.mark.parametrize("kind", _ANCHORING_LEAF_KINDS)
def test_on_missing_is_accepted_on_every_kind_that_anchors_something(kind: str):
    """The complement of the ``on_missing`` rejection rows above.

    Those rows stay green if the rejection widens to the whole vocabulary, which
    would leave ``on_missing`` unwritable and its default the only reachable
    policy. These four are the kinds that select an anchor distinct from the thing
    asserted, so the opt-in has something to decide.
    """
    assert len(_ANCHORING_LEAF_KINDS) == 4

    config = TraceChecksConfig(
        **_block(_constraint(EVERY_CONSTRAINT_KIND[kind], on_missing="pass"))
    )

    assert config.constraints[0].on_missing is OnMissing.PASS


def test_a_composite_over_anchored_kinds_still_admits_an_anchor_policy():
    """The composite arm of the same complement: the rule reads kinds, not nesting.

    ``examples/native/multi_service_cache_debug`` writes exactly this — an
    ``on_missing: pass`` over an ``all_of`` of orderings, so a read that never
    happened is charged to the presence check rather than to all three. A rule
    refusing every composite outright would leave that pack unloadable.
    """
    nested_orderings = {"all_of": [_AN_ORDERING, {"any_of": [_AN_ORDERING]}]}

    config = TraceChecksConfig(**_block(_constraint(nested_orderings, on_missing="pass")))

    assert config.constraints[0].on_missing is OnMissing.PASS


def test_the_matchable_table_answers_for_every_event_kind():
    """The sweep below reads the table, so a kind missing from it is never swept.

    Two sources: the table here and the event vocabulary the timeline builds. A
    kind added to :class:`TraceEventKind` with no row would leave every matcher
    over it rejected, and the per-kind sweep would not notice.
    """
    assert set(TRACE_MATCHABLE_FIELDS_BY_KIND) == set(TraceEventKind)


def test_a_matcher_may_read_every_field_its_kind_carries():
    """The rejection is scoped to fields the kind lacks, not to fields it has.

    Driven per kind over the declared table, so a rule that rejected everything —
    which every negative row above would still pass — fails here.
    """
    for kind, matchable in TRACE_MATCHABLE_FIELDS_BY_KIND.items():
        matcher: dict[str, Any] = {"kind": kind.value}
        for name in sorted(matchable):
            matcher[name] = {"payment_id": {"exists": True}} if name == "args" else {"exists": True}
        TraceConstraintExpr(present={"match": matcher})


def test_the_operator_samples_span_the_declared_vocabulary():
    """The table below answers for every operator, so the sweep is not a subset."""
    assert set(_OPERATOR_SAMPLES) == TRACE_PREDICATE_OPERATORS


@pytest.mark.parametrize(("operator", "value"), sorted(_OPERATOR_SAMPLES.items()))
def test_an_operator_is_declared_by_its_value_and_survives_the_wire(operator: str, value: Any):
    """Which operators a predicate asserts must mean the same on both substrates.

    The runner receives this config as JSON inside the trial spec, and dumping a
    model writes every unset field as ``null`` — so reading declaredness off
    pydantic's ``model_fields_set`` would report all seventeen operators after the
    round trip and grade a one-operator predicate as a seventeen-operator
    conjunction. Reading it off the values is what makes the two sides agree.
    """
    predicate = ValuePredicate(**{operator: value})
    assert predicate.declared_operators() == {operator}

    delivered = ValuePredicate.model_validate_json(predicate.model_dump_json())
    assert delivered.declared_operators() == {operator}, (
        f"{operator} is the only operator written, but after the wire round trip the "
        f"predicate asserts {sorted(delivered.declared_operators())}"
    )


_QUANTIFIERS = ("first", "last", "any", "all")

# The self-referential shapes some trajectory decides, and the two trajectories that
# decide them: the number of identical matching calls the shape is TRUE of, and the
# number it is FALSE of. Every other cell of the cross-product below is rejected.
#
# The witness differs by row and is the content of the rule. ``before`` reads "the
# events occur at least twice", so two calls satisfy it and one does not.
# ``absent_before`` anchored 'last' reads "the events occur once". ``absent_between``
# from first to last reads "exactly twice".
_ORDERING_SURVIVORS: dict[tuple[str, str, str], tuple[int, int]] = {
    (kind, left, right): (2, 1)
    for kind in ("before", "immediately_before")
    for left in ("first", "any")
    for right in ("last", "any")
}
_PREFIX_SURVIVORS: dict[str, tuple[int, int]] = {"last": (1, 2)}
_WINDOW_SURVIVORS: dict[tuple[str, str], tuple[int, int]] = {("first", "last"): (2, 1)}

_SELF_REFERENTIAL_CELL_COUNT = 38
_SELF_REFERENTIAL_SURVIVOR_COUNT = 10


def _self_referential_cells() -> list[tuple[str, dict[str, Any], tuple[int, int] | None]]:
    """Every self-referential shape the vocabulary can write, with its witnesses.

    The third element is ``None`` where the shape is rejected, and the (true, false)
    call counts where it survives.
    """
    cells = [
        (
            f"{kind}_{left}_before_{right}",
            _self_referential_order(kind, left, right),
            _ORDERING_SURVIVORS.get((kind, left, right)),
        )
        for kind, left, right in itertools.product(
            ("before", "immediately_before"), _QUANTIFIERS, _QUANTIFIERS
        )
    ]
    cells += [
        (f"absent_before_{anchor}", _self_referential_prefix(anchor), _PREFIX_SURVIVORS.get(anchor))
        for anchor in ("first", "last")
    ]
    cells += [
        (
            f"absent_between_{start}_{end}",
            _self_referential_window(start, end),
            _WINDOW_SURVIVORS.get((start, end)),
        )
        for start, end in itertools.product(("first", "last"), ("first", "last"))
    ]
    return cells


_SELF_REFERENTIAL_CELLS = _self_referential_cells()


def _timeline_of(matching_calls: int):
    """A trajectory carrying *matching_calls* identical calls the shapes select."""
    return build_turn_timeline(
        [
            Turn("user", "chase the delivery"),
            Turn(
                "assistant",
                "working",
                recorded=[
                    recorded_call("http_request", sequence=index) for index in range(matching_calls)
                ],
            ),
        ]
    )


def _verdict(require: dict[str, Any], matching_calls: int) -> bool:
    config = TraceChecksConfig(**_block(_constraint(require)))
    return evaluate_trace_checks(_timeline_of(matching_calls), config).constraints[0].passed


# Zero is in the sweep because it is the count that separates a constant from a
# contingency: a shape true at one, two, three and four matching calls looks like a
# tautology until the empty trajectory answers differently.
_CALLS_SWEPT = (0, 1, 2, 3, 4)


def _config_evading_the_load_rejection(require: dict[str, Any]) -> TraceChecksConfig:
    """The block the evaluator would see had the rejection under test not been written.

    A rejected shape cannot be built through the path that rejects it, so its
    payload is validated on its own — the payload models carry no such rule — and
    only the expression around it is assembled unvalidated. The matchers,
    quantifiers, weights and ``on_missing`` are the ones production would evaluate.
    """
    kind, payload = next(iter(require.items()))
    annotation = TraceConstraintExpr.model_fields[kind].annotation
    payload_model = next(arg for arg in get_args(annotation) if arg is not type(None))
    expression = TraceConstraintExpr.model_construct(**{kind: payload_model(**payload)})
    return TraceChecksConfig.model_construct(
        constraints=[
            TraceConstraint.model_construct(
                id="probe", description="a probe constraint", require=expression
            )
        ]
    )


def _verdicts_over_the_sweep(require: dict[str, Any], *, loadable: bool) -> str:
    """The shape's verdict at each of :data:`_CALLS_SWEPT`, written ``T`` / ``F``."""
    config = (
        TraceChecksConfig(**_block(_constraint(require)))
        if loadable
        else _config_evading_the_load_rejection(require)
    )
    return "".join(
        "T" if evaluate_trace_checks(_timeline_of(count), config).constraints[0].passed else "F"
        for count in _CALLS_SWEPT
    )


# What each self-referential shape actually answers, at zero to four identical
# matching calls, under the default ``on_missing``. Measured against the evaluator
# and pinned here, because the rejection rule's justification is a claim about these
# verdicts and a claim about verdicts is falsifiable. Two rows carry the whole
# argument: ``absent_before_first`` is the one rejected shape that is not a
# constant, and ``immediately_before_first_before_last`` is the one survivor that
# reads "exactly twice" where its three siblings read "at least twice".
_MEASURED_VERDICTS: dict[str, str] = {
    "before_first_before_first": "FFFFF",
    "before_first_before_last": "FFTTT",
    "before_first_before_any": "FFTTT",
    "before_first_before_all": "FFFFF",
    "before_last_before_first": "FFFFF",
    "before_last_before_last": "FFFFF",
    "before_last_before_any": "FFFFF",
    "before_last_before_all": "FFFFF",
    "before_any_before_first": "FFFFF",
    "before_any_before_last": "FFTTT",
    "before_any_before_any": "FFTTT",
    "before_any_before_all": "FFFFF",
    "before_all_before_first": "FFFFF",
    "before_all_before_last": "FFFFF",
    "before_all_before_any": "FFFFF",
    "before_all_before_all": "FFFFF",
    "immediately_before_first_before_first": "FFFFF",
    "immediately_before_first_before_last": "FFTFF",
    "immediately_before_first_before_any": "FFTTT",
    "immediately_before_first_before_all": "FFFFF",
    "immediately_before_last_before_first": "FFFFF",
    "immediately_before_last_before_last": "FFFFF",
    "immediately_before_last_before_any": "FFFFF",
    "immediately_before_last_before_all": "FFFFF",
    "immediately_before_any_before_first": "FFFFF",
    "immediately_before_any_before_last": "FFTTT",
    "immediately_before_any_before_any": "FFTTT",
    "immediately_before_any_before_all": "FFFFF",
    "immediately_before_all_before_first": "FFFFF",
    "immediately_before_all_before_last": "FFFFF",
    "immediately_before_all_before_any": "FFFFF",
    "immediately_before_all_before_all": "FFFFF",
    "absent_before_first": "FTTTT",
    "absent_before_last": "FTFFF",
    "absent_between_first_first": "FFFFF",
    "absent_between_first_last": "FFTFF",
    "absent_between_last_first": "FFFFF",
    "absent_between_last_last": "FFFFF",
}

# The one rejected shape whose verdict the trajectory moves.
_PRESENT_WRITTEN_THE_LONG_WAY_ROUND = "absent_before_first"


def test_the_self_referential_sweep_covers_the_whole_quantifier_cross_product():
    """A shrunken sweep would prove the rule over the cells it kept and no others."""
    assert len(_SELF_REFERENTIAL_CELLS) == _SELF_REFERENTIAL_CELL_COUNT
    surviving = [cell for cell in _SELF_REFERENTIAL_CELLS if cell[2] is not None]
    assert len(surviving) == _SELF_REFERENTIAL_SURVIVOR_COUNT
    assert set(_MEASURED_VERDICTS) == {label for label, _, _ in _SELF_REFERENTIAL_CELLS}


@pytest.mark.parametrize(
    ("label", "require", "witnesses"),
    _SELF_REFERENTIAL_CELLS,
    ids=[label for label, _, _ in _SELF_REFERENTIAL_CELLS],
)
def test_every_self_referential_shape_answers_what_the_sweep_recorded(
    label: str, require: dict[str, Any], witnesses: tuple[int, int] | None
):
    """Rejected or kept, each shape's verdict is measured rather than assumed.

    The rejected rows are the point: ``pytest.raises`` alone proves the load error
    and says nothing about the reason given for it. Evaluating them with the
    rejection stepped over is what makes "this shape is a constant" a claim the
    suite can catch being wrong.
    """
    measured = _verdicts_over_the_sweep(require, loadable=witnesses is not None)

    assert measured == _MEASURED_VERDICTS[label]


def test_the_one_rejected_shape_that_is_not_a_constant_is_present_in_disguise():
    """``absent_before`` at its own ``first`` is rejected for a different reason.

    Twenty-seven of the twenty-eight rejected shapes answer the same thing however
    the agent behaved, which is why an author writing one learns nothing. This one
    answers what ``present`` answers — nothing precedes the first of a matched set,
    so the constraint reduces to "the events occurred at all". It stays rejected as
    pathological authoring, not as a check no trajectory moves, and the ``present``
    column is measured here rather than written down so the two cannot drift.
    """
    rejected = {label for label, _, witnesses in _SELF_REFERENTIAL_CELLS if witnesses is None}
    moved_by_the_trajectory = {
        label for label in rejected if len(set(_MEASURED_VERDICTS[label])) > 1
    }

    assert moved_by_the_trajectory == {_PRESENT_WRITTEN_THE_LONG_WAY_ROUND}
    assert _MEASURED_VERDICTS[_PRESENT_WRITTEN_THE_LONG_WAY_ROUND] == _verdicts_over_the_sweep(
        {"present": {"match": _SELF_REFERENTIAL_MATCH}}, loadable=True
    )


@pytest.mark.parametrize(
    ("require", "witnesses"),
    [(require, witnesses) for _, require, witnesses in _SELF_REFERENTIAL_CELLS],
    ids=[label for label, _, _ in _SELF_REFERENTIAL_CELLS],
)
def test_a_self_referential_shape_loads_only_where_some_trajectory_decides_it(
    require: dict[str, Any], witnesses: tuple[int, int] | None
):
    """Both sides of an ordering selecting one set of events is usually constant.

    Rejecting the shape outright would delete ten satisfiable constraints, and
    admitting it wholesale ships 28 checks whose verdict no agent can move. The line
    runs through the quantifiers, which is why the whole cross-product is swept.
    """
    if witnesses is not None:
        TraceChecksConfig(**_block(_constraint(require)))
        return
    with pytest.raises(ValidationError):
        TraceChecksConfig(**_block(_constraint(require)))


@pytest.mark.parametrize(
    ("require", "witnesses"),
    [(require, w) for _, require, w in _SELF_REFERENTIAL_CELLS if w is not None],
    ids=[label for label, _, w in _SELF_REFERENTIAL_CELLS if w is not None],
)
def test_a_surviving_self_referential_shape_is_decided_by_the_trajectory(
    require: dict[str, Any], witnesses: tuple[int, int]
):
    """Each survivor is contingent, which is the whole reason it survives.

    A survivor no trajectory satisfies would be the constant the rule exists to
    reject, admitted under a different spelling.
    """
    satisfied, refuted = witnesses
    assert _verdict(require, satisfied) is True
    assert _verdict(require, refuted) is False


# Spanning the float domain a weight could be written as, including the denormal
# floor and both zeros: the sweep is over what the model *admits*, so widening the
# domain is what the assertion below catches.
_WEIGHT_DOMAIN = (5e-324, 1e-300, 0.5, 1.0, 1e300, 0.0, -0.0, -1.0, float("inf"), float("nan"))


def test_every_weight_the_model_admits_keeps_the_component_denominator_positive():
    """The fold divides by Σweight with no zero-denominator branch, so it must hold.

    ``Σweight > 0`` over a populated constraint list is a construction invariant
    rather than a checked precondition, and it is one only because every admitted
    weight is positive. Widening the weight domain — admitting a zero, say — makes
    an all-zero weight set reachable and the fold's denominator zero, so the
    invariant is asserted over the domain rather than over the weights packs write.
    """
    admitted = []
    for index, weight in enumerate(_WEIGHT_DOMAIN):
        try:
            config = TraceChecksConfig(
                **_block(
                    _constraint(
                        {"present": {"match": _TOOL_CALL}}, id=f"probe_{index}", weight=weight
                    )
                )
            )
        except ValidationError:
            continue
        admitted.append(config.constraints[0].weight)

    assert len(admitted) == 5, f"the domain admitted {admitted}, not the five positive rows"
    assert min(admitted) > 0.0
    assert sum(admitted) > 0.0


def test_the_shared_block_spans_the_declared_vocabulary():
    """The fixture other tiers read really does cover what they claim it covers."""
    assert set(EVERY_CONSTRAINT_KIND) == TRACE_CONSTRAINT_KINDS

    exercised: set[str] = set()
    for predicate in EVERY_OPERATOR_MATCHER["args"].values():
        exercised |= set(predicate)
    for name in ("tool", "executor", "status", "result"):
        exercised |= set(EVERY_OPERATOR_MATCHER[name])
    assert exercised == TRACE_PREDICATE_OPERATORS, (
        f"the shared matcher exercises {sorted(exercised)}, not every declared operator; "
        f"missing {sorted(TRACE_PREDICATE_OPERATORS - exercised)}"
    )
