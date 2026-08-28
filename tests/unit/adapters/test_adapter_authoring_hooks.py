"""What the three under-adapter helpers answer for a non-native pack, and why.

Three helpers in :mod:`tolokaforge.adapters._task_loader` report the pre-run
authoring facts a grading block is held against:
:func:`tool_inventory_under_adapter`, :func:`replay_world_under_adapter`, and
:func:`seeded_tables_under_adapter`. Each dispatches through the adapter's
grading hook: a registered adapter answers for itself, and an ``adapter_type``
this environment has no class for answers :meth:`unresolvable` with
:attr:`SkipKind.STRUCTURAL`. Two adapters cover this: one that implements
nothing so the fingerprint is symmetric across the three helpers and its
inherited :meth:`BaseAdapter` defaults answer :attr:`SkipKind.ADAPTER_DECLARED`,
and one whose ``grading_tool_inventory`` classmethod answers a concrete
inventory so the seam through the adapter is exercised end-to-end.

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
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    SkipKind,
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


def test_the_tool_inventory_helper_dispatches_through_the_hook_for_a_registered_non_native_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The helper reads the adapter's own answer, not a native reading of the task.

    A registered adapter whose ``grading_tool_inventory`` classmethod answers a
    concrete :class:`ToolInventory` gets that inventory back through the helper,
    so every tool-aware rule the gate holds is held against the set the adapter
    presents at runtime rather than the ``tools.agent`` native reading.
    """
    task, task_dir = a_task
    register_adapter("an_adapter_that_owns_its_tool_inventory", _AnAdapterThatOwnsItsToolInventory)

    layer = tool_inventory_under_adapter(task, task_dir, "an_adapter_that_owns_its_tool_inventory")

    assert layer != ToolInventory.unresolvable()
    assert layer == _AN_ADAPTER_OWNED_INVENTORY
    assert layer.known is True


def test_the_mechanism_lock_adapter_answers_its_concrete_inventory_when_asked_directly(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """The adapter's classmethod answers a real inventory, off the class.

    Locks the shape the helper dispatches into: the classmethod returns
    :data:`_AN_ADAPTER_OWNED_INVENTORY` off the class alone, so the seam the
    helper reaches through the adapter carries the same value the class does.
    """
    task, task_dir = a_task

    layer = _AnAdapterThatOwnsItsToolInventory.grading_tool_inventory(task, task_dir)

    assert layer == _AN_ADAPTER_OWNED_INVENTORY
    assert layer.known is True


def test_the_tool_inventory_helper_answers_structural_unresolvable_for_an_unregistered_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """A name the registry does not know answers :attr:`SkipKind.STRUCTURAL`.

    The helper's not-installed arm: ``adapter_class`` returns ``None`` for a
    name no discovered entry-point and no manual registration knows, and the
    helper answers :meth:`ToolInventory.unresolvable` with
    :attr:`SkipKind.STRUCTURAL` — the never-fatal answer for a pack whose
    adapter nothing here can interrogate.
    """
    task, task_dir = a_task

    layer = tool_inventory_under_adapter(task, task_dir, "an_adapter_no_one_registered")

    assert layer == ToolInventory.unresolvable(kind=SkipKind.STRUCTURAL)
    assert layer.skip_kind is SkipKind.STRUCTURAL


def test_the_replay_world_helper_dispatches_through_the_hook_for_a_registered_non_native_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The helper reads the adapter's own answer for the world a replay executes in.

    A registered adapter inheriting :meth:`BaseAdapter.grading_replay_world`
    answers :meth:`ReplayWorld.unresolvable` with :attr:`SkipKind.ADAPTER_DECLARED`
    off its default — the honest "I cannot say" every third-party adapter starts
    with. The kind separates this from the STRUCTURAL arm the not-installed case
    takes.
    """
    task, task_dir = a_task
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)

    world = replay_world_under_adapter(task, task_dir, "an_adapter_that_implements_nothing")

    assert world == ReplayWorld.unresolvable(kind=SkipKind.ADAPTER_DECLARED)
    assert world.skip_kind is SkipKind.ADAPTER_DECLARED


def test_the_seeded_tables_helper_dispatches_through_the_hook_for_a_registered_non_native_adapter(
    a_task: tuple[TaskConfig, Path],
    a_pristine_registry: None,
) -> None:
    """The helper reads the adapter's own answer for the tables ``id_fields`` keys.

    A registered adapter inheriting :meth:`BaseAdapter.grading_seeded_tables`
    answers :meth:`SeededTablesLayer.unresolvable` with :attr:`SkipKind.ADAPTER_DECLARED`
    off its default; the STRUCTURAL arm remains reachable only for a name the
    registry does not know.
    """
    task, task_dir = a_task
    register_adapter("an_adapter_that_implements_nothing", _AnAdapterThatImplementsNothing)

    tables = seeded_tables_under_adapter(task, task_dir, "an_adapter_that_implements_nothing")

    assert tables == SeededTablesLayer.unresolvable(kind=SkipKind.ADAPTER_DECLARED)
    assert tables.skip_kind is SkipKind.ADAPTER_DECLARED


def test_the_three_helpers_route_native_through_native_adapters_own_hooks(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """The native path resolves through the same seam every other adapter uses.

    Each helper's answer for ``adapter_type == "native"`` is exactly what
    :class:`NativeAdapter`'s hook classmethod returns. The equality lock proves
    the migration did not regress the native path — the helper is a dispatch,
    not a duplicate of the hook.
    """
    task, task_dir = a_task

    assert tool_inventory_under_adapter(
        task, task_dir, "native"
    ) == NativeAdapter.grading_tool_inventory(task, task_dir)
    assert replay_world_under_adapter(
        task, task_dir, "native"
    ) == NativeAdapter.grading_replay_world(task, task_dir)
    assert seeded_tables_under_adapter(
        task, task_dir, "native"
    ) == NativeAdapter.grading_seeded_tables(task, task_dir)
