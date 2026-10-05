"""The gate-default advisory and the model agree on which ``on_missing`` a gate can carry.

Two rules read one ``require`` tree: the model refuses an ``on_missing`` the tree gives
nothing to decide over, and the authoring advisory asks a ``severity: gate`` with no
``on_missing`` to declare one. Where they disagree a gate has no spelling that both
loads and grades as written, so the walk below builds every shape the vocabulary
composes and holds the two against each other on each.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.trace_checks_configs import (
    COMPOSITE_CONSTRAINT_KINDS,
    EVERY_CONSTRAINT_KIND,
    PAYMENT_BINDER,
)
from tolokaforge.core.grading.config_validation import (
    _check_severity_gate_default_on_missing_is_risky,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    OnMissing,
    TraceConstraint,
    TraceConstraintKind,
)

pytestmark = pytest.mark.unit


_LEAVES: dict[str, dict[str, Any]] = {
    kind: require
    for kind, require in EVERY_CONSTRAINT_KIND.items()
    if TraceConstraintKind(kind) not in COMPOSITE_CONSTRAINT_KINDS
}

# ``negate`` holds exactly one expression, so a pair goes under it as an ``all_of``.
_WRAPPERS: dict[str, Callable[[list[dict[str, Any]]], dict[str, Any]]] = {
    "all_of": lambda items: {"all_of": items},
    "any_of": lambda items: {"any_of": items},
    "negate": lambda items: {"negate": items[0] if len(items) == 1 else {"all_of": items}},
}


def test_the_walk_spans_every_leaf_and_every_composite_kind() -> None:
    """A kind added to the vocabulary fails here rather than going unwalked."""
    assert COMPOSITE_CONSTRAINT_KINDS, "no field of TraceConstraintExpr nests expressions"
    assert set(_LEAVES) == {
        kind.value for kind in TRACE_CONSTRAINT_KINDS - COMPOSITE_CONSTRAINT_KINDS
    }
    assert set(_WRAPPERS) == {kind.value for kind in COMPOSITE_CONSTRAINT_KINDS}


def _shapes() -> list[tuple[str, frozenset[str], dict[str, Any]]]:
    """Every leaf alone, every composite over every leaf and pair, and three-deep nests."""
    shapes = [(leaf, frozenset({leaf}), require) for leaf, require in _LEAVES.items()]
    for composite, wrap in _WRAPPERS.items():
        for leaf in _LEAVES:
            shapes.append((f"{composite}[{leaf}]", frozenset({leaf}), wrap([_LEAVES[leaf]])))
        for first, second in itertools.product(_LEAVES, repeat=2):
            shapes.append(
                (
                    f"{composite}[{first},{second}]",
                    frozenset({first, second}),
                    wrap([_LEAVES[first], _LEAVES[second]]),
                )
            )
    for first, second in itertools.product(_LEAVES, repeat=2):
        nested = {"all_of": [{"any_of": [_LEAVES[first]]}, {"negate": _LEAVES[second]}]}
        shapes.append(
            (f"all_of[any_of[{first}],negate[{second}]]", frozenset({first, second}), nested)
        )
    return shapes


def _gate(
    require: dict[str, Any], leaves: frozenset[str], on_missing: str | None
) -> dict[str, Any]:
    constraint: dict[str, Any] = {
        "id": "gate",
        "description": "a gate over the shape",
        "severity": "gate",
        "require": require,
    }
    if on_missing is not None:
        constraint["on_missing"] = on_missing
    # The ``present`` leaf's matcher reads values only this binder puts in scope.
    if "present" in leaves:
        constraint["bind"] = PAYMENT_BINDER
    return constraint


def _admits(require: dict[str, Any], leaves: frozenset[str], policy: OnMissing) -> bool:
    try:
        TraceConstraint.model_validate(_gate(require, leaves, policy.value))
    except ValidationError:
        return False
    return True


_SHAPES = _shapes()


@pytest.mark.parametrize(
    ("leaves", "require"),
    [pytest.param(leaves, require, id=label) for label, leaves, require in _SHAPES],
)
def test_every_gate_shape_has_a_spelling_both_rules_accept(
    leaves: frozenset[str], require: dict[str, Any]
) -> None:
    """The advisory asks for an ``on_missing`` exactly where the model would take one.

    Firing where the model refuses ``on_missing: fail`` leaves the author only
    ``withhold``, which changes grades — or, on a tree holding ``absent``, nothing at
    all; staying silent where the model admits it drops the advisory from the gates
    whose anchor can error.
    """
    defaulted = TraceConstraint.model_validate(_gate(require, leaves, None))
    fires = bool(_check_severity_gate_default_on_missing_is_risky([("gate", defaulted)]).advisories)
    admits_fail = _admits(require, leaves, OnMissing.FAIL)

    assert fires == admits_fail
    if fires:
        assert _admits(require, leaves, OnMissing.WITHHOLD)
