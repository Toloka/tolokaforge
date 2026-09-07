"""Composite constraint operators: ``all_of``, ``any_of``, ``negate``.

Each composite recursively evaluates its nested expression through
:mod:`~tolokaforge.core.grading.trace_checks.evaluator`. The recursion resolves
via a lazy function-body import of ``_evaluate`` — the same pattern
:func:`~tolokaforge.core.grading.trace_checks.matcher._operator_holds` uses for
:mod:`~tolokaforge.core.plugin_registry`.
"""

from __future__ import annotations

from tolokaforge.core.grading.trace_checks.resolver import _Resolver
from tolokaforge.core.grading.trace_checks.truth import (
    _NEGATED,
    _conjunction,
    _disjunction,
    _Truth,
)
from tolokaforge.core.models import OnMissing, TraceConstraintExpr


def _all_of(
    payload: list[TraceConstraintExpr], resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    from tolokaforge.core.grading.trace_checks.evaluator import _evaluate

    return _conjunction(_evaluate(expr, resolver, on_missing) for expr in payload)


def _any_of(
    payload: list[TraceConstraintExpr], resolver: _Resolver, on_missing: OnMissing
) -> _Truth:
    from tolokaforge.core.grading.trace_checks.evaluator import _evaluate

    return _disjunction(_evaluate(expr, resolver, on_missing) for expr in payload)


def _negate(payload: TraceConstraintExpr, resolver: _Resolver, on_missing: OnMissing) -> _Truth:
    from tolokaforge.core.grading.trace_checks.evaluator import _evaluate

    return _NEGATED[_evaluate(payload, resolver, on_missing)]
