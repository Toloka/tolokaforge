"""What a ``trace_checks`` block may say, and what it is rejected for saying.

The vocabulary is type-level: no task context, no timeline. So every shape an
author can get wrong is answerable at load, and answering it there is what makes
the evaluator's "a predicate on a ``None`` field is unmatched" rule safe — without
these rejections a typo produces a matcher that selects nothing, which the default
``on_missing`` reports as the agent's failure rather than the author's.

Each row of :data:`_REJECTIONS` is one malformed block and the remediation text its
message must carry. :func:`test_the_rejection_table_names_every_validator_that_raises`
holds the table against the module: a validator that raises a ``ValueError`` no row
provokes is a rejection nothing pins the wording of.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.trace_checks_configs import (
    EVERY_CONSTRAINT_KIND,
    EVERY_OPERATOR_MATCHER,
    every_kind_block,
)
from tolokaforge.core.models import TraceChecksConfig
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
