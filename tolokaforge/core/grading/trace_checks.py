"""Scoring a pack's trace checks against the trial's event timeline.

Substrate-neutral and pure — no services, no I/O — over the timeline both
substrates build, so a constraint reaches the same verdict whichever substrate
grades the trial.

:func:`select_events` is the **only** function that resolves a matcher, and it
resolves one under an environment of bound values. A constraint reads events
through it and nothing else, so a constraint declaring a ``bind`` is the whole
existing evaluator run once per candidate assignment rather than a second
evaluator. :func:`evaluate_trace_checks` folds the constraint verdicts into the
component score.

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

import itertools
import operator
import re
from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tolokaforge.core.grading.key_manifest import (
    EVALUATED,
    NO_TIMELINE_EVENTS_SKIP,
    TRACE_ALTERNATIVES_KEY,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
    UNBOUND_BINDING_SKIP,
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
    BoundValue,
    CountConstraint,
    ImmediatelyBeforeConstraint,
    KeyAccountingRecord,
    MatcherSide,
    OnMissing,
    OnUnbound,
    PresentConstraint,
    Quantifier,
    TraceBinding,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraint,
    TraceConstraintExpr,
    TraceConstraintKind,
    TraceConstraintResult,
    TraceConstraintSeverity,
    TraceMatcher,
    TracePathResult,
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

    ``unmakeable_comparisons`` names the comparisons a bound value's type put out of
    reach, which is a property of the matcher and the environment rather than of any
    one event: a binding reference against a text field is unmakeable on every event
    once the binding holds a non-string.
    """

    matched: tuple[TraceEvent, ...]
    undecidable: tuple[TraceEvent, ...]
    unreadable_fields: tuple[str, ...]
    unmakeable_comparisons: tuple[str, ...]

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


class _Truth(str, Enum):
    """Whether an event matches, given evidence that may be missing."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def select_events(
    timeline: TrialTimeline, matcher: TraceMatcher, bindings: Mapping[str, Any]
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
    """
    results = _results_by_call_id(timeline)
    matched: list[TraceEvent] = []
    undecidable: list[TraceEvent] = []
    unreadable: set[str] = set()
    unmakeable: dict[str, None] = {}
    for event in timeline.events:
        if event.kind is not matcher.kind:
            continue
        truth, missing = _resolve(matcher, event, results, bindings, unmakeable)
        if truth is _Truth.TRUE:
            matched.append(event)
        elif truth is _Truth.UNKNOWN:
            undecidable.append(event)
            unreadable |= missing
    return MatcherOutcome(
        tuple(matched), tuple(undecidable), tuple(sorted(unreadable)), tuple(unmakeable)
    )


# Fields only the tool-call record supplies. ``None`` in one of them is the
# difference between "the agent did not do that" and "nobody wrote down what
# happened", and a matcher that cannot tell them apart passes every negative
# constraint on a re-graded bundle.
_RECORD_ONLY_FIELDS = frozenset({"executor", "status"})


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


def _resolve(
    matcher: TraceMatcher,
    event: TraceEvent,
    results: Mapping[str, TraceEvent],
    bindings: Mapping[str, Any],
    unmakeable: dict[str, None],
) -> tuple[_Truth, frozenset[str]]:
    """Kleene conjunction over the matcher's predicates, and the fields left unread.

    A definitely-failing predicate decides the event whatever the missing evidence
    would have said, so it wins over undecidability however the two are ordered —
    which is what keeps an unexecuted call to a tool the matcher does not name out
    of the undecidable set.

    ``unmakeable`` is filled before that conjunction rather than inside it, keyed by
    message so repeats collapse. An author's type mistake is unconditional, and
    reading it off only the events that survived the other predicates would hide it
    behind whichever predicate happened to fail first.
    """
    outcome = _outcome_of(event, results)
    unreadable_when_none = _unreadable_when_none(outcome)
    readings = _predicate_readings(matcher, event, outcome)
    unmakeable.update(
        dict.fromkeys(
            message
            for field, value, predicate in readings
            for message in _unmakeable_comparisons(field, value, predicate, bindings)
        )
    )
    unreadable: set[str] = set()
    for field, value, predicate in readings:
        if value is None and field in unreadable_when_none:
            unreadable.add(field)
        elif not _predicate_holds(predicate, value, bindings):
            return _Truth.FALSE, frozenset()
    if unreadable:
        return _Truth.UNKNOWN, frozenset(unreadable)
    return _Truth.TRUE, frozenset()


