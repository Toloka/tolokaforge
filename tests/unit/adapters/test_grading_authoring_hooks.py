"""Who answers "what tool set, replay world, seeded tables" a task's grading is checked against.

Three siblings of :meth:`BaseAdapter.grading_hash_source_layer`, added by ADR-0042:
:meth:`grading_tool_inventory`, :meth:`grading_replay_world`,
:meth:`grading_seeded_tables`. Each carries the same shape and the same rationale:
an adapter that has not implemented the hook must say it cannot answer, so a pack
whose runtime tool set / replay world / seeded state is not the native reading of
``task.yaml`` is reported and not refused. The native reading is preserved on
:class:`NativeAdapter` — its overrides answer through the same helpers the run path
uses, so what the authoring gate reads and what the trial does are one reading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters import NativeAdapter
from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    seeded_tables_from_task,
)
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    SkipKind,
    ToolInventory,
)
from tolokaforge.core.grading.golden_replay import classify_initial_state
from tolokaforge.core.models import TaskConfig

pytestmark = pytest.mark.unit

_A_REAL_TASK = (
    Path(__file__).resolve().parents[3]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


class _AnAdapterThatImplementsNothing(BaseAdapter):
    """A plugin written against the shipped interface, holding no grading overrides.

    Deliberately abstract — every grading hook is a classmethod precisely so the
    answer needs no instance, and nothing here overrides anything.
    """


@pytest.fixture
def a_task() -> tuple[TaskConfig, Path]:
    """A real task and its directory, since the hooks are asked about exactly those."""
    return load_task_yaml(_A_REAL_TASK)


def test_an_adapter_that_never_implemented_the_tool_inventory_hook_cannot_say(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """Silence is "I cannot say", tagged as the adapter's own silence.

    :attr:`SkipKind.ADAPTER_DECLARED` is what makes such a skip promotable under
    ``--strict-authoring`` — an author targeting the adapter can hold it to
    implementing the hook, while an author whose environment simply has not
    installed the adapter still validates through the STRUCTURAL arm.
    """
    task, task_dir = a_task

    layer = _AnAdapterThatImplementsNothing.grading_tool_inventory(task, task_dir)

    assert layer == ToolInventory.unresolvable()
    assert layer.known is False
    assert layer.skip_kind is SkipKind.ADAPTER_DECLARED


def test_an_adapter_that_never_implemented_the_replay_world_hook_cannot_say(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """Same shape as the tool-inventory sibling — one story, three hooks."""
    task, task_dir = a_task

    world = _AnAdapterThatImplementsNothing.grading_replay_world(task, task_dir)

    assert world == ReplayWorld.unresolvable()
    assert world.known is False
    assert world.skip_kind is SkipKind.ADAPTER_DECLARED


def test_an_adapter_that_never_implemented_the_seeded_tables_hook_cannot_say(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """Same shape as the tool-inventory sibling — one story, three hooks."""
    task, task_dir = a_task

    layer = _AnAdapterThatImplementsNothing.grading_seeded_tables(task, task_dir)

    assert layer == SeededTablesLayer.unresolvable()
    assert layer.known is False
    assert layer.skip_kind is SkipKind.ADAPTER_DECLARED


def test_a_native_task_answers_its_tool_inventory_through_build_tool_inventory(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """NativeAdapter's hook resolves the same set the run path builds a task description from.

    Equality against :func:`build_tool_inventory` reads directly: the two are one
    reading — what the pre-run authoring gate holds ``present`` / ``absent``
    matchers against, and what the runner receives on the wire.
    """
    task, task_dir = a_task

    inventory = NativeAdapter.grading_tool_inventory(task, task_dir)

    assert inventory == build_tool_inventory(task, task_dir)
    assert inventory.known is True


def test_a_native_task_answers_its_replay_world_from_the_two_task_facts(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """``initial_state.json_db`` and ``tools.agent.mcp_server`` are the whole world.

    Equality against the reference construction reads two consumers off one place:
    what the authoring gate holds a golden-action block against at pre-run, and
    what :func:`require_replayable_golden_actions` builds at grade time.
    """
    task, task_dir = a_task

    world = NativeAdapter.grading_replay_world(task, task_dir)

    assert world == ReplayWorld(
        initial_state=classify_initial_state(task.initial_state.json_db),
        mcp_server=bool(task.tools.agent.get("mcp_server")) if task.tools.agent else False,
    )
    assert world.known is True


def test_a_native_task_answers_its_seeded_tables_through_seeded_tables_from_task(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """NativeAdapter's hook reads the same tables the run path seeds on trial start.

    Equality against :func:`seeded_tables_from_task` is what makes a declared
    ``id_fields`` primary key held against a real view of the state the trial
    starts on — the check the run path relies on, hoisted forward to pre-run.
    """
    task, task_dir = a_task

    layer = NativeAdapter.grading_seeded_tables(task, task_dir)

    assert layer == SeededTablesLayer(tables=seeded_tables_from_task(task, task_dir))
    assert layer.known is True
