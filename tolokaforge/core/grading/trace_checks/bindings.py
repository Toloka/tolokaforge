"""Candidate-set enumeration and side-reading reductions for a constraint's binder."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tolokaforge.core.grading.trace_checks.matcher import (
    _MISSING,
    MatcherOutcome,
    _argument_at,
    _missing_evidence,
    _outcome_of,
    _results_by_call_id,
    _unreadable_when_none,
    select_events,
)
from tolokaforge.core.grading.trace_checks.resolver import _restricted
from tolokaforge.core.grading.trace_timeline import TraceEvent, TrialTimeline
from tolokaforge.core.models import (
    AnchorQuantifier,
    BoundValue,
    Quantifier,
    TraceBinding,
    TraceConstraint,
)


@dataclass(frozen=True)
class _Candidates:
    """The assignments a constraint's binder yields, and what it left undetermined.

    ``definite`` are the assignments the trial binds whatever its missing evidence
    would have said; ``undecidable`` are the ones some completion of that evidence
    binds and another does not. ``unnamed`` marks a binder event whose extracted
    value the trial does not record at all — a candidate under some completion, with
    no value this trial can name, so it decides nothing and leaves the fold undecided.

    ``missing`` maps the position of each event that left the set undetermined to the
    evidence the trial does not carry there, and holds only events that contributed:
    one whose membership is unsettled and that carried no value to bind either way
    changes no reading of the set.

    ``emptiness`` separates the two ways a candidate set is empty — no event
    selected, or events selected that carried no value to bind — because they are
    different author mistakes and a single "no match" tells neither.
    """

    definite: list[Mapping[str, Any]]
    undecidable: list[Mapping[str, Any]]
    unnamed: bool
    missing: Mapping[int, frozenset[str]]
    emptiness: str

    @property
    def undetermined(self) -> str:
        """Why the candidate set itself could not be determined, empty when it was.

        Non-empty exactly where the binder left something open — an event whose
        membership the trial cannot settle, or one whose value it does not record.
        """
        if not self.missing:
            return ""
        fields = frozenset[str]().union(*self.missing.values())
        return "the candidate set cannot be determined: " + _missing_evidence(fields, self.missing)


_UNBOUND_ENVIRONMENT: Mapping[str, Any] = {}
"""What a constraint declaring no binder resolves its matchers under."""


def _candidates(timeline: TrialTimeline, constraint: TraceConstraint) -> _Candidates:
    """Every distinct assignment the binder yields, in the order its events occur.

    The binder resolves through :func:`select_events` like every other matcher, so
    it has an undecidable set of its own: an event whose membership the trial cannot
    settle yields a candidate the constraint must be decided *without* assuming, and
    one whose extraction reads unrecorded evidence yields a candidate with no value.
    """
    if constraint.bind is None:
        return _Candidates([_UNBOUND_ENVIRONMENT], [], False, {}, "")
    outcome = _restricted(
        select_events(timeline, constraint.bind.match, _UNBOUND_ENVIRONMENT), constraint.within
    )
    results = _results_by_call_id(timeline)
    bound: list[Mapping[str, Any]] = []
    possible: list[Mapping[str, Any]] = []
    missing: dict[int, frozenset[str]] = {}
    unnamed = False
    for event, settled in _selected(outcome):
        reading = _bound_event(constraint.bind, event, results)
        (bound if settled else possible).extend(reading.assignments)
        unnamed = unnamed or bool(reading.unreadable)
        membership = frozenset() if settled else frozenset(outcome.unreadable_fields)
        # Only an event that contributed puts the set in doubt: one whose membership
        # the binder could not settle and that carried no value to bind either way
        # changes no reading of the set.
        if reading.unreadable or (reading.assignments and not settled):
            missing[event.position] = reading.unreadable | membership
    definite = _distinct(bound)
    undecidable = _distinct(possible, definite)
    return _Candidates(
        definite,
        undecidable,
        unnamed,
        missing,
        "" if definite or undecidable or unnamed else _emptiness(outcome),
    )


def _selected(outcome: MatcherOutcome) -> list[tuple[TraceEvent, bool]]:
    """Every event the binder drew a candidate from, and whether it definitely did."""
    return [
        *((event, True) for event in outcome.matched),
        *((event, False) for event in outcome.undecidable),
    ]


def _distinct(
    assignments: Sequence[Mapping[str, Any]], already: Sequence[Mapping[str, Any]] = ()
) -> list[Mapping[str, Any]]:
    """``assignments`` with the repeats dropped, keeping the first of each.

    Candidates are distinct *values*, not events: ten calls naming one record are
    one reading of the ``require`` tree rather than ten identical ones, and the
    values a failure names are the ones the author has to look at rather than that
    list with duplicates in it. A linear scan rather than a set because an
    assignment is a dict and a bound value may itself be a list or a dict — nothing
    here is hashable.

    ``already`` are assignments the caller has resolved ahead of these, and drops an
    undecidable candidate a definite one already binds: a value the trial definitely
    binds is in the set whatever the missing evidence says, so a second undecidable
    copy of it enumerates a reading already read and names the value twice.
    """
    distinct = list(already)
    kept: list[Mapping[str, Any]] = []
    for assignment in assignments:
        if not any(_same_assignment(assignment, seen) for seen in distinct):
            distinct.append(assignment)
            kept.append(assignment)
    return kept


def _same_assignment(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Whether two assignments bind every name to the same value of the same type.

    Type as well as equality, because ``True == 1`` in Python: equality alone would
    collapse two values the arguments they came from distinguish, and the surviving
    one would then decide the constraint for both. Every assignment one binder
    yields carries the same names, so one side's are enough to compare over.
    """
    return all(type(left[name]) is type(right[name]) and left[name] == right[name] for name in left)


