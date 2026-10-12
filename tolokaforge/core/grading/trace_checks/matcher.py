"""What one matcher selects from a timeline, under an environment of bound values.

:func:`select_events` is the only function that resolves a matcher; every other
trace-check module reads events through it and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from tolokaforge.core.grading.predicates import ever_satisfiable, json_type_of
from tolokaforge.core.grading.regex_engine import CompiledPatterns, RegexEngineKind
from tolokaforge.core.grading.trace_checks.truth import _Truth
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    TrialTimeline,
)
from tolokaforge.core.models import TraceMatcher, ValuePredicate
from tolokaforge.runner.models import TRACE_PREDICATE_REGEX_OPERATORS


class _Makeability(str, Enum):
    """How one binding comparison read on one event.

    Three-valued rather than two, because a comparison over a value no JSON type
    names is evidence neither way, and reading it as ``MADE`` would let one call
    that omitted the argument silence a report a sibling call earned.
    """

    MADE = "made"
    UNMAKEABLE = "unmakeable"
    NEITHER = "neither"


@dataclass(frozen=True)
class _ComparisonRecord:
    """One binding reference of one matcher, as it read on one candidate event."""

    event: TraceEvent
    field: str
    operator: str
    binding: str
    bound: Any
    state: _Makeability

    __hash__ = None
    """Unhashable, like the :class:`TraceEvent` every record embeds — without this,
    the generated hash could only raise, naming ``TraceEvent`` rather than the
    record."""

    @property
    def reference(self) -> tuple[str, str, str]:
        """What the record is a reading *of*, which repeats across the candidates."""
        return (self.field, self.operator, self.binding)


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

    ``comparisons`` holds how each binding reference the matcher declares read on
    each event that was a candidate for it — the events every *other* reading of the
    matcher admits, so a comparison speaks only for the events it was actually read
    on.
    """

    matched: tuple[TraceEvent, ...]
    undecidable: tuple[TraceEvent, ...]
    unreadable_fields: tuple[str, ...]
    comparisons: tuple[_ComparisonRecord, ...]

    @property
    def unmakeable_comparisons(self) -> tuple[str, ...]:
        """The comparisons no candidate could make, as the sentences the grade prints.

        A reference is reported when at least one candidate could not make its
        comparison and none of them made it, which is the printed sentence's own
        truth: "the comparison was not made" must not be printed on a trajectory
        where some candidate made it. A candidate that read no comparison at all —
        an argument it did not carry — neither reports nor silences, since a call
        that omitted the argument is no evidence that the reference is reachable.
        """
        by_reference: dict[tuple[str, str, str], list[_ComparisonRecord]] = {}
        for record in self.comparisons:
            by_reference.setdefault(record.reference, []).append(record)
        return tuple(
            _unmakeable_message(records[0])
            for records in by_reference.values()
            if any(record.state is _Makeability.UNMAKEABLE for record in records)
            and all(record.state is not _Makeability.MADE for record in records)
        )

    @property
    def indeterminate_reason(self) -> str | None:
        """Which evidence is missing and where, or ``None`` when the matcher is decided.

        A property rather than a field: non-``None`` exactly when ``undecidable`` is
        non-empty is the contract callers branch on, and deriving it makes the two
        unable to disagree.
        """
        if not self.undecidable:
            return None
        return "the matcher cannot be decided: " + _missing_evidence(
            self.unreadable_fields, (event.position for event in self.undecidable)
        )


def _missing_evidence(fields: Iterable[str], positions: Iterable[int]) -> str:
    """Which record-only evidence the trial does not carry, and where.

    One phrasing for both places evidence goes missing — a predicate that could not
    be read and an extraction that could not be read — so the two cannot drift into
    two vocabularies for the same gap.
    """
    where = sorted(positions)
    label = "position" if len(where) == 1 else "positions"
    return (
        f"the trial records no {' or '.join(sorted(fields))} at "
        f"{label} {', '.join(str(position) for position in where)}"
    )


_RegexOperands: TypeAlias = Mapping[tuple[str, str], CompiledPatterns]
"""Each ``regex`` / ``not_regex`` a matcher declares, compiled, keyed by the field
its predicate reads and the operator name."""


