"""What the three under-adapter helpers answer for a non-native pack, and why.

Three helpers in :mod:`tolokaforge.adapters._task_loader` report the pre-run
authoring facts a grading block is held against:
:func:`tool_inventory_under_adapter`, :func:`replay_world_under_adapter`, and
:func:`seeded_tables_under_adapter`. All three answer ``unresolvable()`` for
every ``adapter_type`` that is not ``native``, regardless of what the adapter
would say — the helpers do not consult the adapter. Two adapters cover this:
one that implements nothing so the fingerprint is symmetric across the three
helpers, and one whose ``grading_tool_inventory`` classmethod answers a real
inventory when asked directly, so the second half of the story — the helper
discards the adapter's answer, not the adapter's silence — is not left to the
reader's imagination.

The registry is isolated with the same ``a_pristine_registry`` fixture
:mod:`tests.unit.adapters.test_hash_source_hook` uses, for the same reason: a
registration made here must not survive the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tolokaforge.adapters as adapters_package
from tolokaforge.adapters import register_adapter
from tolokaforge.adapters._task_loader import (
    load_task_yaml,
    replay_world_under_adapter,
    seeded_tables_under_adapter,
    tool_inventory_under_adapter,
)
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
)
from tolokaforge.core.models import TaskConfig

pytestmark = pytest.mark.unit

_A_REAL_TASK = (
    Path(__file__).resolve().parents[3]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


_AN_ADAPTER_OWNED_INVENTORY = ToolInventory(
    declared=frozenset({"an_adapter_owned_tool"}),
    agent_declared=frozenset({"an_adapter_owned_tool"}),
    user_declared=frozenset(),
    actor_split_known=True,
    parameters={},
    known=True,
)
"""A concrete non-unresolvable inventory the mechanism-lock adapter would report.

The shape is deliberately minimal — one tool, no parameters — because the
assertion reads only whether the answer is ``unresolvable()``, not what the
inventory carries. Anything the ``ToolInventory`` invariants accept works.
"""


class _AnAdapterThatImplementsNothing(BaseAdapter):
    """A plugin written against the shipped interface, holding no grading overrides.

    Deliberately abstract: the three helpers ask the class, not an instance, so
    nothing here overrides anything and the class never has to be constructed.
    """


class _AnAdapterThatOwnsItsToolInventory(BaseAdapter):
    """A plugin whose runtime tool set is not what ``tools.agent.enabled`` reports.

    Answers a real :class:`ToolInventory` when asked directly for the tool set
    it presents at runtime. Deliberately abstract for the same reason
    :class:`_AnAdapterThatImplementsNothing` is — the classmethod answers off
    the class and no instance is needed to exercise the seam.
    """

    @classmethod
    def grading_tool_inventory(cls, task: TaskConfig, task_dir: Path) -> ToolInventory:
        return _AN_ADAPTER_OWNED_INVENTORY


@pytest.fixture
def a_task() -> tuple[TaskConfig, Path]:
    """A real task and its directory, since the helpers read exactly those."""
    return load_task_yaml(_A_REAL_TASK)


@pytest.fixture
def a_pristine_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry as a fresh process has it: nothing registered, nothing discovered.

    Replacing the dict keeps a test's registration out of the real one; clearing
    the flag puts the test before first discovery, so the registration ordering
    the discovery path relies on is exercised, not skipped.
    """
    monkeypatch.setattr(adapters_package, "_ADAPTERS", {})
    monkeypatch.setattr(adapters_package, "_DISCOVERED", False)


def test_the_tool_inventory_helper_answers_unresolvable_for_a_non_native_adapter_whose_hook_would_say_otherwise(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The helper does not consult the adapter — a registered override is ignored.

    The registered adapter answers a concrete :class:`ToolInventory` when asked
    directly for its tool set, and the helper still returns
    :meth:`ToolInventory.unresolvable`. Reading the helper's answer as the
    adapter's answer for a non-native pack is what leaves every tool-aware rule
    silently unchecked.
    """
    task, task_dir = a_task
    register_adapter("an_adapter_that_owns_its_tool_inventory", _AnAdapterThatOwnsItsToolInventory)

    layer = tool_inventory_under_adapter(task, task_dir, "an_adapter_that_owns_its_tool_inventory")

    assert layer == ToolInventory.unresolvable()


def test_the_mechanism_lock_adapter_answers_its_concrete_inventory_when_asked_directly(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """The adapter's classmethod does answer — the helper is what discards that answer.

    Names the second half of the reproducer explicitly: the non-unresolvable
    reading is available off the class, so the helper's ``unresolvable()``
    result is not the adapter's silence, it is the helper's blindness.
    """
    task, task_dir = a_task

    layer = _AnAdapterThatOwnsItsToolInventory.grading_tool_inventory(task, task_dir)

    assert layer == _AN_ADAPTER_OWNED_INVENTORY
    assert layer.known is True


def test_the_replay_world_helper_answers_unresolvable_for_a_non_native_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The world the golden actions would replay against is not read from the adapter.

    An adapter that implements nothing gives the helper the same answer as one
    that does — the helper never asks either. The rule reading the world skips
    on the world's own ``unresolvable()`` flag, so a task whose ``json_db`` and
    ``mcp_server`` do resolve is not checked against them.
    """
    task, _ = a_task
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)

    world = replay_world_under_adapter(task, "an_adapter_that_implements_nothing")

    assert world == ReplayWorld.unresolvable()


def test_the_seeded_tables_helper_answers_unresolvable_for_a_non_native_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The tables the ``id_fields`` declaration keys are not read from the adapter.

    The helper returns :meth:`SeededTablesLayer.unresolvable` for the same
    reason its neighbours do: the ``adapter_type != native`` branch short-circuits
    before any adapter is consulted, so a declaration held against tables the
    task actually seeds reads as unchecked.
    """
    task, task_dir = a_task
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)

    tables = seeded_tables_under_adapter(task, task_dir, "an_adapter_that_implements_nothing")

    assert tables == SeededTablesLayer.unresolvable()