def _emptiness(outcome: MatcherOutcome) -> str:
    selected = len(outcome.matched) + len(outcome.undecidable)
    if not selected:
        return "the binding selected no event"
    label = "event" if selected == 1 else "events"
    return f"the binding selected {selected} {label}, none of which carried every value it extracts"


@dataclass(frozen=True)
class _BoundEvent:
    """What one event the binder selected contributes to the candidate set.

    ``unreadable`` names the evidence some completion of the record would have bound
    a value out of and this trial does not carry. It is non-empty only where
    ``assignments`` is empty, since a name reading nothing empties the product: the
    event is a candidate whose value the trial cannot name, which is missing evidence
    rather than an event carrying no value.
    """

    assignments: list[Mapping[str, Any]]
    unreadable: frozenset[str]


def _bound_event(
    binding: TraceBinding, event: TraceEvent, results: Mapping[str, TraceEvent]
) -> _BoundEvent:
    """One assignment per element of the cross product of the names' extracted values.

    A plain ``field`` reads one value and a ``pattern`` one per capture, so a name
    reading nothing empties the product — the event binds no assignment at all
    rather than an assignment with a hole in it. Which emptiness that is follows
    :func:`_unreadable_when_none`, the same rule a predicate over the field reads:
    an absent argument is an absent value, and a ``result`` on a call the trial
    recorded no outcome for is evidence the trial does not carry.
    """
    outcome = _outcome_of(event, results)
    names = sorted(binding.values)
    extracted = [_extracted(binding.values[name], event, outcome) for name in names]
    unreadable = _unreadable_when_none(outcome).intersection(
        binding.values[name].head_segment()
        for name, values in zip(names, extracted, strict=True)
        if not values
    )
    return _BoundEvent(
        [dict(zip(names, values, strict=True)) for values in itertools.product(*extracted)],
        unreadable,
    )


def _extracted(bound: BoundValue, event: TraceEvent, outcome: TraceEvent | None) -> list[Any]:
    """The values one name reads off one event, in the order the event carries them."""
    value = _binder_reading(bound, event, outcome)
    if value is None:
        return []
    if bound.pattern is None:
        return [value]
    if not isinstance(value, str):
        return []
    return [match.group(1) for match in re.finditer(bound.pattern, value)]


