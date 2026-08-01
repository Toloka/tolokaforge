"""Scoring a pack's trace checks against the trial's event timeline.

Substrate-neutral and pure — no services, no I/O — over the timeline both
substrates build, so a constraint reaches the same verdict whichever substrate
grades the trial.

:func:`select_events` is the **only** function that resolves a matcher. A
constraint reads events through it and nothing else, which is what keeps argument
correlation (#681) a change to one signature. :func:`evaluate_trace_checks` folds
the constraint verdicts into the component score.

Both layers are three-valued. An event either definitely matches, definitely does
not, or cannot be decided because the evidence a predicate reads was never
recorded — and the third case is a state the timeline reaches routinely, on every
bundle re-graded without its tool-call record. Collapsing it into "did not match"
would satisfy every negative constraint in the agent's favour, which
``docs/GRADING.md`` G4 names as the hazard to avoid. A constraint is therefore
decided only when every completion of the undecidable evidence agrees, and an
undecided constraint is a failing sub-check that names what could not be read.

The result carries the author keys this evaluation decomposed, per constraint
kind, for the runner's accounted-keys ledger. Recording them here rather than in
the runner phase is what makes the account honest: a kind the evaluation never
reaches — one nested inside a composite the walk stopped descending into, say —
is never recorded, where a runner-side walk of the *config* would report it as
evaluated whatever the evaluator did.

The authored vocabulary is documented in ``docs/GRADING.md`` § "Trace Checks".
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tolokaforge.core.grading.key_manifest import (
    EVALUATED,
    NO_TIMELINE_EVENTS_SKIP,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
)
from tolokaforge.core.grading.predicates import contains
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import (
    AbsentBeforeConstraint,
    AbsentBetweenConstraint,
    AbsentConstraint,
    AdjacencyView,
    AnchorQuantifier,
    BeforeConstraint,
    CountConstraint,
    ImmediatelyBeforeConstraint,
    KeyAccountingRecord,
    MatcherSide,
    OnMissing,
    PresentConstraint,
    Quantifier,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraint,
    TraceConstraintExpr,
    TraceConstraintKind,
    TraceConstraintResult,
    TraceMatcher,
    TurnWindow,
    ValuePredicate,
)

__all__ = ["MatcherOutcome", "evaluate_trace_checks", "select_events"]


@dataclass(frozen=True)
class MatcherOutcome:
    """What one matcher resolved to on one timeline.

    ``matched`` holds the events that match whatever the missing evidence turns out
    to be, in ascending ``position``. ``undecidable`` holds the events whose every
    other predicate passes but whose record-only evidence the timeline does not
    carry, so some completion of the record would match them and another would not.

    A constraint is decided when every completion of ``undecidable`` yields the same
    verdict, and indeterminate otherwise — so a caller reads both tuples, never
    ``matched`` alone.
    """

    matched: tuple[TraceEvent, ...]
    undecidable: tuple[TraceEvent, ...]
    unreadable_fields: tuple[str, ...]

    @property
    def indeterminate_reason(self) -> str | None:
        """Which evidence is missing and where, or ``None`` when the matcher is decided.

        A property rather than a field: non-``None`` exactly when ``undecidable`` is
        non-empty is the contract callers branch on, and deriving it makes the two
        unable to disagree.
        """
        if not self.undecidable:
            return None
        label = "position" if len(self.undecidable) == 1 else "positions"
        positions = ", ".join(str(event.position) for event in self.undecidable)
        return (
            f"the matcher cannot be decided: the trial records no "
            f"{' or '.join(self.unreadable_fields)} at {label} {positions}"
        )


class _Truth(str, Enum):
    """Whether an event matches, given evidence that may be missing."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def select_events(timeline: TrialTimeline, matcher: TraceMatcher) -> MatcherOutcome:
    """Resolve ``matcher`` against ``timeline``.

    ``kind`` selects the event class and nothing is inferred from which predicates
    are present. A ``tool_call`` matcher reads ``status`` and ``result`` from the
    result paired to it by ``call_id`` — the call event itself carries neither — so
    "a failed call to X with argument Y" is one matcher rather than two.

    A predicate over a ``None`` field is unmatched rather than vacuously true, and
    where that ``None`` means "only the tool-call record could have said, and it did
    not" the event is undecidable instead.
    """
    results = _results_by_call_id(timeline)
    matched: list[TraceEvent] = []
    undecidable: list[TraceEvent] = []
    unreadable: set[str] = set()
    for event in timeline.events:
        if event.kind is not matcher.kind:
            continue
        truth, missing = _resolve(matcher, event, results)
        if truth is _Truth.TRUE:
            matched.append(event)
        elif truth is _Truth.UNKNOWN:
            undecidable.append(event)
            unreadable |= missing
    return MatcherOutcome(tuple(matched), tuple(undecidable), tuple(sorted(unreadable)))


