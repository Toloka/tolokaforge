"""The operator names a :class:`~tolokaforge.runner.models.ValuePredicate` may carry.

Stdlib only: a task's MCP server process checks the read rules its tools declare
against these names (:mod:`tolokaforge.core.mutates_state_meta`) and imports
nothing of the engine's models.
"""

from __future__ import annotations

TRACE_PREDICATE_OPERATORS: frozenset[str] = frozenset(
    {
        "equals",
        "equals_ci",
        "contains",
        "contains_ci",
        "not_contains",
        "not_equals",
        "regex",
        "not_regex",
        "is_null",
        "omitted",
        "gt",
        "gte",
        "lt",
        "lte",
        "date_gt",
        "date_gte",
        "date_lt",
        "date_lte",
        "in_",
        "not_in",
        "len_gt",
        "len_gte",
        "exists",
        "equals_binding",
        "contains_binding",
    }
)
"""Every operator a :class:`~tolokaforge.runner.models.ValuePredicate` may carry, written out rather than
comprehended from the model, so the per-operator answer table has a second source
to be checked against."""

TRACE_PREDICATE_MODIFIERS: frozenset[str] = frozenset({"regex_engine"})
"""The :class:`~tolokaforge.runner.models.ValuePredicate` fields that change how its operators read and assert
nothing on their own — so a predicate declaring only a modifier declares no operator."""

TRACE_PREDICATE_REGEX_OPERATORS: frozenset[str] = frozenset({"regex", "not_regex"})
"""The operators whose operand is a pattern, compiled under the predicate's engine."""

TRACE_PREDICATE_BINDING_OPERATORS: frozenset[str] = frozenset(
    {"equals_binding", "contains_binding"}
)
"""The operators whose value is a name in the constraint's binding environment
rather than a value to compare against."""

TRACE_PREDICATE_DATE_OPERATORS: frozenset[str] = frozenset(
    {"date_gt", "date_gte", "date_lt", "date_lte"}
)
"""The operators whose literal is an ISO-8601 date or datetime. Read by the
load-tier validator that refuses a bound value no calendar reading holds — the
same second-source symmetry ``TRACE_PREDICATE_BINDING_OPERATORS`` carries."""
