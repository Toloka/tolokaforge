"""Pairwise ordering constraint operators: ``before``, ``immediately_before``."""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable, Mapping

from tolokaforge.core.grading.trace_checks.bindings import _Reading, _side_readings
from tolokaforge.core.grading.trace_checks.resolver import _Resolver
from tolokaforge.core.grading.trace_checks.truth import _decide, _Truth
from tolokaforge.core.grading.trace_timeline import TraceEventKind, TrialTimeline
from tolokaforge.core.models import (
    AdjacencyView,
    BeforeConstraint,
    ImmediatelyBeforeConstraint,
    MatcherSide,
    OnMissing,
    Quantifier,
)


def _before(payload: BeforeConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    return _ordering(payload.left, payload.right, resolver, on_missing, operator.lt)


def _immediately_before(
    payload: ImmediatelyBeforeConstraint, resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    adjacent = _adjacency(resolver.timeline, payload.among)
    return _ordering(payload.left, payload.right, resolver, on_missing, adjacent)


def _ordering(
    left: MatcherSide,
    right: MatcherSide,
    resolver: _Resolver,
    on_missing: OnMissing,
    relation: Callable[[int, int], bool],
) -> _Truth:
    lefts = _side_readings(resolver.resolve("left", left.match, anchor=True), left.quantifier)
    rights = _side_readings(resolver.resolve("right", right.match, anchor=True), right.quantifier)
    return _decide(
        (
            _relation_holds(chosen_left, left.quantifier, chosen_right, right.quantifier, relation)
            for chosen_left in lefts
            for chosen_right in rights
        ),
        on_missing,
    )


def _relation_holds(
    left: _Reading,
    left_quantifier: Quantifier,
    right: _Reading,
    right_quantifier: Quantifier,
    relation: Callable[[int, int], bool],
) -> bool | None:
    """Whether the relation holds over this pair of readings, quantifier by quantifier.

    ``first`` / ``last`` have already reduced their side to one position, so an
    existential and a universal reading of it answer the same.
    """
    if not left or not right:
        return None
    return _quantified(
        left_quantifier,
        (
            _quantified(right_quantifier, (relation(position, other) for other in right))
            for position in left
        ),
    )


def _quantified(quantifier: Quantifier, values: Iterable[bool]) -> bool:
    return all(values) if quantifier is Quantifier.ALL else any(values)


_VIEW_KINDS: Mapping[AdjacencyView, frozenset[TraceEventKind]] = {
    AdjacencyView.TOOL_CALLS: frozenset({TraceEventKind.TOOL_CALL}),
    AdjacencyView.TOOL_RESULTS: frozenset({TraceEventKind.TOOL_RESULT}),
    AdjacencyView.MESSAGES: frozenset(
        {TraceEventKind.ASSISTANT_MESSAGE, TraceEventKind.USER_MESSAGE}
    ),
    AdjacencyView.EVENTS: frozenset(TraceEventKind),
}


def _adjacency(timeline: TrialTimeline, among: AdjacencyView) -> Callable[[int, int], bool]:
    """Whether one position immediately precedes another in the named view.

    An event the view does not contain is adjacent to nothing: ``among:
    tool_calls`` over a matched message names a sequence that message has no place
    in.
    """
    kinds = _VIEW_KINDS[among]
    ranks = {
        event.position: rank
        for rank, event in enumerate(event for event in timeline.events if event.kind in kinds)
    }

    def immediately_precedes(left: int, right: int) -> bool:
        if left not in ranks or right not in ranks:
            return False
        return ranks[right] == ranks[left] + 1

    return immediately_precedes