# Fields only the tool-call record supplies. ``None`` in one of them is the
# difference between "the agent did not do that" and "nobody wrote down what
# happened", and a matcher that cannot tell them apart passes every negative
# constraint on a re-graded bundle.
_RECORD_ONLY_FIELDS = frozenset({"executor", "status"})


def _resolve(
    matcher: TraceMatcher, event: TraceEvent, results: Mapping[str, TraceEvent]
) -> tuple[_Truth, frozenset[str]]:
    """Kleene conjunction over the matcher's predicates, and the fields left unread.

    A definitely-failing predicate decides the event whatever the missing evidence
    would have said, so it wins over undecidability however the two are ordered —
    which is what keeps an unexecuted call to a tool the matcher does not name out
    of the undecidable set.
    """
    outcome = _outcome_of(event, results)
    # With no outcome event at all — a call the trial never recorded a result for —
    # the text that call would have returned is unrecorded rather than empty, so a
    # ``result`` predicate is as undecidable there as a ``status`` one.
    unreadable_when_none = (
        _RECORD_ONLY_FIELDS if outcome is not None else _RECORD_ONLY_FIELDS | {"result"}
    )
    unreadable: set[str] = set()
    for field, value, predicate in _predicate_readings(matcher, event, outcome):
        if value is None and field in unreadable_when_none:
            unreadable.add(field)
        elif not _predicate_holds(predicate, value):
            return _Truth.FALSE, frozenset()
    if unreadable:
        return _Truth.UNKNOWN, frozenset(unreadable)
    return _Truth.TRUE, frozenset()


def _predicate_readings(
    matcher: TraceMatcher, event: TraceEvent, outcome: TraceEvent | None
) -> list[tuple[str, Any, ValuePredicate]]:
    """Every declared predicate paired with the value it reads on this event."""
    declared = [
        ("tool", event.tool_name, matcher.tool),
        ("executor", event.executor, matcher.executor),
        ("status", outcome.status if outcome is not None else None, matcher.status),
        ("result", outcome.result if outcome is not None else None, matcher.result),
        ("text", event.text, matcher.text),
    ]
    readings = [
        (field, value, predicate) for field, value, predicate in declared if predicate is not None
    ]
    readings.extend(
        (f"args.{path}", _argument_at(event.arguments, path), predicate)
        for path, predicate in (matcher.args or {}).items()
    )
    return readings


def _outcome_of(event: TraceEvent, results: Mapping[str, TraceEvent]) -> TraceEvent | None:
    """The event a call's outcome is read from: the result paired to it, or itself."""
    if event.kind is not TraceEventKind.TOOL_CALL:
        return event
    return results.get(event.call_id or "")


def _results_by_call_id(timeline: TrialTimeline) -> dict[str, TraceEvent]:
    return {
        event.call_id: event
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_RESULT and event.call_id is not None
    }


def _argument_at(arguments: Mapping[str, Any] | None, path: str) -> Any:
    """The value a dotted argument path addresses, or ``None`` where it does not resolve."""
    value: Any = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _predicate_holds(predicate: ValuePredicate, value: Any) -> bool:
    """Whether every operator the predicate declares holds — it is their conjunction.

    A predicate declaring no operator is rejected at load, so the conjunction is
    never over the empty set and never vacuously true.
    """
    return all(
        _operator_holds(name, value, getattr(predicate, name))
        for name in predicate.declared_operators()
    )


def _operator_holds(name: str, value: Any, expected: Any) -> bool:
    """One operator over one value.

    Only ``exists`` reads a ``None``. Every other operator is false there rather
    than answering about a value the trial does not have — ``not_equals`` against an
    absent argument would otherwise hold, which is the vacuous truth the timeline
    contract forbids.
    """
    if value is None and name != "exists":
        return False
    return _OPERATORS[name](value, expected)


