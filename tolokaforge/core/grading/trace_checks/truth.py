"""Kleene truth primitives the trace-checks package folds constraint verdicts over."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

from tolokaforge.core.models import OnMissing


class _Truth(str, Enum):
    """Whether an event matches, given evidence that may be missing.

    ``WITHHELD`` names the author-opted-out verdict :class:`OnMissing.WITHHOLD`
    yields on an unmatched anchor. It is contagious under composites — an
    ``all_of`` or ``any_of`` a withheld branch enters withholds too, unless a
    definite verdict beats it (``FALSE`` under conjunction, ``TRUE`` under
    disjunction), so an author writing ``all_of: [genuinely_failing,
    withheld_thing]`` still learns which branch failed.
    """

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"
    WITHHELD = "withheld"


def _conjunction(verdicts: Iterable[_Truth]) -> _Truth:
    seen = set(verdicts)
    if _Truth.FALSE in seen:
        return _Truth.FALSE
    if _Truth.WITHHELD in seen:
        return _Truth.WITHHELD
    return _Truth.UNKNOWN if _Truth.UNKNOWN in seen else _Truth.TRUE


def _disjunction(verdicts: Iterable[_Truth]) -> _Truth:
    seen = set(verdicts)
    if _Truth.TRUE in seen:
        return _Truth.TRUE
    if _Truth.WITHHELD in seen:
        return _Truth.WITHHELD
    return _Truth.UNKNOWN if _Truth.UNKNOWN in seen else _Truth.FALSE


def _decide(values: Iterable[bool | None], on_missing: OnMissing) -> _Truth:
    """The verdict every reachable reading agrees on, or ``UNKNOWN``.

    ``None`` is an unmatched anchor — a question the trial answered by not
    containing the anchor at all, not one the missing record left open — so
    ``on_missing`` resolves it before the readings are compared. Under
    ``OnMissing.WITHHOLD`` a single unmatched anchor withholds the whole reading;
    with no unmatched anchor at all, ``WITHHOLD`` reads as ``FAIL`` — the
    ordering or adjacency was decidable.
    """
    readings = list(values)
    if on_missing is OnMissing.WITHHOLD and any(value is None for value in readings):
        return _Truth.WITHHELD
    unmatched_verdict = on_missing is OnMissing.PASS
    agreed = {unmatched_verdict if value is None else value for value in readings}
    if agreed == {True}:
        return _Truth.TRUE
    if agreed == {False}:
        return _Truth.FALSE
    return _Truth.UNKNOWN


_NEGATED: Mapping[_Truth, _Truth] = {
    _Truth.TRUE: _Truth.FALSE,
    _Truth.FALSE: _Truth.TRUE,
    _Truth.UNKNOWN: _Truth.UNKNOWN,
    _Truth.WITHHELD: _Truth.WITHHELD,
}
