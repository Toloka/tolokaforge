"""Reading a call's arguments by dotted path, and evaluating a :class:`ValuePredicate` on them.

The trace matcher (:mod:`~tolokaforge.core.grading.trace_checks.matcher`) and the
tool-effect rules (:mod:`~tolokaforge.core.tool_effects`) read arguments through
these functions alone, so a ``mutates_state`` read rule and a ``trace_checks``
matcher give one path and one operator the same meaning.

A leaf: :mod:`tolokaforge.runner.models` is read only for annotations, so the
modules that configuration imports can import this one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, TypeAlias

from tolokaforge.core.grading.regex_engine import CompiledPatterns, RegexEngineKind

if TYPE_CHECKING:
    from tolokaforge.runner.models import ValuePredicate

__all__ = [
    "MISSING",
    "PredicatePatterns",
    "argument_at",
    "compile_predicate_patterns",
    "operator_holds",
    "predicate_holds",
]

MISSING: Any = object()
"""What an argument path resolves to where the key was never sent — distinct from
JSON ``null``, which resolves to ``None``. Only ``omitted`` reads the difference
between the two: :func:`operator_holds` reads the sentinel directly for
``is_null`` / ``omitted`` and collapses it to ``None`` for every other operator, so
it never reaches a registered operator. A caller that hands a resolved value on
anywhere else collapses it itself."""

PredicatePatterns: TypeAlias = Mapping[tuple[str, str], CompiledPatterns]
"""Each ``regex`` / ``not_regex`` a set of predicates declares, compiled, keyed by the
field its predicate reads and the operator name."""


def argument_at(arguments: Any, path: str) -> Any:
    """The value a dotted argument path addresses, or :data:`MISSING`.

    :data:`MISSING` where the path does not resolve — an ancestor that is not a
    mapping, or a segment absent from the mapping that carries it.
    """
    value: Any = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return MISSING
        value = value[segment]
    return value


def compile_predicate_patterns(
    predicates: Iterable[tuple[str, ValuePredicate]], section: RegexEngineKind
) -> PredicatePatterns:
    """Every pattern *predicates* declare, compiled under each predicate's effective engine.

    ``section`` is the engine of the block the predicates sit in, which a predicate
    naming its own overrides. A pattern its engine refuses raises
    :class:`~tolokaforge.core.grading.regex_engine.UncompilablePattern`.
    """
    return {
        (field, name): CompiledPatterns.compile(
            predicate.patterns_of(name), predicate.regex_engine_under(section)
        )
        for field, predicate in predicates
        for name in sorted(predicate.declared_operators() & predicate.PATTERN_OPERATORS)
    }


def predicate_holds(
    field: str,
    predicate: ValuePredicate,
    value: Any,
    bindings: Mapping[str, Any],
    patterns: PredicatePatterns,
) -> bool:
    """Whether every operator the predicate declares holds — it is their conjunction.

    A predicate declaring no operator is rejected at load, so the conjunction is
    never over the empty set and never vacuously true. A pattern operator reads its
    compiled form out of ``patterns`` under ``(field, operator)``; every other
    operator reads the authored value.
    """
    return all(
        operator_holds(
            name,
            value,
            (
                patterns[(field, name)]
                if name in predicate.PATTERN_OPERATORS
                else getattr(predicate, name)
            ),
            bindings,
        )
        for name in predicate.declared_operators()
    )


def operator_holds(name: str, value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    """One operator over one value.

    ``is_null`` reads whether the field held an explicit JSON ``null``, and
    ``omitted`` whether the key was never sent — the pair whose meaning turns on
    the argument's state before the reading. Both are special-cased ahead of the
    entry-point dispatch: the :data:`MISSING` sentinel that separates the two is
    not part of the operator seam, so the seam cannot answer for them; their
    registered callables are stubs kept only to keep the frozenset and the
    entry-point registry in lockstep. Every other operator reads a :data:`MISSING`
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
        return (value is MISSING) is expected
    if value is MISSING:
        value = None
    if value is None and name != "exists":
        return False
    from tolokaforge.core.plugin_registry import load_trace_check_operator

    op = load_trace_check_operator(name)
    return op(value, expected, bindings)