def _as_number(value: Any) -> float | None:
    """``value`` as a number, or ``None`` when it is not one — ``bool`` is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _numeric(compare: Callable[[float, float], bool]) -> Callable[[Any, Any], bool]:
    def holds(value: Any, bound: float) -> bool:
        number = _as_number(value)
        return number is not None and compare(number, bound)

    return holds


def _by_length(compare: Callable[[int, int], bool]) -> Callable[[Any, Any], bool]:
    def holds(value: Any, bound: int) -> bool:
        return isinstance(value, Sized) and compare(len(value), bound)

    return holds


def _equals_ci(value: Any, expected: str) -> bool:
    return isinstance(value, str) and value.casefold() == expected.casefold()


def _matches_regex(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.search(pattern, value) is not None


_OPERATORS: Mapping[str, Callable[[Any, Any], bool]] = {
    "equals": operator.eq,
    "equals_ci": _equals_ci,
    "contains": contains,
    "contains_ci": lambda value, needle: contains(value, needle, ci=True),
    "not_equals": operator.ne,
    "regex": _matches_regex,
    "gt": _numeric(operator.gt),
    "gte": _numeric(operator.ge),
    "lt": _numeric(operator.lt),
    "lte": _numeric(operator.le),
    "in_": lambda value, allowed: value in allowed,
    "not_in": lambda value, rejected: value not in rejected,
    "len_gt": _by_length(operator.gt),
    "len_gte": _by_length(operator.ge),
    "exists": lambda value, expected: (value is not None) is expected,
}


def evaluate_trace_checks(timeline: TrialTimeline, config: TraceChecksConfig) -> TraceChecksResult:
    """Score ``config``'s constraints against ``timeline``.

    A timeline carrying neither a conversational turn nor a tool call is the trial
    that left no trace of itself: no constraint is evaluated and every declared
    kind is accounted as skipped, because every constraint would otherwise score
    against evidence the trial does not carry. A caller reads ``constraints`` to
    tell the two apart — empty is the trial that left no trace.
    """
    if not timeline.events:
        return TraceChecksResult(
            accounted_keys=_accounting(_declared_kinds(config), NO_TIMELINE_EVENTS_SKIP)
        )
    visited: set[TraceConstraintKind] = set()
    results = [
        _evaluate_constraint(timeline, constraint, visited) for constraint in config.constraints
    ]
    return TraceChecksResult(
        passed=all(result.passed for result in results),
        score=_weighted_fraction(results),
        constraints=results,
        accounted_keys=_accounting(visited, EVALUATED),
    )


def _accounting(
    kinds: Iterable[TraceConstraintKind], record: KeyAccountingRecord
) -> dict[str, KeyAccountingRecord]:
    """``record`` filed against the block's key and each kind's own."""
    return {
        TRACE_CONSTRAINTS_KEY: record,
        **{TRACE_CONSTRAINT_KEY_BY_KIND[kind]: record for kind in kinds},
    }


def _declared_kinds(config: TraceChecksConfig) -> set[TraceConstraintKind]:
    """Every kind the block declares, including those nested inside a composite."""
    return {kind for item in config.constraints for kind in _expression_kinds(item.require)}


def _expression_kinds(expr: TraceConstraintExpr) -> set[TraceConstraintKind]:
    """``expr``'s own kind, and recursively those of the expressions it holds.

    Nesting is read off the payload's shape rather than from a second list of
    which kinds compose, so a future composite kind is walked into by existing
    code instead of being silently treated as a leaf.
    """
    kind = expr.declared_kind()
    payload = getattr(expr, kind.value)
    nested = payload if isinstance(payload, list) else [payload]
    kinds = {kind}
    for item in nested:
        if isinstance(item, TraceConstraintExpr):
            kinds |= _expression_kinds(item)
    return kinds


def _weighted_fraction(results: Sequence[TraceConstraintResult]) -> float:
    """``Σ(weight · passed) / Σ(weight)`` over the evaluated constraints.

    Every weight is positive and an evaluated constraint list is non-empty, so the
    denominator is positive by construction. There is deliberately no
    zero-denominator branch: any score it returned would be a convention no author
    chose, and the load-time bound is what removes the need for one.
    """
    total = sum(result.weight for result in results)
    earned = sum(result.weight for result in results if result.passed)
    return earned / total


def _evaluate_constraint(
    timeline: TrialTimeline, constraint: TraceConstraint, visited: set[TraceConstraintKind]
) -> TraceConstraintResult:
    on_missing = constraint.on_missing or OnMissing.FAIL
    resolver = _Resolver(timeline, constraint.within, visited)
    truth = _evaluate(constraint.require, resolver, on_missing)
    kind = constraint.require.declared_kind()
    return TraceConstraintResult(
        id=constraint.id,
        kind=kind,
        passed=truth is _Truth.TRUE,
        weight=constraint.weight,
        message=_message(truth, kind, resolver, on_missing),
        matched_positions=resolver.matched_positions(),
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
    ) -> None:
        self.timeline = timeline
        self.visited_kinds = visited_kinds
        self._within = within
        self._resolved: list[_Resolved] = []

    def resolve(self, label: str, matcher: TraceMatcher, *, anchor: bool) -> MatcherOutcome:
        outcome = _restricted(select_events(self.timeline, matcher), self._within)
        self._resolved.append(_Resolved(label, outcome, anchor))
        return outcome

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


def _restricted(outcome: MatcherOutcome, within: TurnWindow | None) -> MatcherOutcome:
    """``outcome`` with every event outside the constraint's turn window dropped.

    The window narrows what a matcher selects, not what the timeline contains, so
    positions stay the trial's own and an adjacency view is still read over the
    whole trial.
    """
    if within is None:
        return outcome
    matched = tuple(event for event in outcome.matched if _inside(event, within))
    undecidable = tuple(event for event in outcome.undecidable if _inside(event, within))
    return MatcherOutcome(matched, undecidable, outcome.unreadable_fields if undecidable else ())


def _inside(event: TraceEvent, window: TurnWindow) -> bool:
    below = window.first_turn is not None and event.turn_index < window.first_turn
    above = window.last_turn is not None and event.turn_index > window.last_turn
    return not below and not above


def _evaluate(expr: TraceConstraintExpr, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    kind = expr.declared_kind()
    resolver.visited_kinds.add(kind)
    return _HANDLERS[kind](getattr(expr, kind.value), resolver, on_missing)


def _present(payload: PresentConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=True))
    return _decide((None if count == 0 else True for count in counts), on_missing)


def _absent(payload: AbsentConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=False))
    return _decide((count == 0 for count in counts), on_missing)


def _count(payload: CountConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=False))
    return _decide(
        (_within_bounds(count, payload.min, payload.max) for count in counts), on_missing
    )


def _reachable_counts(outcome: MatcherOutcome) -> range:
    """Every match count some completion of the undecidable evidence produces.

    The whole interval, not its two ends: ``count: {min: 1, max: 1}`` over no
    definite matches and two undecidables is satisfied by exactly one of them
    matching, while neither end of ``[0, 2]`` satisfies it — so a rule reading only
    the ends would answer "definitely false" where a completion says otherwise.
    """
    definite = len(outcome.matched)
    return range(definite, definite + len(outcome.undecidable) + 1)


def _within_bounds(count: int, minimum: int | None, maximum: int | None) -> bool:
    return (minimum is None or count >= minimum) and (maximum is None or count <= maximum)


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


def _all_of(
    payload: list[TraceConstraintExpr], resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    return _conjunction(_evaluate(expr, resolver, on_missing) for expr in payload)


def _any_of(
    payload: list[TraceConstraintExpr], resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    return _disjunction(_evaluate(expr, resolver, on_missing) for expr in payload)


def _negate(payload: TraceConstraintExpr, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    return _NEGATED[_evaluate(payload, resolver, on_missing)]


def _conjunction(verdicts: Iterable[_Truth]) -> _Truth:
    seen = set(verdicts)
    if _Truth.FALSE in seen:
        return _Truth.FALSE
    return _Truth.UNKNOWN if _Truth.UNKNOWN in seen else _Truth.TRUE


def _disjunction(verdicts: Iterable[_Truth]) -> _Truth:
    seen = set(verdicts)
    if _Truth.TRUE in seen:
        return _Truth.TRUE
    return _Truth.UNKNOWN if _Truth.UNKNOWN in seen else _Truth.FALSE


_NEGATED: Mapping[_Truth, _Truth] = {
    _Truth.TRUE: _Truth.FALSE,
    _Truth.FALSE: _Truth.TRUE,
    _Truth.UNKNOWN: _Truth.UNKNOWN,
}

_HANDLERS: Mapping[TraceConstraintKind, Callable[[Any, _Resolver, OnMissing], _Truth]] = {
    TraceConstraintKind.PRESENT: _present,
    TraceConstraintKind.ABSENT: _absent,
    TraceConstraintKind.COUNT: _count,
    TraceConstraintKind.BEFORE: _before,
    TraceConstraintKind.IMMEDIATELY_BEFORE: _immediately_before,
    TraceConstraintKind.ABSENT_BEFORE: _absent_before,
    TraceConstraintKind.ABSENT_BETWEEN: _absent_between,
    TraceConstraintKind.ALL_OF: _all_of,
    TraceConstraintKind.ANY_OF: _any_of,
    TraceConstraintKind.NEGATE: _negate,
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


def _decide(values: Iterable[bool | None], on_missing: OnMissing) -> _Truth:
    """The verdict every reachable reading agrees on, or ``UNKNOWN``.

    ``None`` is an unmatched anchor — a question the trial answered by not
    containing the anchor at all, not one the missing record left open — so
    ``on_missing`` resolves it before the readings are compared.
    """
    unmatched_verdict = on_missing is OnMissing.PASS
    agreed = {unmatched_verdict if value is None else value for value in values}
    if agreed == {True}:
        return _Truth.TRUE
    if agreed == {False}:
        return _Truth.FALSE
    return _Truth.UNKNOWN


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
    unmatched = resolver.unmatched_anchors() if on_missing is OnMissing.FAIL else []
    if unmatched:
        return f"{kind.value} is unmatched: {' and '.join(unmatched)} selected no event"
    return f"{kind.value}: {_FAILURE_DETAIL[kind]}"
