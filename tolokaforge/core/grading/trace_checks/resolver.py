"""The per-constraint :class:`_Resolver` and its message-rendering surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tolokaforge.core.grading.trace_checks.matcher import MatcherOutcome, select_events
from tolokaforge.core.grading.trace_checks.truth import _Truth
from tolokaforge.core.grading.trace_timeline import TraceEvent, TrialTimeline
from tolokaforge.core.models import (
    OnMissing,
    TraceConstraintKind,
    TraceMatcher,
    TurnWindow,
)


@dataclass(frozen=True)
class _Resolved:
    """One matcher of one constraint, and what it selected.

    ``anchor`` marks the positions whose emptiness is an *unmatched* constraint
    rather than an answer: a `before` side that selected nothing leaves the
    ordering unasked, while an `absent` matcher selecting nothing is the very thing
    it asserts.
    """

    label: str
    outcome: MatcherOutcome
    anchor: bool


class _Resolver:
    """Resolves one constraint's matchers, remembering what each one found.

    ``visited_kinds`` is the block's accumulator rather than this constraint's:
    every expression the evaluation reaches adds its kind to it, and the block's
    accounting is what the set holds once every constraint has been evaluated.
    """

    def __init__(
        self,
        timeline: TrialTimeline,
        within: TurnWindow | None,
        visited_kinds: set[TraceConstraintKind],
        bindings: Mapping[str, Any],
    ) -> None:
        self.timeline = timeline
        self.visited_kinds = visited_kinds
        self._within = within
        self._bindings = bindings
        self._resolved: list[_Resolved] = []

    def resolve(self, label: str, matcher: TraceMatcher, *, anchor: bool) -> MatcherOutcome:
        outcome = _restricted(select_events(self.timeline, matcher, self._bindings), self._within)
        self._resolved.append(_Resolved(label, outcome, anchor))
        return outcome

    def unmakeable_comparisons(self) -> list[str]:
        """Every comparison a bound value's type put out of reach, in matcher order."""
        return list(
            dict.fromkeys(
                message
                for item in self._resolved
                for message in item.outcome.unmakeable_comparisons
            )
        )

    def matched_positions(self) -> list[int]:
        return sorted({event.position for item in self._resolved for event in item.outcome.matched})

    def undecided(self) -> list[str]:
        """Why each matcher that could not be decided could not be decided."""
        return [
            f"{item.label}: {item.outcome.indeterminate_reason}"
            for item in self._resolved
            if item.outcome.indeterminate_reason is not None
        ]

    def unmatched_anchors(self) -> list[str]:
        return [
            item.label
            for item in self._resolved
            if item.anchor and not item.outcome.matched and not item.outcome.undecidable
        ]

    def empty_sides(self) -> list[str]:
        """Every resolved matcher whose outcome carried no event, anchor or not.

        Reads the withheld verdict on ``count`` — whose matcher is not an
        anchor (its empty count is a defined verdict) but whose ``WITHHELD``
        under ``on_missing: withhold`` still names the same side.
        """
        return [
            item.label
            for item in self._resolved
            if not item.outcome.matched and not item.outcome.undecidable
        ]


def _restricted(outcome: MatcherOutcome, within: TurnWindow | None) -> MatcherOutcome:
    """``outcome`` with every event outside the constraint's turn window dropped.

    The window narrows what a matcher selects, not what the timeline contains, so
    positions stay the trial's own and an adjacency view is still read over the
    whole trial. It narrows the comparisons the same way, so a reference reduces
    over the candidates the author's window admits and a call outside it neither
    reports nor silences.
    """
    if within is None:
        return outcome
    matched = tuple(event for event in outcome.matched if _inside(event, within))
    undecidable = tuple(event for event in outcome.undecidable if _inside(event, within))
    return MatcherOutcome(
        matched,
        undecidable,
        outcome.unreadable_fields if undecidable else (),
        tuple(record for record in outcome.comparisons if _inside(record.event, within)),
    )


def _inside(event: TraceEvent, window: TurnWindow) -> bool:
    below = window.first_turn is not None and event.turn_index < window.first_turn
    above = window.last_turn is not None and event.turn_index > window.last_turn
    return not below and not above


# Why a definite failure failed, per kind — the sentence a task author reads
# beside the constraint's id in the grade.
_FAILURE_DETAIL: Mapping[TraceConstraintKind, str] = {
    TraceConstraintKind.PRESENT: "no event matched",
    TraceConstraintKind.ABSENT: "an event matched",
    TraceConstraintKind.COUNT: "the number of matching events is outside the declared bounds",
    TraceConstraintKind.BEFORE: (
        "no match is ordered before the other side under the declared quantifiers"
    ),
    TraceConstraintKind.IMMEDIATELY_BEFORE: (
        "no match is immediately followed by the other side in that view"
    ),
    TraceConstraintKind.ABSENT_BEFORE: "a forbidden event occurs before the anchor",
    TraceConstraintKind.ABSENT_BETWEEN: "a forbidden event occurs inside the window",
    TraceConstraintKind.ALL_OF: "a nested expression does not hold",
    TraceConstraintKind.ANY_OF: "no nested expression holds",
    TraceConstraintKind.NEGATE: "the negated expression holds",
}


def _message(
    truth: _Truth, kind: TraceConstraintKind, resolver: _Resolver, on_missing: OnMissing
) -> str:
    """What the grade says about this constraint — empty when it passed."""
    if truth is _Truth.TRUE:
        return ""
    if truth is _Truth.UNKNOWN:
        return f"{kind.value} cannot be decided — " + "; ".join(resolver.undecided())
    if truth is _Truth.WITHHELD:
        empty = resolver.empty_sides()
        return f"{kind.value} withheld: {' and '.join(empty)} selected no event"
    unmatched = resolver.unmatched_anchors() if on_missing is OnMissing.FAIL else []
    if unmatched:
        return f"{kind.value} is unmatched: {' and '.join(unmatched)} selected no event"
    return f"{kind.value}: {_FAILURE_DETAIL[kind]}"