def select_events(
    timeline: TrialTimeline,
    matcher: TraceMatcher,
    bindings: Mapping[str, Any],
    *,
    regex_engine: RegexEngineKind,
) -> MatcherOutcome:
    """Resolve ``matcher`` against ``timeline`` under ``bindings``.

    ``kind`` selects the event class and nothing is inferred from which predicates
    are present. A ``tool_call`` matcher reads ``status`` and ``result`` from the
    result paired to it by ``call_id`` — the call event itself carries neither — so
    "a failed call to X with argument Y" is one matcher rather than two.

    A predicate over a ``None`` field is unmatched rather than vacuously true, and
    where that ``None`` means "only the tool-call record could have said, and it did
    not" the event is undecidable instead.

    ``bindings`` is the environment the constraint's binder produced for one
    candidate assignment — the required third argument, so every call site says what
    it resolves under, and empty for a matcher referencing nothing.

    ``regex_engine`` is the block's engine, which a predicate naming its own
    overrides. Every pattern is compiled before any event is read, so one its
    engine refuses raises :class:`~tolokaforge.core.grading.regex_engine.UncompilablePattern`
    on every resolution — not only on a timeline holding an event that reaches it.
    """
    regexes = _regex_operands(matcher, regex_engine)
    results = _results_by_call_id(timeline)
    matched: list[TraceEvent] = []
    undecidable: list[TraceEvent] = []
    unreadable: set[str] = set()
    comparisons: list[_ComparisonRecord] = []
    for event in timeline.events:
        if event.kind is not matcher.kind:
            continue
        truth, missing, records = _resolve(matcher, event, results, bindings, regexes)
        comparisons.extend(records)
        if truth is _Truth.TRUE:
            matched.append(event)
        elif truth is _Truth.UNKNOWN:
            undecidable.append(event)
            unreadable |= missing
    return MatcherOutcome(
        tuple(matched), tuple(undecidable), tuple(sorted(unreadable)), tuple(comparisons)
    )


_RECORD_ONLY_FIELDS = frozenset({"executor", "status"})
"""Fields only the tool-call record supplies. ``None`` in one of them is the
difference between "the agent did not do that" and "nobody wrote down what
happened", and a matcher that cannot tell them apart passes every negative
constraint on a re-graded bundle."""


def _unreadable_when_none(outcome: TraceEvent | None) -> frozenset[str]:
    """The fields whose ``None`` on this event is missing evidence, not an answer.

    With no outcome event at all — a call the trial never recorded a result for —
    the text that call would have returned is unrecorded rather than empty, so a
    ``result`` reading is as undecidable there as a ``status`` one. An outcome that
    exists and carries no ``result`` is a definite absence instead.

    The one rule for both readings of a field: a matcher's predicate over it and a
    binding's extraction out of it.
    """
    if outcome is not None:
        return _RECORD_ONLY_FIELDS
    return _RECORD_ONLY_FIELDS | {"result"}


def _regex_operands(matcher: TraceMatcher, section: RegexEngineKind) -> _RegexOperands:
    """Every pattern the matcher declares, compiled under its predicate's effective engine."""
    return {
        (field, name): CompiledPatterns.compile(
            predicate.patterns_of(name), predicate.regex_engine_under(section)
        )
        for field, predicate in _declared_predicates(matcher)
        for name in sorted(predicate.declared_operators() & TRACE_PREDICATE_REGEX_OPERATORS)
    }