def _unmakeable_comparisons(
    field: str, value: Any, predicate: ValuePredicate, bindings: Mapping[str, Any]
) -> Iterable[str]:
    """Why a binding reference on this reading could not be compared at all.

    Both binding operators read a text field as text: ``contains`` takes two strings
    as a substring pair and falls back to equality for any other pairing, and
    ``equals`` over a string and a non-string is false outright. So a non-string
    bound value against a text field is false on every trajectory whichever operator
    the author wrote — indistinguishable from an agent failure to everything that
    reads the score, and so reported as a comparison rather than folded in as one.

    A predicate is a conjunction, so both references are read: an author who wrote
    two of them made two mistakes and is owed both.
    """
    if not isinstance(value, str):
        return ()
    references = [(name, getattr(predicate, name)) for name in sorted(_BINDING_OPERATORS)]
    return [
        f"the {field} comparison was not made: binding {bound_name!r} holds "
        f"{bindings[bound_name]!r} of type {type(bindings[bound_name]).__name__}, and "
        f"{name} reads a text field as text — a non-string value neither equals a string "
        "nor is found inside one, which is false on every trajectory. Reference the binding "
        "from an args predicate, which compares two arguments as they were written, or bind "
        "a regex capture, which is always text"
        for name, bound_name in references
        if bound_name is not None and not isinstance(bindings[bound_name], str)
    ]


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


def _predicate_holds(predicate: ValuePredicate, value: Any, bindings: Mapping[str, Any]) -> bool:
    """Whether every operator the predicate declares holds — it is their conjunction.

    A predicate declaring no operator is rejected at load, so the conjunction is
    never over the empty set and never vacuously true.
    """
    return all(
        _operator_holds(name, value, getattr(predicate, name), bindings)
        for name in predicate.declared_operators()
    )


