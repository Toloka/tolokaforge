"""``plugin_registry.load_trace_check_operator`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.trace_check_operators`` entry-point
group:

* every shipped operator resolves to the module-level callable
  ``pyproject.toml`` registers it against, so a future refactor renaming
  any symbol trips this test before it lands;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use.

The loader returns the callable directly — no factory wrapper — so a
resolved operator is invoked as ``op(value, expected, bindings)`` at the
dispatch site.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading import trace_check_operator
from tolokaforge.core.plugin_registry import (
    TRACE_CHECK_OPERATORS_GROUP,
    UnknownImplementationError,
    available_trace_check_operators,
    load_trace_check_operator,
)
from tolokaforge.runner.models import TRACE_PREDICATE_OPERATORS

pytestmark = pytest.mark.unit


_SHIPPED_NAME_TO_SYMBOL = {
    "equals": trace_check_operator.equals,
    "equals_ci": trace_check_operator.equals_ci,
    "contains": trace_check_operator.contains_op,
    "contains_ci": trace_check_operator.contains_ci,
    "not_contains": trace_check_operator.not_contains_op,
    "not_equals": trace_check_operator.not_equals,
    "regex": trace_check_operator.regex_matches,
    "not_regex": trace_check_operator.not_regex_matches,
    "gt": trace_check_operator.gt,
    "gte": trace_check_operator.gte,
    "lt": trace_check_operator.lt,
    "lte": trace_check_operator.lte,
    "in_": trace_check_operator.in_op,
    "not_in": trace_check_operator.not_in_op,
    "len_gt": trace_check_operator.len_gt,
    "len_gte": trace_check_operator.len_gte,
    "exists": trace_check_operator.exists,
    "equals_binding": trace_check_operator.equals_binding,
    "contains_binding": trace_check_operator.contains_binding,
}


def test_every_registered_name_matches_the_predicate_vocabulary() -> None:
    """The registered set and ``ValuePredicate``'s operator field set agree.

    A name in one and not the other is a plug-in the schema cannot address, or a
    schema field the loader cannot resolve — both of which the load tier would
    catch only after a real config drove them into the dispatch.
    """
    assert set(_SHIPPED_NAME_TO_SYMBOL) == TRACE_PREDICATE_OPERATORS
    assert set(available_trace_check_operators()) == TRACE_PREDICATE_OPERATORS


@pytest.mark.parametrize("name", sorted(_SHIPPED_NAME_TO_SYMBOL))
def test_each_shipped_name_resolves_to_its_module_level_callable(name: str) -> None:
    assert load_trace_check_operator(name) is _SHIPPED_NAME_TO_SYMBOL[name]


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_trace_check_operator("nonexistent_operator")
    message = str(excinfo.value)
    assert TRACE_CHECK_OPERATORS_GROUP in message
    assert "equals" in message
    assert excinfo.value.group == TRACE_CHECK_OPERATORS_GROUP
    assert "equals" in excinfo.value.known
