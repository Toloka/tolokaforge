"""``plugin_registry.load_custom_check_executor`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.custom_check_executors`` entry-point
group:

* the two shipped factories (``check_runner``, ``in_memory``) resolve to the
  callables ``pyproject.toml`` registers them against, so a future refactor
  renaming either symbol trips this test before it lands;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.check_runner import (
    CheckRunner,
    InMemoryCheckExecutor,
    _check_runner_factory,
    _in_memory_check_executor_factory,
)
from tolokaforge.core.plugin_registry import (
    CUSTOM_CHECK_EXECUTORS_GROUP,
    UnknownImplementationError,
    available_custom_check_executors,
    load_custom_check_executor,
)

pytestmark = pytest.mark.unit


def test_check_runner_resolves_to_the_shipped_factory() -> None:
    assert load_custom_check_executor("check_runner") is _check_runner_factory


def test_in_memory_resolves_to_the_shipped_factory() -> None:
    assert load_custom_check_executor("in_memory") is _in_memory_check_executor_factory


def test_check_runner_factory_returns_a_check_runner_instance() -> None:
    """The factory the loader hands back builds the production executor.

    Locks the two-step "loader → factory() → instance" chain end-to-end so a
    future refactor that hooks ``pyproject.toml`` to the class directly
    (skipping the factory) trips this test.
    """
    factory = load_custom_check_executor("check_runner")
    assert isinstance(factory(), CheckRunner)


def test_in_memory_factory_returns_an_in_memory_check_executor_instance() -> None:
    factory = load_custom_check_executor("in_memory")
    assert isinstance(factory(), InMemoryCheckExecutor)


def test_available_lists_both_shipped_names() -> None:
    names = available_custom_check_executors()
    assert "check_runner" in names
    assert "in_memory" in names


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_custom_check_executor("nonexistent")
    message = str(excinfo.value)
    assert CUSTOM_CHECK_EXECUTORS_GROUP in message
    assert "check_runner" in message
    assert "in_memory" in message
    assert excinfo.value.group == CUSTOM_CHECK_EXECUTORS_GROUP
    assert "check_runner" in excinfo.value.known
    assert "in_memory" in excinfo.value.known
