"""The comparability table the gate reads before a comparison is ever made.

:func:`ever_satisfiable` answers off two JSON type names what ``operator.eq`` and
:func:`contains` answer off two values, and the table it reads is written by hand.
The only thing that holds a hand-written table honest is running the shipped
operators over real values of each type, which is what the brute-force cell test
does: every cell is one parameter, so a flipped cell names itself.

The comparisons it is measured against are the registered binding operators the
evaluator itself dispatches through — resolved from the
``tolokaforge.trace_check_operators`` entry-point group — rather than a second
copy of that dispatch written here. A table answering for an operator the
evaluator no longer calls answers for nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from itertools import product
from typing import Any

import pytest

from tolokaforge.core.grading.predicates import (
    _EVER_SATISFIABLE,
    JSON_TYPES,
    date_comparison_key,
    ever_satisfiable,
    json_type_of,
)
from tolokaforge.core.plugin_registry import load_trace_check_operator
from tolokaforge.runner.models import TRACE_PREDICATE_BINDING_OPERATORS


def _binding_op(name: str) -> Callable[[Any, Any], bool]:
    """Two-arg view of a registered binding operator, bound through a synthetic name.

    A binding operator dispatches through a name in the constraint's ``bind``
    environment — its signature is ``(value, expected_name, bindings) -> bool``.
    The comparison the operator makes over one bound value is what this table
    holds honest, so the wrapper binds ``expected`` to a fresh key and reads the
    real bound value out of ``bindings``.
    """
    op = load_trace_check_operator(name)
    return lambda value, bound: op(value, "b", {"b": bound})


_BINDING_OPERATORS: Mapping[str, Callable[[Any, Any], bool]] = {
    name: _binding_op(name) for name in TRACE_PREDICATE_BINDING_OPERATORS
}

pytestmark = pytest.mark.unit

# Values per JSON type, chosen so that no cell can be answered "never" by an
# impoverished sample: ``1`` / ``1.0`` / ``True`` are the pairs Python equates
# across the type boundary, and the containers span an empty one, a scalar member,
# a nested list, a nested object and a member of every scalar type, which is what a
# descent has to be given before "unfindable" means anything.
_VALUES_PER_JSON_TYPE: Mapping[str, tuple[Any, ...]] = {
    "string": ("", "a", "abc", "1", "true"),
    "integer": (0, 1, -3),
    "number": (0.0, 1.0, 2.5),
    "boolean": (True, False),
    "array": ([], [1], [1.0], [True], ["abc"], [[1]], [{"a": 1}]),
    "object": ({}, {"a": 1}, {"a": {"b": 2}}, {"a": "abc"}, {"a": [1]}),
}

_CELLS = tuple(
    pytest.param(operator, held, bound, id=f"{operator}-{held}-{bound}")
    for operator in sorted(_BINDING_OPERATORS)
    for held, bound in product(sorted(JSON_TYPES), sorted(JSON_TYPES))
)

_SAMPLES = tuple(
    pytest.param(value_type, value, id=f"{value_type}-{value!r}")
    for value_type, values in _VALUES_PER_JSON_TYPE.items()
    for value in values
)


def _a_satisfying_pair(operator: str, held: str, bound: str) -> tuple[Any, Any] | None:
    """The first sampled pair of these two types the shipped operator holds for."""
    compare = _BINDING_OPERATORS[operator]
    for held_value, bound_value in product(
        _VALUES_PER_JSON_TYPE[held], _VALUES_PER_JSON_TYPE[bound]
    ):
        if compare(held_value, bound_value):
            return (held_value, bound_value)
    return None


@pytest.mark.parametrize(("value_type", "value"), _SAMPLES)
def test_every_sampled_value_reads_as_the_json_type_it_is_filed_under(
    value_type: str, value: Any
) -> None:
    """The brute force is only evidence about a cell if the samples are of its types.

    This is also where ``bool`` before ``int`` is held: ``True`` filed under
    ``boolean`` reads as ``integer`` under the obvious ordering, and every
    ``boolean`` cell would then be measured with an integer.
    """
    assert json_type_of(value) == value_type


@pytest.mark.parametrize(("operator", "held", "bound"), _CELLS)
def test_each_table_cell_agrees_with_the_shipped_operator_over_real_values(
    operator: str, held: str, bound: str
) -> None:
    """One cell of the table against the comparison it answers for.

    A cell claiming "never" is refuted by one satisfying pair; a cell claiming
    "sometimes" is refuted by every sampled pair failing. The asymmetry the gate
    rests on lives in four of these cells and nowhere else: ``contains`` over an
    ``array`` or ``object`` haystack holds against any scalar needle, and against
    no container one.
    """
    pair = _a_satisfying_pair(operator, held, bound)

    assert ever_satisfiable(operator, held, bound) == (pair is not None), pair


@pytest.mark.parametrize(
    "off_domain",
    ["null", "strng", "", None],
    ids=["a-legal-json-schema-type-the-table-omits", "a-typo", "an-empty-type", "untypeable"],
)
def test_a_type_name_the_table_does_not_answer_for_is_no_evidence(off_domain: str | None) -> None:
    """A schema may write any string, and the gate has no false-reject mode.

    ``type: "null"`` is legal JSON Schema, a typo is legal YAML, and ``None`` is a
    property that wrote no single type at all. Every one of them is *no evidence*
    on either side of the comparison — never a mismatch, and never a raise inside
    ``tolokaforge validate``.
    """
    assert ever_satisfiable("equals_binding", off_domain, "integer") is True
    assert ever_satisfiable("equals_binding", "string", off_domain) is True
    assert ever_satisfiable("contains_binding", off_domain, "array") is True
    assert ever_satisfiable("contains_binding", "array", off_domain) is True


@pytest.mark.parametrize(
    "untypeable", [None, object(), (1, 2), {1, 2}], ids=["none", "an-object", "a-tuple", "a-set"]
)
def test_a_value_no_json_type_describes_reads_as_no_evidence(untypeable: Any) -> None:
    """The value side of the same policy.

    A tuple and a set have no JSON reading and ``None`` is a field holding nothing
    at all; answering any of them with a type name would let a caller call a
    comparison impossible on the strength of a value nothing described.
    """
    assert json_type_of(untypeable) is None


def test_the_table_answers_for_exactly_the_operators_a_predicate_may_bind() -> None:
    """A binding operator with no row is a comparison nothing can be checked against.

    The row set is written out rather than comprehended from the model, so this is
    the second source that catches the drift; ``ever_satisfiable`` raises on an
    operator it has no row for rather than answering "no evidence", because that
    gap is the codebase's and not an author's.
    """
    assert set(_EVER_SATISFIABLE) == set(TRACE_PREDICATE_BINDING_OPERATORS)

    with pytest.raises(ValueError, match="is not a binding operator"):
        ever_satisfiable("regex", "string", "string")


# --------------------------------------------------------------------------
# ``date_comparison_key`` — the shared normalization every date check reads.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("2026-03-01", datetime(2026, 3, 1, tzinfo=timezone.utc)),
        ("2026-03-01T12:00:00Z", datetime(2026, 3, 1, 12, tzinfo=timezone.utc)),
        ("2026-03-01T12:00:00+00:00", datetime(2026, 3, 1, 12, tzinfo=timezone.utc)),
        (
            "2026-03-01T12:00:00+02:00",
            datetime(2026, 3, 1, 10, tzinfo=timezone.utc),
        ),
        ("2026-03-01T12:00:00", datetime(2026, 3, 1, 12, tzinfo=timezone.utc)),
        (
            "2026-03-01T12:00:00.123456Z",
            datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=timezone.utc),
        ),
        (
            "2026-03-01T12:00:00.1Z",
            datetime(2026, 3, 1, 12, 0, 0, 100000, tzinfo=timezone.utc),
        ),
    ],
    ids=[
        "date-only-midnight-utc",
        "z-suffix",
        "explicit-utc-offset",
        "east-two-normalized-to-utc",
        "naive-read-as-utc",
        "microseconds",
        "one-fractional-digit-padded",
    ],
)
def test_date_comparison_key_normalizes_an_iso_shape(literal: str, expected: datetime) -> None:
    """Every accepted shape reads through the same UTC datetime.

    Six rows span the policy: a date-only string is midnight UTC of that day;
    the Z suffix and an explicit ``+00:00`` are the same instant; a naive
    datetime reads as UTC; a non-UTC offset converts; a fractional second
    passes through — including the one-digit shape Python 3.10's
    ``fromisoformat`` would reject without the helper's padding.
    """
    assert date_comparison_key(literal) == expected


@pytest.mark.parametrize(
    "value",
    [None, 5, 5.0, True, [], {}, "next week", "March 2026", "2026-13-01", ""],
    ids=[
        "none",
        "integer",
        "number",
        "boolean",
        "empty-list",
        "empty-object",
        "prose",
        "month-only-prose",
        "invalid-month",
        "empty-string",
    ],
)
def test_date_comparison_key_reads_a_non_date_as_no_evidence(value: Any) -> None:
    """A non-string and a string no calendar holds both read as ``None``.

    JSON carries no date type, so ``json_type_of``'s "no evidence" reading
    extends here: a value the helper cannot read is unknown rather than a
    mismatch. The four date operators pair this with a load-time refusal on
    the *bound* side, so a value that reads as ``None`` at runtime is what
    the gate has already covered from the author's side.
    """
    assert date_comparison_key(value) is None
