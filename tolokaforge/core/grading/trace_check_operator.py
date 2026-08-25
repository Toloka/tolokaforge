"""Per-operator seam under the trace-check evaluator.

Every operator a ``ValuePredicate`` may declare is one entry-point in the
``tolokaforge.trace_check_operators`` group. The registry is the sole
dispatch table :func:`~tolokaforge.core.grading.trace_checks._operator_holds`
reads: a downstream package registering ``equals_semver`` under that name
starts scoring every trace-check config in the wild that writes
``equals_semver`` in a :class:`~tolokaforge.core.models.ValuePredicate`,
with no framework PR.

Two arities collapse to one Protocol. The 15 non-binding operators ignore
``bindings``; the two binding operators (identified by the ``_binding``
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
from typing import Any, TypeAlias

from tolokaforge.core.grading.predicates import contains

__all__ = [
    "TraceCheckOperator",
    "contains_binding",
    "contains_ci",
    "contains_op",
    "equals",
    "equals_binding",
    "equals_ci",
    "exists",
    "gt",
    "gte",
    "in_op",
    "len_gt",
    "len_gte",
    "lt",
    "lte",
    "not_equals",
    "not_in_op",
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


def regex_matches(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return isinstance(value, str) and re.search(expected, value) is not None


def gt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.gt)


def gte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.ge)


def lt(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.lt)


def lte(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return _numeric(value, expected, _operator.le)


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
