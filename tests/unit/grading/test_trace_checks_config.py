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
from typing import Any

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
    TraceConstraintExpr,
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
        "TraceMatcher",
        "AnchorSide",
        "CountConstraint",
        "ImmediatelyBeforeConstraint",
        "TraceConstraintExpr",
        "TurnWindow",
        "TraceConstraint",
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


_TOOL_CALL = {"kind": "tool_call", "tool": {"equals": "write_file"}}

# One value per operator, in the shape that operator reads. ``exists: False`` and
# ``gt: 0`` are the rows that matter: both are falsy, and a declaredness rule
# reading truthiness rather than presence would drop them.
_OPERATOR_SAMPLES: dict[str, Any] = {
    "equals": "written",
    "equals_ci": "WRITTEN",
    "contains": "writ",
    "contains_ci": "WRIT",
    "not_equals": "deleted",
    "regex": "^writ",
    "gt": 0.0,
    "gte": 1.5,
    "lt": 10.0,
    "lte": 9.5,
    "in_": ["written", "queued"],
    "not_in": ["deleted"],
    "len_gt": 0,
    "len_gte": 2,
    "exists": False,
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
        label="result_without_a_status_predicate",
        block=_block(
            _constraint(
                {"present": {"match": {"kind": "tool_result", "result": {"contains": "ok"}}}}
            )
        ),
        message="#717",
        validator="_require_a_success_status_beside_a_result_predicate",
    ),
    _Rejection(
        label="result_beside_a_non_success_status",
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_result",
                            "status": {"not_equals": "error"},
                            "result": {"contains": "ok"},
                        }
                    }
                }
            )
        ),
        message="#717",
        validator="_require_a_success_status_beside_a_result_predicate",
    ),
    _Rejection(
        label="result_beside_a_two_operator_status",
        block=_block(
            _constraint(
                {
                    "present": {
                        "match": {
                            "kind": "tool_result",
                            "status": {"equals": "success", "exists": True},
                            "result": {"contains": "ok"},
                        }
                    }
                }
            )
        ),
        message="#717",
        validator="_require_a_success_status_beside_a_result_predicate",
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
        validator="_require_distinct_constraint_ids",
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
        message="severity: gate (#680)",
        validator="_require_a_weight_that_scores",
    ),
    _Rejection(
        label="weight_that_is_not_a_number",
        block=_block(_constraint({"present": {"match": _TOOL_CALL}}, weight=float("nan"))),
        message="is not a finite number",
        validator="_require_a_weight_that_scores",
    ),
    _Rejection(
        label="no_constraints",
        block=_block(),
        message="at least 1 item",
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
)


@pytest.mark.parametrize("rejection", _REJECTIONS, ids=[row.label for row in _REJECTIONS])
def test_a_malformed_block_is_rejected_naming_the_fix(rejection: _Rejection):
    with pytest.raises(ValidationError) as excinfo:
        TraceChecksConfig(**rejection.block)

    assert rejection.message in str(excinfo.value), str(excinfo.value)


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


_KINDS_THAT_ANCHOR_NOTHING = frozenset({"present", "absent", "count"})


@pytest.mark.parametrize("kind", sorted(set(EVERY_CONSTRAINT_KIND) - _KINDS_THAT_ANCHOR_NOTHING))
def test_on_missing_is_accepted_on_every_kind_that_anchors_something(kind: str):
    """The complement of the three ``on_missing`` rejection rows above.

    Those rows stay green if the rejection widens to the whole vocabulary, which
    would leave ``on_missing`` unwritable and its default the only reachable
    policy. These seven are the kinds that select an anchor distinct from the thing
    asserted, so the opt-in has something to decide.
    """
    assert len(set(EVERY_CONSTRAINT_KIND) - _KINDS_THAT_ANCHOR_NOTHING) == 7

    config = TraceChecksConfig(
        **_block(_constraint(EVERY_CONSTRAINT_KIND[kind], on_missing="pass"))
    )

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
        # #717 admits a result predicate only beside a success status.
        if "result" in matchable:
            matcher["status"] = {"equals": "success"}
        TraceConstraintExpr(present={"match": matcher})


def test_the_operator_samples_span_the_declared_vocabulary():
    """The table below answers for every operator, so the sweep is not a subset."""
    assert set(_OPERATOR_SAMPLES) == TRACE_PREDICATE_OPERATORS


@pytest.mark.parametrize(("operator", "value"), sorted(_OPERATOR_SAMPLES.items()))
def test_an_operator_is_declared_by_its_value_and_survives_the_wire(operator: str, value: Any):
    """Which operators a predicate asserts must mean the same on both substrates.

    The runner receives this config as JSON inside the trial spec, and dumping a
    model writes every unset field as ``null`` — so reading declaredness off
    pydantic's ``model_fields_set`` would report all fifteen operators after the
    round trip and grade a one-operator predicate as a fifteen-operator conjunction.
    Reading it off the values is what makes the two sides agree.
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
# ``absent_before`` anchored 'last' reads "at most once" — true at one call, false at
# two. ``absent_between`` from first to last reads "exactly twice".
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


def test_the_self_referential_sweep_covers_the_whole_quantifier_cross_product():
    """A shrunken sweep would prove the rule over the cells it kept and no others."""
    assert len(_SELF_REFERENTIAL_CELLS) == _SELF_REFERENTIAL_CELL_COUNT
    surviving = [cell for cell in _SELF_REFERENTIAL_CELLS if cell[2] is not None]
    assert len(surviving) == _SELF_REFERENTIAL_SURVIVOR_COUNT


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
