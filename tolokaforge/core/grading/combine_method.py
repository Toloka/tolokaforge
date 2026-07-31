"""Which aggregation rule ``combine.method`` names, and what each one returns.

Substrate-neutral by construction: stdlib-only pure functions over floats and
plain collections, so the core grading engine and the runner service select the
author's aggregation by one rule instead of each carrying its own branch table.

``weighted_mean`` is an argument rather than a computation here: each substrate
builds its component map by its own inclusion rule and normalises by its own
weights. It is required on every call and read only by the ``weighted`` branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, cast, get_args

CombineMethod = Literal["weighted", "all", "any"]

COMBINE_METHODS: tuple[str, ...] = get_args(CombineMethod)
"""Every aggregation an author may declare, read off :data:`CombineMethod` itself."""

RETIRED_COMBINE_METHOD_ALIASES: Mapping[str, str] = MappingProxyType(
    {"all_pass": "all", "any_pass": "any"}
)
"""Names that were declared but never dispatched, mapped to the rule each one meant."""

_EMPTY_COMPONENT_SCORES = (
    "combine.method aggregates the scored components and none were scored: "
    "min, max and a mean over an empty component map have no answer. A caller "
    "with nothing to aggregate decides its own verdict before combining."
)


def _unsupported_message(value: object, *, context: str) -> str:
    supported = ", ".join(repr(method) for method in COMBINE_METHODS)
    message = f"{context}: {value!r} is not a supported combine method. Choose one of {supported}."
    replacement = RETIRED_COMBINE_METHOD_ALIASES.get(value) if isinstance(value, str) else None
    if replacement is None:
        return message
    return (
        f"{message} {value!r} never worked: it was declared but never dispatched, so "
        f"a trial declaring it was graded by a rule its author did not choose. "
        f"Use {replacement!r}."
    )


def validate_combine_method(value: object, *, context: str) -> CombineMethod:
    """Return ``value`` as a combine method, or raise ``ValueError`` naming ``context``.

    A retired alias is rejected like any other unsupported value, with the rule it
    meant named so the author's fix is one line.
    """
    if isinstance(value, str) and value in COMBINE_METHODS:
        return cast(CombineMethod, value)
    raise ValueError(_unsupported_message(value, context=context))


def combine_by_method(
    *,
    method: CombineMethod,
    component_scores: Mapping[str, float],
    weighted_mean: float,
    pass_threshold: float,
) -> tuple[float, bool]:
    """Fold the scored components into one ``(score, binary_pass)`` by ``method``.

    ``all`` scores the weakest component and passes only if every one clears
    ``pass_threshold``; ``any`` scores the strongest and passes if one does;
    ``weighted`` reports the caller's mean and compares that. Raises ``ValueError``
    on an unsupported method and on an empty ``component_scores``.
    """
    if not component_scores:
        raise ValueError(_EMPTY_COMPONENT_SCORES)
    scores = tuple(component_scores.values())
    if method == "weighted":
        return weighted_mean, weighted_mean >= pass_threshold
    if method == "all":
        return min(scores), all(score >= pass_threshold for score in scores)
    if method == "any":
        return max(scores), any(score >= pass_threshold for score in scores)
    raise ValueError(_unsupported_message(method, context="combine.method"))