def _resolve(
    matcher: TraceMatcher,
    event: TraceEvent,
    results: Mapping[str, TraceEvent],
    bindings: Mapping[str, Any],
    regexes: _RegexOperands,
) -> tuple[_Truth, frozenset[str], list[_ComparisonRecord]]:
    """What this event decides, what it left unread, and what it compared.

    The verdict is the Kleene conjunction over the matcher's predicates. A
    definitely-failing predicate decides the event whatever the missing evidence
    would have said, so it wins over undecidability however the two are ordered —
    which is what keeps an unexecuted call to a tool the matcher does not name out
    of the undecidable set. A reading whose comparison could not be made is one of
    those definite failures: ``40 == "report.md"`` is false.

    A comparison is recorded on this event only where every *other* reading admits
    it, so an event some other predicate definitely rejects speaks for no comparison
    — which is what keeps a call to a tool the matcher does not name out of the
    candidate set. A reading that made no comparison of its own rejects nothing,
    whether it was refused or read a value no JSON type names: two bad references on
    one matcher would otherwise empty each other's candidate set, and an author who
    wrote two of them would be told about neither.

    A predicate is evaluated only where its result can still change that answer —
    see :func:`_rejections` — so on a call ``tool`` rejects, no ``result`` pattern
    runs. The records keep the matcher's declared order whatever order the
    predicates were evaluated in.
    """
    outcome = _outcome_of(event, results)
    unreadable_when_none = _unreadable_when_none(outcome)
    readings = _predicate_readings(matcher, event, outcome)
    records = [
        _comparison_records(field, None if value is _MISSING else value, predicate, bindings, event)
        for field, value, predicate in readings
    ]
    unread = {
        index
        for index, (field, value, _) in enumerate(readings)
        if value is None and field in unreadable_when_none
    }
    unreadable = {readings[index][0] for index in unread}
    failed, rejecting = _rejections(readings, records, unread, bindings, regexes)
    candidate_records = [
        record for index, found in enumerate(records) if not rejecting - {index} for record in found
    ]
    if failed:
        return _Truth.FALSE, frozenset(), candidate_records
    if unreadable:
        return _Truth.UNKNOWN, frozenset(unreadable), candidate_records
    return _Truth.TRUE, frozenset(), candidate_records


def _rejections(
    readings: list[tuple[str, Any, ValuePredicate]],
    records: list[list[_ComparisonRecord]],
    unread: set[int],
    bindings: Mapping[str, Any],
    regexes: _RegexOperands,
) -> tuple[bool, frozenset[int]]:
    """Whether some reading fails, and the rejecting readings that settle the records.

    A rejecting reading is a failing one whose every comparison was made, a reading
    with no comparison included. The records :func:`_resolve` keeps turn only on the
    rejecting set: all of them with none, the sole rejecter's own with one, none
    with two or more. So once a reading has failed — the verdict is then false —
    only the readings that could still reject are evaluated, and evaluation stops at
    a second rejecter, or at one carrying no records, since nothing a further
    reading says can then change which records are kept. The set returned is exact
    up to that point and answers the same as the full one would.

    Readings are evaluated cheapest first — ``tool``, ``executor`` and ``status``,
    then ``args`` paths, then ``text`` and ``result``, whose values are the
    unbounded ones a pattern scans — and an unread reading is never evaluated.
    """
    failed = False
    rejecting: list[int] = []
    for index in sorted(set(range(len(readings))) - unread, key=lambda i: _cost(readings[i][0])):
        field, value, predicate = readings[index]
        could_reject = all(record.state is _Makeability.MADE for record in records[index])
        if (failed and not could_reject) or _predicate_holds(
            field, predicate, value, bindings, regexes
        ):
            continue
        failed = True
        if could_reject:
            rejecting.append(index)
        if len(rejecting) > 1 or (rejecting and not records[rejecting[0]]):
            break
    return failed, frozenset(rejecting)


_FIELD_COST: Mapping[str, int] = {"tool": 0, "executor": 0, "status": 0, "text": 2, "result": 2}
"""Which of a matcher's event fields :func:`_rejections` reads first; an ``args``
path ranks between the two tiers."""

_ARGS_COST = 1


def _cost(field: str) -> int:
    return _ARGS_COST if field.startswith(_ARGS_PREFIX) else _FIELD_COST[field]


def _comparison_records(
    field: str,
    value: Any,
    predicate: ValuePredicate,
    bindings: Mapping[str, Any],
    event: TraceEvent,
) -> list[_ComparisonRecord]:
    """How each binding reference on this reading compared on this one event.

    A predicate is a conjunction, so both references are read: an author who wrote
    two of them made two mistakes and is owed both.
    """
    references = [(name, getattr(predicate, name)) for name in _binding_operator_names()]
    return [
        _ComparisonRecord(
            event=event,
            field=field,
            operator=name,
            binding=bound_name,
            bound=bindings[bound_name],
            state=_makeability(name, value, bindings[bound_name]),
        )
        for name, bound_name in references
        if bound_name is not None
    ]


