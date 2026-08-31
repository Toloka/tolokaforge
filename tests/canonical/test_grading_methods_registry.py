"""``tolokaforge.grading_methods`` — the runner-side dispatch-selector registry.

The runner reads ``RunnerGradingConfig.grading_method`` off the wire and
resolves the value against this entry-point group at ``RegisterTrial``.
This canonical suite locks the group's public contract: the two shipped
markers resolve; ``available_grading_methods()`` matches the ADR-locked
set; an unknown name fails loud with a message an operator can act on;
and the pydantic model widened past its former closed ``Literal`` so a
downstream adapter can register its own dispatch under the same group
without a framework PR.

See :mod:`tolokaforge.core.grading.grading_method` for the marker
Protocol + shipped implementations.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.grading_method import (
    CompositeGradingMethod,
    TestExecutionGradingMethod,
)
from tolokaforge.core.plugin_registry import (
    GRADING_METHODS_GROUP,
    UnknownImplementationError,
    available_grading_methods,
    load_grading_method,
)
from tolokaforge.runner.models import RunnerGradingConfig

pytestmark = pytest.mark.canonical


def test_builtin_registrations_resolve() -> None:
    assert load_grading_method("composite") is CompositeGradingMethod
    assert load_grading_method("test_execution") is TestExecutionGradingMethod
    assert available_grading_methods() == ["composite", "test_execution"]


def test_unknown_grading_method_raises_named_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_grading_method("does_not_exist")

    message = str(excinfo.value)
    assert "does_not_exist" in message
    assert GRADING_METHODS_GROUP in message
    assert "composite" in message
    assert "test_execution" in message


def test_grading_method_field_is_open_string_and_none_default() -> None:
    assert RunnerGradingConfig().grading_method is None

    downstream = RunnerGradingConfig(grading_method="terminal_bench_native")
    assert downstream.grading_method == "terminal_bench_native"

    round_tripped = RunnerGradingConfig.model_validate_json(downstream.model_dump_json())
    assert round_tripped.grading_method == "terminal_bench_native"
