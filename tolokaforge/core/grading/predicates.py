"""Value predicates shared by every grader that compares an authored value to a real one.

Substrate-neutral and stdlib-only. A comparison written here is the one comparison
the codebase makes, so a `contains` in a JSONPath state check and a `contains` in a
trace check cannot drift into two readings of the same word.

`ever_satisfiable` answers the same question one step earlier, off two JSON type
names rather than two values: whether any pair of values of those types could
satisfy the comparison at all. An authoring gate holding two schemas can then say
"this comparison is false on every trajectory" before the run is paid for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["JSON_TYPES", "contains", "ever_satisfiable", "json_type_of"]

JSON_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "array", "object"}
)
"""The type names a JSON schema's ``type`` may hold that :func:`ever_satisfiable`
answers for. A schema is free to write any string — ``null`` is legal JSON Schema
and a typo is legal YAML — so a name outside this set is one the table has no
answer for rather than one it refuses."""

# Python compares these three across the type boundary: ``bool`` subclasses ``int``
# and an ``int`` equals a ``float`` of the same value, so ``True == 1 == 1.0``.
_NUMERICALLY_COMPARABLE_JSON_TYPES: frozenset[str] = frozenset({"integer", "number", "boolean"})

_SCALAR_JSON_TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean"})

# Per binding operator, the bound types some pair of real values can satisfy it
# against, per held type. Asymmetric for ``contains``: the descent reaches the
# scalars inside a container haystack and never compares a container to the needle,
# so an ``array`` or ``object`` haystack finds any scalar while an ``array`` or
# ``object`` needle is findable in nothing. Written out rather than comprehended
# from the operators, so the brute-force test has two sources to hold against.
_EVER_SATISFIABLE: Mapping[str, Mapping[str, frozenset[str]]] = {
    "equals_binding": {
        "string": frozenset({"string"}),
        "integer": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "number": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "boolean": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "array": frozenset({"array"}),
        "object": frozenset({"object"}),
    },
    "contains_binding": {
        "string": frozenset({"string"}),
        "integer": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "number": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "boolean": _NUMERICALLY_COMPARABLE_JSON_TYPES,
        "array": _SCALAR_JSON_TYPES,
        "object": _SCALAR_JSON_TYPES,
    },
}


def json_type_of(value: Any) -> str | None:
    """The name in :data:`JSON_TYPES` a runtime value reads as, or ``None``.

    ``None`` is *no evidence* — a value no name in the set describes, which every
    caller must treat as unknown and never as a mismatch. ``bool`` is answered
    before ``int``, which it subclasses.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def ever_satisfiable(operator: str, held: str | None, bound: str | None) -> bool:
    """Whether *some* pair of values of these two JSON types satisfies *operator*.

    *held* types the value the predicate reads and *bound* the value the binding
    holds, and the answer is asymmetric: ``contains`` descends into a container
    haystack, so an ``array`` or ``object`` held value finds any scalar, while an
    ``array`` or ``object`` bound value is found in nothing.

    A name outside :data:`JSON_TYPES` on either side — and ``None``, which is a
    schema or a value the caller could not type — answers ``True``: no evidence
    that the pair can never hold, which is the only answer a gate with no
    false-reject mode may give. A caller wanting to report that absence asks
    ``held in JSON_TYPES`` itself.

    An operator this table does not answer for is the codebase's mistake rather
    than an author's, and raises.
    """
    if operator not in _EVER_SATISFIABLE:
        raise ValueError(
            f"{operator!r} is not a binding operator, so no satisfiability table answers "
            f"for it: {sorted(_EVER_SATISFIABLE)}"
        )
    satisfiable_bounds = _EVER_SATISFIABLE[operator]
    if held not in satisfiable_bounds or bound not in JSON_TYPES:
        return True
    return bound in satisfiable_bounds[held]


def contains(haystack: Any, needle: Any, ci: bool = False) -> bool:
    """Whether ``needle`` occurs anywhere in ``haystack``, by recursive descent.

    Two strings compare as a substring; a list, tuple or set holds the needle when
    any element does; a dict when any **value** does — keys are never searched. Two
    values of any other shape compare by equality, so ``contains`` over a scalar is
    ``equals``.

    ``ci`` folds case, and reaches only the string comparison: two non-strings have
    no case to fold.
    """
    if isinstance(haystack, str) and isinstance(needle, str):
        return needle.casefold() in haystack.casefold() if ci else needle in haystack
    if isinstance(haystack, list | tuple | set):
        return any(contains(item, needle, ci=ci) for item in haystack)
    if isinstance(haystack, dict):
        return any(contains(value, needle, ci=ci) for value in haystack.values())
    return haystack == needle