def _makeability(operator: str, value: Any, bound: Any) -> _Makeability:
    """Whether this pair of runtime values could have satisfied the comparison.

    The question is asked of both operands' JSON types, over
    :func:`~tolokaforge.core.grading.predicates.ever_satisfiable`'s per-operator
    table — which pairs are false on every trajectory is a property of the pair and
    not of which side holds the text, so a text binding read against a natively-typed
    field gets the same answer as the reverse. A pair the table refuses is
    indistinguishable from an agent failure to everything that reads the score, and
    so recorded as a comparison rather than folded in as one.

    A side no JSON type names — an absent argument, a JSON ``null`` — makes the
    reading ``NEITHER``: the comparison was not made there, and that says nothing
    about whether the reference is reachable. That pre-gate is this caller's own, as
    ``ever_satisfiable`` fails open on a type it cannot name.
    """
    held_type, bound_type = json_type_of(value), json_type_of(bound)
    if held_type is None or bound_type is None:
        return _Makeability.NEITHER
    if ever_satisfiable(operator, held_type, bound_type):
        return _Makeability.MADE
    return _Makeability.UNMAKEABLE


def _unmakeable_message(record: _ComparisonRecord) -> str:
    """What the grade says about a reference no candidate could compare.

    True of the candidate set as a whole rather than of any one event, so it names
    the binding, the value it holds and that value's JSON type, the field and the
    operator — and no held type, which differs across the candidates the sentence
    speaks for. The remedy is one the authoring gate's own rules accept: a capture is
    text only where the field beneath it holds text.
    """
    return (
        f"the {record.field} comparison was not made: binding {record.binding!r} holds "
        f"{record.bound!r}, a JSON {json_type_of(record.bound)}, and no candidate carried a "
        f"value at that field which {record.operator} can ever satisfy against it — two JSON "
        "types the operator cannot pair are false on every trajectory, whichever of the two "
        "holds the text. Reference the binding from an args predicate whose arguments the "
        "tools type the same way, or extract a regex capture off a field that holds text"
    )


_EVENT_FIELDS: Mapping[str, Callable[[TraceEvent, TraceEvent | None], Any]] = {
    "tool": lambda event, outcome: event.tool_name,
    "executor": lambda event, outcome: event.executor,
    "status": lambda event, outcome: outcome.status if outcome is not None else None,
    "result": lambda event, outcome: outcome.result if outcome is not None else None,
    "text": lambda event, outcome: event.text,
}
"""The value each field a matcher predicate reads takes on one event; ``args`` paths
resolve through :func:`_argument_at` instead."""

_ARGS_PREFIX = "args."


def _declared_predicates(matcher: TraceMatcher) -> list[tuple[str, ValuePredicate]]:
    """Every declared predicate and the field it reads, event fields before ``args`` paths."""
    declared = [
        (field, getattr(matcher, field))
        for field in _EVENT_FIELDS
        if getattr(matcher, field) is not None
    ]
    declared.extend(
        (f"{_ARGS_PREFIX}{path}", predicate) for path, predicate in (matcher.args or {}).items()
    )
    return declared


def _predicate_readings(
    matcher: TraceMatcher, event: TraceEvent, outcome: TraceEvent | None
) -> list[tuple[str, Any, ValuePredicate]]:
    """Every declared predicate paired with the value it reads on this event."""
    return [
        (field, _value_read(field, event, outcome), predicate)
        for field, predicate in _declared_predicates(matcher)
    ]


def _value_read(field: str, event: TraceEvent, outcome: TraceEvent | None) -> Any:
    if field.startswith(_ARGS_PREFIX):
        return _argument_at(event.arguments, field.removeprefix(_ARGS_PREFIX))
    return _EVENT_FIELDS[field](event, outcome)


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


