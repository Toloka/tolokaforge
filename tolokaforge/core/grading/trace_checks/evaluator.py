"""The top-level evaluator: score fold, multi-path scoring, severity gates.

:func:`evaluate_trace_checks` folds constraint verdicts into the component score
both substrates read; :func:`_evaluate` dispatches one expression through the
:data:`_HANDLERS` registry the constraint package populates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tolokaforge.core.grading.key_manifest import (
    EVALUATED,
    NO_TIMELINE_EVENTS_SKIP,
    TRACE_ALTERNATIVES_KEY,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
    UNBOUND_BINDING_SKIP,
)
from tolokaforge.core.grading.trace_checks.bindings import _Candidates, _candidates
from tolokaforge.core.grading.trace_checks.dispatch import _HANDLERS
from tolokaforge.core.grading.trace_checks.resolver import _message, _Resolver
from tolokaforge.core.grading.trace_checks.truth import _conjunction, _Truth
from tolokaforge.core.grading.trace_timeline import TrialTimeline
from tolokaforge.core.models import (
    KeyAccountingRecord,
    OnMissing,
    OnUnbound,
    TraceBinding,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraint,
    TraceConstraintExpr,
    TraceConstraintKind,
    TraceConstraintResult,
    TraceConstraintSeverity,
    TracePathResult,
)


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
    """``results`` scored over its non-gate, non-withheld members, or collapsed to the gate verdict.

    A gate enters neither the numerator nor the denominator, so a decision set whose
    every member is a gate has no weighted average to take — and an author who wrote
    only gates asked for a defined verdict, not an empty sum. It scores the gates'
    own verdict, which is what :func:`~tolokaforge.core.grading.rubric.aggregate_rubric`
    already returns for an all-required rubric. The collapse lives here rather than inside
    :func:`_weighted_fraction`, whose denominator its callers keep positive, and it
    is not conditional on ``alternatives``: a flat block of nothing but gates
    reaches it too.

    A withheld constraint is out of the decision on both sides: excluded from the
    numerator and denominator of the weighted fraction, from the all-gates collapse,
    and from ``failed_gate_ids`` — a withheld gate is neither passing nor failing.
    """
    scored = [
        item
        for item in results
        if item.severity is not TraceConstraintSeverity.GATE and not item.withheld
    ]
    decided = [item for item in results if not item.withheld]
    return _DecisionSet(
        results=results,
        score=(
            _weighted_fraction(scored)
            if scored
            else (1.0 if all(item.passed for item in decided) else 0.0)
        ),
        failed_gate_ids=[
            item.id
            for item in results
            if item.severity is TraceConstraintSeverity.GATE
            and not item.passed
            and not item.withheld
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
        passed=all(item.passed for item in winner.results if not item.withheld),
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
    return {kind for item in declared for kind in item.require.kinds_in_tree()}


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
        ledger.unevaluated |= constraint.require.kinds_in_tree()
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
    withheld = truth is _Truth.WITHHELD
    return TraceConstraintResult(
        id=constraint.id,
        kind=kind,
        passed=truth is _Truth.TRUE,
        undecided=truth is _Truth.UNKNOWN,
        withheld=withheld,
        weight=constraint.weight,
        severity=constraint.severity,
        message=(
            "; ".join(unmakeable)
            if unmakeable
            else _folded_message(readings, truth, kind, candidates.undetermined)
        ),
        matched_positions=(
            [] if withheld else sorted({item for reading in readings for item in reading.positions})
        ),
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
    _Truth.WITHHELD: "withheld under",
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


def _evaluate(expr: TraceConstraintExpr, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    kind = expr.declared_kind()
    resolver.visited_kinds.add(kind)
    return _HANDLERS[kind](getattr(expr, kind.value), resolver, on_missing)