def _binder_reading(bound: BoundValue, event: TraceEvent, outcome: TraceEvent | None) -> Any:
    """The raw value the extraction addresses, before any capture narrows it.

    An extraction reads a JSON ``null`` and an absent key as one condition — the
    call that omitted the argument silences a report a sibling call earned, so
    the :data:`_MISSING` sentinel :func:`_argument_at` returns is folded into
    ``None`` at this boundary and never reaches a bound value.
    """
    head = bound.head_segment()
    if head != "args":
        return _BINDER_FIELDS[head](event, outcome)
    _, _, path = bound.field.partition(".")
    value = _argument_at(event.arguments, path) if path else event.arguments
    return None if value is _MISSING else value


# ``status`` and ``executor`` are on no row: a binding over a closed vocabulary of a
# handful of members is rejected at load, so the extraction never reaches here.
_BINDER_FIELDS: Mapping[str, Callable[[TraceEvent, TraceEvent | None], Any]] = {
    "tool": lambda event, outcome: event.tool_name,
    "text": lambda event, outcome: event.text,
    "result": lambda event, outcome: outcome.result if outcome is not None else None,
}


# One reading of one side: the positions it holds under one completion of the
# undecidable evidence. Empty means the side matched nothing, which is an
# unmatched constraint rather than a verdict.
_Reading = tuple[int, ...]


def _side_readings(
    outcome: MatcherOutcome, quantifier: Quantifier | AnchorQuantifier | None
) -> list[_Reading]:
    """Every position set this side could hold once the missing evidence is known.

    Two readings bound a side quantified ``any`` or ``all`` **once some event
    definitely matches it**: existential quantification is monotone in the matched
    set and universal quantification antitone, whatever relation the constraint
    applies, so the definitely-matched set and the everything-matched set bracket
    every completion between them.

    With nothing definitely matched the bracket breaks, because its bottom is the
    empty reading and an empty side is not a vacuous quantification: it is an
    unmatched anchor, whose verdict ``on_missing`` supplies rather than the
    relation. A reading of one undecidable event can therefore fall outside the two
    ends — the singletons are where a universal reading is largest and an
    existential one smallest — so they are enumerated alongside them.

    A ``first`` / ``last`` side instead **selects** one event, and every
    undecidable event ahead of the earliest (behind the latest) definite match is a
    selection some completion makes. Those are enumerated rather than bracketed,
    because two extremes bound a verdict only where the relation reading the
    selection is monotone in it, and two of the kinds are not: an undecidable event
    landing between two matches turns an unsatisfied ``immediately_before`` into a
    satisfied one, and an undecidable anchor can be the one that makes an
    ``absent_between`` window exist at all rather than widening it.
    """
    matched = tuple(event.position for event in outcome.matched)
    unknown = tuple(event.position for event in outcome.undecidable)
    if quantifier is not None and quantifier in _SELECTING_QUANTIFIERS:
        return _selections(matched, unknown, earliest=quantifier == AnchorQuantifier.FIRST)
    if not unknown:
        return [matched]
    if not matched:
        return [(), *((position,) for position in unknown), tuple(sorted(unknown))]
    return [matched, tuple(sorted(matched + unknown))]


# Both quantifier domains spell these the same, and a ``str`` enum compares and
# hashes as its value, so one set covers a ``MatcherSide`` and an ``AnchorSide``.
_SELECTING_QUANTIFIERS = frozenset({AnchorQuantifier.FIRST, AnchorQuantifier.LAST})


def _selections(matched: _Reading, unknown: _Reading, *, earliest: bool) -> list[_Reading]:
    """Every single event a ``first`` / ``last`` side could end up selecting."""
    if not matched:
        return [(), *((position,) for position in unknown)]
    chosen = min(matched) if earliest else max(matched)
    ahead = [
        position for position in unknown if (position < chosen if earliest else position > chosen)
    ]
    return [(position,) for position in [*ahead, chosen]]