def _operator_holds(name: str, value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    """One operator over one value.

    Only ``exists`` reads a ``None``. Every other operator is false there rather
    than answering about a value the trial does not have — ``not_equals`` against an
    absent argument would otherwise hold, which is the vacuous truth the timeline
    contract forbids.

    A binding operator substitutes and delegates: ``expected`` is a name rather than
    a value, and the comparison is the one the literal form makes, so a constraint
    over a single candidate scores exactly as the same constraint with that value
    written out. The name is in the environment by construction — a reference to a
    name the constraint does not bind is a load error.
    """
    if value is None and name != "exists":
        return False
    if name in _BINDING_OPERATORS:
        return _BINDING_OPERATORS[name](value, bindings[expected])
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

# A reference substitutes its bound value into the comparison the literal operator
# already makes, so the two spellings cannot drift into two readings of one word.
_BINDING_OPERATORS: Mapping[str, Callable[[Any, Any], bool]] = {
    "equals_binding": operator.eq,
    "contains_binding": contains,
}


def evaluate_trace_checks(timeline: TrialTimeline, config: TraceChecksConfig) -> TraceChecksResult:
    """Score ``config``'s constraints against ``timeline``.

    A block declaring ``alternatives`` is scored once per route, over the shared
    constraints and that route's own, and the component is the best route's score
    with the winner named. A route is scored as a whole rather than step by step,
    so half of one route plus half of another beats neither.

    A timeline carrying neither a conversational turn nor a tool call is the trial
    that left no trace of itself: no constraint is evaluated and every declared
    kind is accounted as skipped, because every constraint would otherwise score
    against evidence the trial does not carry. A caller reads ``constraints`` to
    tell the two apart — empty is the trial that left no trace.
    """
    if not timeline.events:
        return TraceChecksResult(
            accounted_keys=_accounting(config, _declared_kinds(config), NO_TIMELINE_EVENTS_SKIP)
        )
    ledger = _KindLedger()
    shared = [
        _evaluate_constraint(timeline, constraint, ledger) for constraint in config.constraints
    ]
    if config.alternatives is None:
        return _component(config, _decision_set(shared), ledger, winner_id="", paths=[])
    routes = {
        path.id: _decision_set(
            shared + [_evaluate_constraint(timeline, item, ledger) for item in path.constraints]
        )
        for path in config.alternatives
    }
    winner_id = max(routes, key=lambda path_id: _precedence(routes[path_id]))
    paths = [
        TracePathResult(id=path_id, score=route.score, gate_failed=bool(route.failed_gate_ids))
        for path_id, route in routes.items()
    ]
    return _component(config, routes[winner_id], ledger, winner_id=winner_id, paths=paths)


class _KindLedger:
    """Which constraint kinds the evaluation walked, and which it never entered.

    A kind reaches ``visited`` only by carrying a verdict — its ``require`` tree was
    evaluated, or its constraint scored without entering one because the binder
    yielded no assignment. A walk of the *config* would report a kind as evaluated
    whatever the evaluator did, which is the accounting dishonesty this module exists
    to avoid, and filing a kind the grade *failed the trial on* as skipped is the
    same dishonesty pointing the other way.

    ``unevaluated`` holds the kinds of the constraints whose ``require`` tree was
    never entered, a state a binding that selected nothing reaches. The block files a
    skip for those no constraint scored — so under a skipped tree that is its nested
    kinds, the top-level one having taken the constraint's own verdict.
    """

    def __init__(self) -> None:
        self.visited: set[TraceConstraintKind] = set()
        self.unevaluated: set[TraceConstraintKind] = set()

    def skipped_kinds(self) -> set[TraceConstraintKind]:
        """The kinds no constraint evaluated, which the block accounts as skipped."""
        return self.unevaluated - self.visited


@dataclass(frozen=True)
class _DecisionSet:
    """One route's verdicts — the shared constraints and that route's own — folded.

    ``score`` is the route's own normalised score, before a gate zeroes anything:
    a gate shuts the component, never the number a route earned, and the argmax
    runs over these rather than over the zeroed values so that a route cannot
    escape its own gate by scoring below a clean sibling.
    """

    results: list[TraceConstraintResult]
    score: float
    failed_gate_ids: list[str]


def _precedence(route: _DecisionSet) -> tuple[float, bool]:
    """What the argmax orders routes by: the score, then whether a gate shut.

    ``max`` keeps the first of equal keys and the routes are in declaration order,
    so a tie between clean routes is broken by which one the author wrote first.
    A route whose gate failed wins a tie outright: it can only ever shut the
    component and never rescue it, so the trial's verdict does not turn on where
    in the file the two routes were written.
    """
    return (route.score, bool(route.failed_gate_ids))


def _decision_set(results: list[TraceConstraintResult]) -> _DecisionSet:
    """``results`` scored over its non-gate members, or collapsed to the gate verdict.

    A gate enters neither the numerator nor the denominator, so a decision set whose
    every member is a gate has no weighted average to take — and an author who wrote
    only gates asked for a defined verdict, not an empty sum. It scores the gates'
    own verdict, which is what :func:`~tolokaforge.core.grading.rubric.aggregate_rubric`
    already returns for an all-required rubric. The collapse lives here rather than inside
    :func:`_weighted_fraction`, whose denominator its callers keep positive, and it
    is not conditional on ``alternatives``: a flat block of nothing but gates
    reaches it too.
    """
    scored = [item for item in results if item.severity is not TraceConstraintSeverity.GATE]
    return _DecisionSet(
        results=results,
        score=(
            _weighted_fraction(scored)
            if scored
            else (1.0 if all(item.passed for item in results) else 0.0)
        ),
        failed_gate_ids=[
            item.id
            for item in results
            if item.severity is TraceConstraintSeverity.GATE and not item.passed
        ],
    )


def _component(
    config: TraceChecksConfig,
    winner: _DecisionSet,
    ledger: _KindLedger,
    *,
    winner_id: str,
    paths: list[TracePathResult],
) -> TraceChecksResult:
    """The winning decision set as the component both substrates read.

    A tripped gate zeroes the score here rather than at each substrate's
    integration, because ``TraceChecksResult.score`` *is* the component both
    :meth:`~tolokaforge.core.grading.combine.GradingEngine.grade_trajectory` and the
    runner's ``GradeTrial`` assign — the same act ``GradeTrial`` performs on the
    judge's aggregate when a required criterion fails, done once here for both.
    """
    return TraceChecksResult(
        passed=all(item.passed for item in winner.results),
        score=0.0 if winner.failed_gate_ids else winner.score,
        constraints=winner.results,
        winning_path=winner_id,
        gate_failed=bool(winner.failed_gate_ids),
        failed_gate_ids=winner.failed_gate_ids,
        paths=paths,
        # The skips are filed per kind and never against the block's own keys: a
        # constraint that bound nothing leaves the kinds nested under its ``require``
        # tree unaccounted, while the block itself was evaluated, and a kind any
        # constraint scored keeps that record because ``skipped_kinds`` subtracts it.
        accounted_keys=_accounting(config, ledger.visited, EVALUATED)
        | {
            TRACE_CONSTRAINT_KEY_BY_KIND[kind]: UNBOUND_BINDING_SKIP
            for kind in ledger.skipped_kinds()
        },
    )


def _accounting(
    config: TraceChecksConfig,
    kinds: Iterable[TraceConstraintKind],
    record: KeyAccountingRecord,
) -> dict[str, KeyAccountingRecord]:
    """``record`` filed against the keys the block declares and each kind's own."""
    alternatives = {TRACE_ALTERNATIVES_KEY: record} if config.alternatives is not None else {}
    return {
        TRACE_CONSTRAINTS_KEY: record,
        **alternatives,
        **{TRACE_CONSTRAINT_KEY_BY_KIND[kind]: record for kind in kinds},
    }


def _declared_kinds(config: TraceChecksConfig) -> set[TraceConstraintKind]:
    """Every kind the block declares, shared or inside a path, nesting included."""
    declared = [
        *config.constraints,
        *(item for path in config.alternatives or () for item in path.constraints),
    ]
    return {kind for item in declared for kind in _expression_kinds(item.require)}


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
    """``Σ(weight · passed) / Σ(weight)`` over the scored constraints of a decision set.

    Every weight is positive and the only caller hands this a non-empty set — a
    decision set with no scored member at all is answered by the gate collapse in
    :func:`_decision_set` — so the denominator is positive by construction. There is
    deliberately no zero-denominator branch: any score it returned would be a
    convention no author chose, and keeping the empty case out is what removes the
    need for one.
    """
    total = sum(result.weight for result in results)
    earned = sum(result.weight for result in results if result.passed)
    return earned / total


def _evaluate_constraint(
    timeline: TrialTimeline, constraint: TraceConstraint, ledger: _KindLedger
) -> TraceConstraintResult:
    """One constraint's verdict, its ``require`` tree read once per bound candidate.

    Quantification over the candidates is outermost and universal: the constraint
    holds when it holds under every assignment its binder yields. So ``negate``
    inside a bound constraint reads "no candidate satisfies", not "not every
    candidate does", and a constraint with one candidate scores exactly as the same
    constraint with that value written as a literal.

    The candidate set is itself three-valued, so the fold runs over the completions
    of the undecidable candidates rather than over one set — and a candidate whose
    value the trial does not record is read as ``UNKNOWN`` without entering the
    ``require`` tree, since there is no environment to enter it under.
    """
    kind = constraint.require.declared_kind()
    candidates = _candidates(timeline, constraint)
    on_missing = constraint.on_missing or OnMissing.FAIL
    definite = [
        _read_under(timeline, constraint, environment, ledger.visited, on_missing)
        for environment in candidates.definite
    ]
    possible = [
        _read_under(timeline, constraint, environment, ledger.visited, on_missing)
        for environment in candidates.undecidable
    ]
    if not definite and not possible:
        # The top-level kind is scored whatever the binder yielded — this branch
        # returns a verdict under it — so only the kinds nested beneath it went
        # unreached. Filing the scored one as a skip would report a kind as
        # unevaluated in the same grade that fails the trial on it.
        ledger.visited.add(kind)
        ledger.unevaluated |= _expression_kinds(constraint.require)
        if not candidates.unnamed:
            return _unbound_result(constraint, kind, candidates)
    readings = definite + possible
    unmakeable = list(dict.fromkeys(item for reading in readings for item in reading.unmakeable))
    truth = (
        _Truth.FALSE
        if unmakeable
        else _folded_truth(
            [reading.truth for reading in definite],
            [reading.truth for reading in possible]
            + ([_Truth.UNKNOWN] if candidates.unnamed else []),
            _unbound_truth(constraint.bind),
        )
    )
    return TraceConstraintResult(
        id=constraint.id,
        kind=kind,
        passed=truth is _Truth.TRUE,
        undecided=truth is _Truth.UNKNOWN,
        weight=constraint.weight,
        severity=constraint.severity,
        message=(
            "; ".join(unmakeable)
            if unmakeable
            else _folded_message(readings, truth, kind, candidates.undetermined)
        ),
        matched_positions=sorted({item for reading in readings for item in reading.positions}),
    )


def _folded_truth(
    definite: Sequence[_Truth], undecidable: Sequence[_Truth], unbound: _Truth
) -> _Truth:
    """The verdict every completion of the candidate set agrees on, or ``UNKNOWN``.

    Kleene AND over a set is its minimum under ``FALSE < UNKNOWN < TRUE``, so over
    the completions ``D ∪ S`` for ``S ⊆ U`` the reachable verdicts are exactly those
    of the empty ``S``, of each singleton, and of ``U`` itself — a minimum over any
    larger ``S`` equals one of its members'. Enumerating only the two ends drops the
    singletons, which is sound while the empty reading is vacuously true and wrong
    the moment ``D`` is empty: there the empty reading is ``on_unbound``, and a
    completion binding one satisfied candidate can hold where both ends fail. That
    over-fail is the hazard :func:`_reachable_counts` already names one level down.
    """
    inside = _conjunction(definite)
    empty_reading = inside if definite else unbound
    if not undecidable:
        return empty_reading
    readings = {
        empty_reading,
        *(_conjunction((inside, truth)) for truth in undecidable),
        _conjunction((inside, *undecidable)),
    }
    return readings.pop() if len(readings) == 1 else _Truth.UNKNOWN


@dataclass(frozen=True)
class _CandidateReading:
    """What one constraint's ``require`` tree decided under one bound assignment."""

    environment: Mapping[str, Any]
    truth: _Truth
    message: str
    positions: list[int]
    unmakeable: list[str]


def _read_under(
    timeline: TrialTimeline,
    constraint: TraceConstraint,
    environment: Mapping[str, Any],
    visited: set[TraceConstraintKind],
    on_missing: OnMissing,
) -> _CandidateReading:
    resolver = _Resolver(timeline, constraint.within, visited, environment)
    truth = _evaluate(constraint.require, resolver, on_missing)
    return _CandidateReading(
        environment=environment,
        truth=truth,
        message=_message(truth, constraint.require.declared_kind(), resolver, on_missing),
        positions=resolver.matched_positions(),
        unmakeable=resolver.unmakeable_comparisons(),
    )


def _folded_message(
    readings: list[_CandidateReading],
    truth: _Truth,
    kind: TraceConstraintKind,
    undetermined: str,
) -> str:
    """What the grade says about a constraint folded over its candidates.

    The sentence is the one the first candidate that reached the folded verdict
    wrote — the reading whose truth *is* the folded truth, so a definite failure
    beside an undecided candidate reports the failure rather than the doubt. The
    assignments named beside it are **every** candidate that reached it. Naming them
    is the only way an author learns which record failed to correlate: the per-kind
    detail sentence is the same whichever value it failed under, so a bound
    constraint without them reports a failure the author cannot act on. A constraint
    that binds nothing names none, since its one reading holds the empty
    environment.

    ``undetermined`` is reported only where the completions of the candidate set
    disagreed, as a matcher's own missing evidence is: where they agree the evidence
    the trial lacks changed nothing an author can act on. It is the whole
    explanation where no single candidate reached the verdict, which is the shape a
    set whose completions disagree among themselves takes.
    """
    if truth is _Truth.TRUE:
        return ""
    clause = undetermined if truth is _Truth.UNKNOWN else ""
    responsible = [item for item in readings if item.truth is truth]
    if not responsible:
        return f"{kind.value} cannot be decided — {clause}"
    sentence = responsible[0].message + _naming(responsible, truth)
    return f"{sentence}; {clause}" if clause else sentence


def _naming(readings: list[_CandidateReading], truth: _Truth) -> str:
    named = ", ".join(_assignment_text(item.environment) for item in readings if item.environment)
    return f"; {_FOLD_PHRASE[truth]} {named}" if named else ""


def _assignment_text(environment: Mapping[str, Any]) -> str:
    """One assignment as an author reads it — every name it binds, parenthesised.

    Parenthesised even at one name, so that a constraint binding two names reads
    unambiguously when several assignments are listed side by side.
    """
    return "(" + ", ".join(f"{name}={value!r}" for name, value in sorted(environment.items())) + ")"


# How a fold over candidates reads its own verdict, beside the values it reached it
# under. Neutral about what the constraint asserted, because a ``negate`` fails
# where its correlation *held*. ``TRUE`` is on no row: a passing constraint says
# nothing at all.
_FOLD_PHRASE: Mapping[_Truth, str] = {
    _Truth.FALSE: "failed under",
    _Truth.UNKNOWN: "undecided under",
}


def _unbound_truth(bind: TraceBinding | None) -> _Truth:
    """What the empty reading of the candidate set decides.

    The universal reading is vacuously true over an empty candidate set, so the
    author's ``on_unbound`` supplies the verdict instead — defaulting to a failure,
    because a binder that never fired usually means the agent never did the thing
    the constraint is about. A constraint declaring no binder never reads this: its
    one candidate is the empty environment, so its set is never empty.
    """
    if bind is not None and bind.on_unbound is OnUnbound.PASS:
        return _Truth.TRUE
    return _Truth.FALSE


def _unbound_result(
    constraint: TraceConstraint, kind: TraceConstraintKind, candidates: _Candidates
) -> TraceConstraintResult:
    """The verdict of a bound constraint whose binder yielded no assignment at all."""
    truth = _unbound_truth(constraint.bind)
    passed = truth is _Truth.TRUE
    return TraceConstraintResult(
        id=constraint.id,
        kind=kind,
        passed=passed,
        undecided=truth is _Truth.UNKNOWN,
        weight=constraint.weight,
        severity=constraint.severity,
        message="" if passed else f"{kind.value} is unbound: {candidates.emptiness}",
        matched_positions=[],
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
    """The raw value the extraction addresses, before any capture narrows it."""
    head = bound.head_segment()
    if head != "args":
        return _BINDER_FIELDS[head](event, outcome)
    _, _, path = bound.field.partition(".")
    return _argument_at(event.arguments, path) if path else event.arguments


# ``status`` and ``executor`` are on no row: a binding over a closed vocabulary of a
# handful of members is rejected at load, so the extraction never reaches here.
_BINDER_FIELDS: Mapping[str, Callable[[TraceEvent, TraceEvent | None], Any]] = {
    "tool": lambda event, outcome: event.tool_name,
    "text": lambda event, outcome: event.text,
    "result": lambda event, outcome: outcome.result if outcome is not None else None,
}


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
    return MatcherOutcome(
        matched,
        undecidable,
        outcome.unreadable_fields if undecidable else (),
        outcome.unmakeable_comparisons,
    )


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
