"""Window-based absent constraint operators: ``absent_before``, ``absent_between``."""

from __future__ import annotations

from tolokaforge.core.grading.trace_checks.bindings import _Reading, _side_readings
from tolokaforge.core.grading.trace_checks.resolver import _Resolver
from tolokaforge.core.grading.trace_checks.truth import _decide, _Truth
from tolokaforge.core.models import AbsentBeforeConstraint, AbsentBetweenConstraint, OnMissing


def _absent_before(
    payload: AbsentBeforeConstraint, resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    anchors = _side_readings(
        resolver.resolve("anchor", payload.anchor.match, anchor=True), payload.anchor.quantifier
    )
    forbidden = _side_readings(resolver.resolve("forbidden", payload.forbidden, anchor=False), None)
    return _decide(
        (
            _nothing_inside(_prefix_window(anchor), blocked)
            for anchor in anchors
            for blocked in forbidden
        ),
        on_missing,
    )


def _absent_between(
    payload: AbsentBetweenConstraint, resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    starts = _side_readings(
        resolver.resolve("start", payload.start.match, anchor=True), payload.start.quantifier
    )
    ends = _side_readings(
        resolver.resolve("end", payload.end.match, anchor=True), payload.end.quantifier
    )
    forbidden = _side_readings(resolver.resolve("forbidden", payload.forbidden, anchor=False), None)
    return _decide(
        (
            _nothing_inside(_between_window(start, end), blocked)
            for start in starts
            for end in ends
            for blocked in forbidden
        ),
        on_missing,
    )


def _prefix_window(anchor: _Reading) -> tuple[int, int] | None:
    """``[0, anchor)`` as an exclusive interval, or ``None`` where nothing anchors it."""
    return (-1, anchor[0]) if anchor else None


def _between_window(start: _Reading, end: _Reading) -> tuple[int, int] | None:
    """``(start, end)``, or ``None`` where the trial holds no such window.

    An inverted or empty window is unmatched rather than vacuously satisfied: the
    author's anchors did not occur in the declared order, so ``on_missing`` decides
    and defaults to a named failure.
    """
    if not start or not end or start[0] >= end[0]:
        return None
    return (start[0], end[0])


def _nothing_inside(window: tuple[int, int] | None, forbidden: _Reading) -> bool | None:
    if window is None:
        return None
    low, high = window
    return not any(low < position < high for position in forbidden)
