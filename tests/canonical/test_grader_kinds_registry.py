"""``tolokaforge.grader_kinds`` — resolver + dual-registration vocabulary lock.

Locks the three invariants the typed-kind registry commits to:

1. The two built-in names (``composite``, ``test_execution``) resolve to
   real classes with matching ``NAME``.
2. An unknown name fails loud via :class:`UnknownImplementationError`
   naming the offending key + the registered set.
3. **Dual-registration invariant** — every shipped name in
   ``tolokaforge.grader_kinds`` is also in ``tolokaforge.grading_methods``
   and vice versa. Downstream adapters implement the pattern too;
   registering a name in only one of the two groups is a defect.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.kinds import (
    CompositeGraderKind,
    GraderKind,
    TestExecutionGraderKind,
)
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_grader_kinds,
    available_grading_methods,
    load_grader_kind,
)

pytestmark = pytest.mark.canonical


def test_builtin_grader_kinds_resolve_to_their_class() -> None:
    assert load_grader_kind("composite") is CompositeGraderKind
    assert load_grader_kind("test_execution") is TestExecutionGraderKind
    assert available_grader_kinds() == ["composite", "test_execution"]


def test_unknown_grader_kind_raises_named_error() -> None:
    with pytest.raises(UnknownImplementationError) as exc_info:
        load_grader_kind("does_not_exist")
    message = str(exc_info.value)
    assert "does_not_exist" in message
    assert "tolokaforge.grader_kinds" in message
    assert "composite" in message
    assert "test_execution" in message


def test_grader_kinds_and_grading_methods_share_vocabulary() -> None:
    """Every shipped name registers in BOTH groups. A name in one but not the
    other is a defect: the marker registry (``tolokaforge.grading_methods``,
    validated at ``RegisterTrial``) and the typed-kind registry
    (``tolokaforge.grader_kinds``, driving dispatch) coexist and must agree.
    Downstream adapters register their names in both groups too."""
    assert set(available_grader_kinds()) == set(available_grading_methods())


def test_grader_kind_classes_are_runtime_checkable_protocol_instances() -> None:
    assert isinstance(CompositeGraderKind(), GraderKind)
    assert isinstance(TestExecutionGraderKind(), GraderKind)
