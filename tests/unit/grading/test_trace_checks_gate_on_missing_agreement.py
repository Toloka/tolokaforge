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
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from tests.utils.trace_checks_configs import EVERY_CONSTRAINT_KIND, PAYMENT_BINDER
from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    validate_grading_yaml,
)
from tolokaforge.core.grading.config_validation import (
    AuthoringReport,
    _check_severity_gate_default_on_missing_is_risky,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    OnMissing,
    TraceConstraint,
    TraceConstraintExpr,
    TraceConstraintKind,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_HELPDESK = (
    _REPO / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)
_ADVISORY = "severity: gate with default on_missing: fail"


def _nests_expressions(annotation: Any) -> bool:
    return annotation is TraceConstraintExpr or any(
        _nests_expressions(arg) for arg in get_args(annotation)
    )


_COMPOSITE_KINDS = frozenset(
    kind
    for kind in TRACE_CONSTRAINT_KINDS
    if _nests_expressions(TraceConstraintExpr.model_fields[kind.value].annotation)
)

_LEAVES: dict[str, dict[str, Any]] = {
    kind: require
    for kind, require in EVERY_CONSTRAINT_KIND.items()
    if TraceConstraintKind(kind) not in _COMPOSITE_KINDS
}

# ``negate`` holds exactly one expression, so a pair goes under it as an ``all_of``.
_WRAPPERS: dict[str, Callable[[list[dict[str, Any]]], dict[str, Any]]] = {
    "all_of": lambda items: {"all_of": items},
    "any_of": lambda items: {"any_of": items},
    "negate": lambda items: {"negate": items[0] if len(items) == 1 else {"all_of": items}},
}


def test_the_walk_spans_every_leaf_and_every_composite_kind() -> None:
    """A kind added to the vocabulary fails here rather than going unwalked."""
    assert _COMPOSITE_KINDS, "no field of TraceConstraintExpr nests expressions"
    assert set(_LEAVES) == {kind.value for kind in TRACE_CONSTRAINT_KINDS - _COMPOSITE_KINDS}
    assert set(_WRAPPERS) == {kind.value for kind in _COMPOSITE_KINDS}


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


def _validate(tmp_path: Path, require: dict[str, Any]) -> AuthoringReport:
    grading = {
        "trace_checks": {
            "constraints": [
                {
                    "id": "probe",
                    "description": "a gate the task cannot pass without",
                    "severity": "gate",
                    "require": require,
                }
            ]
        }
    }
    grading_path = tmp_path / "grading.yaml"
    grading_path.write_text(yaml.safe_dump(grading))
    task, task_dir = load_task_yaml(_HELPDESK)
    return validate_grading_yaml(grading_path, inventory=build_tool_inventory(task, task_dir))


def _tool_call(tool: str) -> dict[str, Any]:
    return {"kind": "tool_call", "tool": {"equals": tool}}


_HTTP_THEN_WRITE = {
    "before": {
        "left": {"quantifier": "any", "match": _tool_call("http_request")},
        "right": {"quantifier": "first", "match": _tool_call("write_file")},
    }
}
_A_REFUND_PROMISE = {
    "absent": {"match": {"kind": "assistant_message", "text": {"contains": "refund"}}}
}


@pytest.mark.parametrize(
    "require",
    [
        pytest.param(
            {
                "all_of": [
                    {"present": {"match": _tool_call("http_request")}},
                    {"present": {"match": _tool_call("write_file")}},
                ]
            },
            id="present_only_all_of",
        ),
        pytest.param(
            {
                "all_of": [
                    _A_REFUND_PROMISE,
                    _HTTP_THEN_WRITE,
                    {"present": {"match": _tool_call("http_request")}},
                ]
            },
            id="absent_before_and_present_under_all_of",
        ),
    ],
)
def test_a_composite_gate_refusing_on_missing_fail_loads_with_none(
    tmp_path: Path, require: dict[str, Any]
) -> None:
    """The issue's two gates load as written at the default ``fail_on``."""
    report = _validate(tmp_path, require)

    assert report.advisories == ()


def test_an_anchored_only_composite_gate_still_draws_the_advisory(tmp_path: Path) -> None:
    """Composites are not silenced wholesale: one holding only anchors keeps the advice."""
    with pytest.raises(ValueError, match=_ADVISORY):
        _validate(tmp_path, {"all_of": [_HTTP_THEN_WRITE, _HTTP_THEN_WRITE]})
