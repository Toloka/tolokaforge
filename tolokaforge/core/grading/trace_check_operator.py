"""Per-operator seam under the trace-check evaluator.

Every operator a ``ValuePredicate`` may declare is one entry-point in the
``tolokaforge.trace_check_operators`` group. The registry is the dispatch
table :func:`~tolokaforge.core.grading.trace_checks._operator_holds` reads
for every operator but the nullness pair: a downstream package registering
``equals_semver`` under that name starts scoring every trace-check config
in the wild that writes ``equals_semver`` in a
:class:`~tolokaforge.core.models.ValuePredicate`, with no framework PR.

``is_null`` and ``omitted`` are the exceptions. Both are special-cased in
``_operator_holds`` ahead of dispatch because their reading turns on a
module-private sentinel that separates JSON ``null`` from key-absence, so
the seam cannot answer for them; the registered callables are stubs kept
only to keep the frozenset and the entry-point registry in lockstep, and a
downstream registration under either name is not reached.

Two arities collapse to one Protocol. Non-binding operators ignore
``bindings``; the binding operators (identified by the ``_binding``
suffix on their registered name) read ``bindings[expected]`` — the name
their author wrote in the constraint's ``bind`` block. The suffix is the
sole marker: no attribute on the callable, no parallel registry.

See ``docs/GRADING.md`` § "Trace checks" for the authored vocabulary and
``docs/GRADER_SERVICE.md`` § "Sub-component plug-in seams" for the seam
row.
"""

from __future__ import annotations

import operator as _operator
import re
from collections.abc import Callable, Mapping, Sized
from datetime import datetime
from typing import Any, TypeAlias

from tolokaforge.core.grading.predicates import contains, date_comparison_key

__all__ = [
    "TraceCheckOperator",
    "contains_binding",
    "contains_ci",
    "contains_op",
    "date_gt",
    "date_gte",
    "date_lt",
    "date_lte",
    "equals",
    "equals_binding",
    "equals_ci",
    "exists",
    "gt",
    "gte",
    "in_op",
    "is_null_stub",
    "len_gt",
    "len_gte",
    "lt",
    "lte",
    "not_contains_op",
    "not_equals",
    "not_in_op",
    "not_regex_matches",
    "omitted_stub",
    "regex_matches",
]

TraceCheckOperator: TypeAlias = Callable[[Any, Any, Mapping[str, Any]], bool]


def equals(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return value == expected


def not_equals(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return value != expected


def equals_ci(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, str) and value.casefold() == expected.casefold()


def contains_op(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return contains(value, expected)


def contains_ci(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return contains(value, expected, ci=True)


def not_contains_op(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return not contains(value, expected)


def regex_matches(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, str) and re.search(expected, value) is not None


def not_regex_matches(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, str) and re.search(expected, value) is None


def gt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.gt)


def gte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.ge)


def lt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.lt)


def lte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.le)


def date_gt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _date_compare(value, expected, _operator.gt)


def date_gte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _date_compare(value, expected, _operator.ge)


def date_lt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _date_compare(value, expected, _operator.lt)


def date_lte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _date_compare(value, expected, _operator.le)


def in_op(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return value in expected


def not_in_op(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return value not in expected


def len_gt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, Sized) and len(value) > expected


def len_gte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, Sized) and len(value) >= expected


def exists(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return (value is not None) is expected


def is_null_stub(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    """Never invoked; ``_operator_holds`` special-cases ``is_null`` ahead of dispatch.

    The registry inventory reads its second source from the entry-point group, so
    every declared operator must resolve to a callable. Reading whether a field
    held an explicit JSON ``null`` turns on the difference between that null and
    an argument the trial never sent, and the sentinel that carries the
    difference is private to :mod:`tolokaforge.core.grading.trace_checks`, so the
    seam cannot answer for it — reaching this callable means the special-case
    gate at the top of ``_operator_holds`` was bypassed.
    """
    raise NotImplementedError(
        "is_null is short-circuited at trace_checks._operator_holds before the "
        "entry-point dispatch. This stub keeps the registered vocabulary in "
        "lockstep with TRACE_PREDICATE_OPERATORS; if execution reached it, the "
        "gate was bypassed"
    )


def omitted_stub(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    """Never invoked; ``_operator_holds`` special-cases ``omitted`` ahead of dispatch.

    Reading whether the key was never sent turns on the same private sentinel
    ``is_null`` reads; the same registry-inventory rule applies. See
    :func:`is_null_stub`.
    """
    raise NotImplementedError(
        "omitted is short-circuited at trace_checks._operator_holds before the "
        "entry-point dispatch. This stub keeps the registered vocabulary in "
        "lockstep with TRACE_PREDICATE_OPERATORS; if execution reached it, the "
        "gate was bypassed"
    )


def equals_binding(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return value == bindings[expected]


def contains_binding(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return contains(value, bindings[expected])


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _numeric(value: Any, expected: Any, compare: Callable[[float, float], bool]) -> bool:
    number = _as_number(value)
    return number is not None and compare(number, expected)


def _date_compare(value: Any, expected: Any, compare: Callable[[datetime, datetime], bool]) -> bool:
    """Two ISO-8601 strings through the shared normalization, or ``False``.

    The bound key check is defensive: :class:`~tolokaforge.runner.models.ValuePredicate`
    already refuses an unparseable bound at load, so a value that reads as
    ``None`` here is what the ``_operator_holds`` gate has already covered for
    every other operator. The check keeps this operator answerable in isolation
    when its bound is called from outside that gate.
    """
    key = date_comparison_key(value)
    bound_key = date_comparison_key(expected)
    return key is not None and bound_key is not None and compare(key, bound_key)