_MISSING: Any = object()
"""What an argument path resolves to where the key was never sent — distinct from
JSON ``null``, which resolves to ``None``. Only ``omitted`` reads the difference
between the two; every other operator receives ``None`` for both. The sentinel
never leaves this module: :func:`_operator_holds` reads it directly for
``is_null`` / ``omitted`` and collapses it to ``None`` for every other operator,
:func:`_resolve` collapses it at the tuple boundary before
:func:`_comparison_records` — the one transit path that would otherwise reach
``json_type_of`` — and :func:`~tolokaforge.core.grading.trace_checks.bindings._binder_reading`
collapses it at the extraction boundary."""


def _argument_at(arguments: Mapping[str, Any] | None, path: str) -> Any:
    """The value a dotted argument path addresses.

    Returns the :data:`_MISSING` sentinel where the path does not resolve — an
    ancestor that is not a mapping, or a segment absent from the mapping that
    carries it. Every operator but ``omitted`` reads the sentinel as ``None`` at
    :func:`_operator_holds`'s dispatch gate, so the key-absent path becomes the
    same reading a JSON ``null`` already had. ``omitted`` is what makes the two
    tellable apart, and reads the sentinel directly.
    """
    value: Any = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _predicate_holds(
    field: str,
    predicate: ValuePredicate,
    value: Any,
    bindings: Mapping[str, Any],
    regexes: _RegexOperands,
) -> bool:
    """Whether every operator the predicate declares holds — it is their conjunction.

    A predicate declaring no operator is rejected at load, so the conjunction is
    never over the empty set and never vacuously true. A pattern operator reads its
    compiled form out of ``regexes``; every other operator reads the authored value.
    """
    return all(
        _operator_holds(
            name,
            value,
            (
                regexes[(field, name)]
                if name in TRACE_PREDICATE_REGEX_OPERATORS
                else getattr(predicate, name)
            ),
            bindings,
        )
        for name in predicate.declared_operators()
    )


def _operator_holds(name: str, value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    """One operator over one value.

    ``is_null`` reads whether the field held an explicit JSON ``null``, and
    ``omitted`` whether the key was never sent — the pair whose meaning turns on
    the argument's state before the reading. Both are special-cased ahead of the
    entry-point dispatch: the :data:`_MISSING` sentinel that separates the two
    is private to this module, so the seam cannot answer for them; their
    registered callables are stubs kept only to keep the frozenset and the
    entry-point registry in lockstep. Every other operator reads a ``_MISSING``
    as ``None`` — so a JSON ``null`` and an absent key collapse to one reading
    at the seam and the sentinel never reaches a registered callable.

    Only ``exists`` reads a ``None``. Every other operator is false there rather
    than answering about a value the trial does not have — ``not_equals`` against
    an absent argument would otherwise hold, which is the vacuous truth the
    timeline contract forbids. The gate is a dispatch invariant declared once
    here rather than repeated inside every registered operator.

    ``name`` resolves through the ``tolokaforge.trace_check_operators``
    entry-point group — the only dispatch table this evaluator reads.
    """
    if name == "is_null":
        return (value is None) is expected
    if name == "omitted":
        return (value is _MISSING) is expected
    if value is _MISSING:
        value = None
    if value is None and name != "exists":
        return False
    from tolokaforge.core.plugin_registry import load_trace_check_operator

    op = load_trace_check_operator(name)
    return op(value, expected, bindings)


def _binding_operator_names() -> list[str]:
    """Registered operator names whose semantics substitute a bound value.

    Materialised from the entry-point registry, filtered by the ``_binding``
    suffix — the sole marker for binding operators (ADR-0040). The discovery
    scan is cached in ``plugin_registry``, so a per-call filter is O(N) over
    the registry size (shipped defaults plus downstream) and does not fire
    the loader.
    """
    from tolokaforge.core.plugin_registry import (
        TRACE_CHECK_OPERATORS_GROUP,
        discover_entry_points,
    )

    return sorted(
        n for n in discover_entry_points(TRACE_CHECK_OPERATORS_GROUP) if n.endswith("_binding")
    )
