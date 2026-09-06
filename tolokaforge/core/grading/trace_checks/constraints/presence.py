"""Count-based constraint operators: ``present``, ``absent``, ``count``."""

from __future__ import annotations

from tolokaforge.core.grading.trace_checks.matcher import MatcherOutcome
from tolokaforge.core.grading.trace_checks.resolver import _Resolver
from tolokaforge.core.grading.trace_checks.truth import _decide, _Truth
from tolokaforge.core.models import (
    AbsentConstraint,
    CountConstraint,
    OnMissing,
    PresentConstraint,
)


def _present(payload: PresentConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    """No match is this constraint's own ``False``, unless the author opted to withhold.

    The matcher is resolved as an anchor so the grade names which side selected
    nothing. A matcher that yielded no candidate at all — no definite match and
    no undecidable one — is what ``on_missing: withhold`` opts out of: the
    verdict is ``WITHHELD`` and the check leaves numerator and denominator
    alone. With a match or an undecidable candidate the reading falls through
    to the count-based decision, where ``PASS`` and ``FAIL`` are refused at
    load and ``WITHHOLD`` behaves as ``FAIL`` on any completion that reads
    zero.
    """
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=True))
    if on_missing is OnMissing.WITHHOLD and counts == range(0, 1):
        return _Truth.WITHHELD
    return _decide((count > 0 for count in counts), on_missing)


def _absent(payload: AbsentConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=False))
    return _decide((count == 0 for count in counts), on_missing)


def _count(payload: CountConstraint, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    """The count-based verdict, unless the matcher yielded nothing and the author withheld.

    An empty outcome — no definite match, no undecidable one — is what
    ``on_missing: withhold`` opts out of, and the ``min`` / ``max`` bounds are
    irrelevant there: the withhold is on the absence of the anchor, not on
    whether the count falls in bounds. With any candidate the reading falls
    through to the bounds check.
    """
    counts = _reachable_counts(resolver.resolve("match", payload.match, anchor=False))
    if on_missing is OnMissing.WITHHOLD and counts == range(0, 1):
        return _Truth.WITHHELD
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
