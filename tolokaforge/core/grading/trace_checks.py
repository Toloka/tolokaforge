"""Resolving a trace-check matcher against the trial's event timeline.

Substrate-neutral and pure — no services, no I/O — over the timeline both
substrates build, so a matcher selects the same events whichever substrate grades
the trial.

:func:`select_events` is the **only** function that resolves a matcher. A
constraint reads events through it and nothing else, which is what keeps argument
correlation (#681) a change to one signature.

Resolution is three-valued. An event either definitely matches, definitely does
not, or cannot be decided because the evidence a predicate reads was never
recorded — and the third case is a state the timeline reaches routinely, on every
bundle re-graded without its tool-call record. Collapsing it into "did not match"
would satisfy every negative constraint in the agent's favour, which
``docs/GRADING.md`` G4 names as the hazard to avoid.

The authored vocabulary these predicates come from is documented in
``docs/GRADING.md`` § "Trace Checks".
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tolokaforge.core.grading.predicates import contains
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import TraceMatcher, ValuePredicate

__all__ = ["MatcherOutcome", "select_events"]


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
    for field, value, predicate in _readings(matcher, event, outcome):
        if value is None and field in unreadable_when_none:
            unreadable.add(field)
        elif not _predicate_holds(predicate, value):
            return _Truth.FALSE, frozenset()
    if unreadable:
        return _Truth.UNKNOWN, frozenset(unreadable)
    return _Truth.TRUE, frozenset()


def _readings(
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
