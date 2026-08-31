"""Every registered adapter satisfies :class:`AdapterGradingContract`.

Two locks. First, :class:`NativeAdapter` — the built-in shape — passes
``isinstance`` against :class:`AdapterGradingContract` at runtime, resolves
every slot the Protocol declares on the class, and returns the shipped
defaults from :class:`BaseAdapter` for the three emit seams and three
capability flags. Second, a registry-wide name-presence sweep: every adapter
class discoverable through :func:`available_adapters` carries each declared
slot as an attribute, so a future refactor cannot silently drop a slot from
:class:`BaseAdapter` without at least one entry-plugin dropping it too.

The sweep is name-only — matching the runtime-checkable Protocol's own
signature-blindness — and the :class:`NativeAdapter` assertions on the
individual defaults are the tighter lock the Protocol cannot enforce alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters import (
    AdapterGradingContract,
    NativeAdapter,
    adapter_class,
    available_adapters,
)
from tolokaforge.adapters._task_loader import GradingSource, GradingSourceKind
from tolokaforge.core.models import TaskConfig

pytestmark = pytest.mark.canonical


_PROTOCOL_METHOD_SLOTS = (
    "grading_source",
    "grading_tool_inventory",
    "grading_replay_world",
    "grading_seeded_tables",
    "emit_runner_grading_payload",
    "preferred_grader_kind",
)

_PROTOCOL_CAPABILITY_FLAGS = (
    "requires_docker_cli_in_runner",
    "grades_from_task_grading_file",
    "syncs_adapter_env_to_state",
)


_A_REAL_TASK = (
    Path(__file__).resolve().parents[2]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


@pytest.fixture
def a_native_adapter(tmp_path: Path) -> NativeAdapter:
    """A minimal :class:`NativeAdapter` constructed for slot resolution.

    The tasks-glob does not need to resolve any task — the slot resolutions
    tested here call each hook against a task supplied directly and never
    reach ``get_task_ids``.
    """
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "**/task.yaml"})


@pytest.fixture
def a_task_and_dir() -> tuple[TaskConfig, Path]:
    """A real task and its directory, since :meth:`grading_source` reads them."""
    from tolokaforge.adapters._task_loader import load_task_yaml

    return load_task_yaml(_A_REAL_TASK)


def test_a_native_adapter_instance_satisfies_the_grading_contract(
    a_native_adapter: NativeAdapter,
) -> None:
    """Runtime ``isinstance`` against the ``@runtime_checkable`` Protocol.

    Locks that every declared slot resolves as an attribute on a real
    :class:`NativeAdapter`; a future refactor that drops a slot from
    :class:`BaseAdapter` fails this before any caller notices.
    """
    assert isinstance(a_native_adapter, AdapterGradingContract)


def test_the_native_adapter_class_resolves_every_declared_slot(
    a_native_adapter: NativeAdapter,
) -> None:
    """Class-level resolution of the six method slots and three capability flags.

    ``hasattr`` on the instance covers both class-level classmethods and
    instance methods, and the capability flags being ``ClassVar[bool]``
    means they resolve as class attributes too.
    """
    for slot in _PROTOCOL_METHOD_SLOTS:
        assert hasattr(a_native_adapter, slot), f"{slot} missing on NativeAdapter instance"
    for flag in _PROTOCOL_CAPABILITY_FLAGS:
        assert hasattr(NativeAdapter, flag), f"{flag} missing on NativeAdapter class"


def test_the_three_capability_flags_read_false_on_a_bare_native_adapter(
    a_native_adapter: NativeAdapter,
) -> None:
    """The shipped defaults are ``False`` for every capability the flags name.

    :class:`NativeAdapter` neither drives the host Docker CLI, nor grades
    from a task ``grading:`` file through the class hook, nor syncs adapter
    env into runner state — the three defaults each read ``False``.
    """
    assert a_native_adapter.requires_docker_cli_in_runner is False
    assert a_native_adapter.grades_from_task_grading_file is False
    assert a_native_adapter.syncs_adapter_env_to_state is False


def test_the_native_adapter_grading_source_is_callable_on_the_class(
    a_native_adapter: NativeAdapter,
    a_task_and_dir: tuple[TaskConfig, Path],
) -> None:
    """:class:`NativeAdapter.grading_source` dispatches from the class, not just an instance.

    Called on the class (``NativeAdapter.grading_source(task, task_dir)``)
    it returns the same :class:`GradingSource` the instance call returns —
    the classmethod dispatch the free-function delegation helper relies on
    to reach the source without instantiating :class:`NativeAdapter` (which
    would demand a ``tasks_glob``).
    """
    task, task_dir = a_task_and_dir

    from_class = NativeAdapter.grading_source(task, task_dir)
    from_instance = a_native_adapter.grading_source(task, task_dir)

    assert from_class == from_instance


def test_the_native_adapter_grading_source_reports_the_pack_grading_yaml_when_present(
    a_native_adapter: NativeAdapter,
    a_task_and_dir: tuple[TaskConfig, Path],
) -> None:
    """Locks the ON_DISK answer :class:`NativeAdapter` returns for a pack shipping ``grading.yaml``.

    The helpdesk_01 fixture ships a sibling ``grading.yaml``, which
    ``load_task_yaml`` resolves as the declared source; the native override
    reads that file off disk and answers :attr:`~GradingSourceKind.ON_DISK`
    with the resolved path and no reason. The default-lock intent —
    :attr:`~GradingSourceKind.UNINTERROGABLE` for a bare adapter — is
    covered by ``_AStubAdapter`` in
    ``tests/unit/adapters/test_grading_contract_defaults.py``.
    """
    task, task_dir = a_task_and_dir

    source = a_native_adapter.grading_source(task, task_dir)

    assert isinstance(source, GradingSource)
    assert source.kind is GradingSourceKind.ON_DISK
    assert source.path == task_dir / "grading.yaml"
    assert source.reason == ""


def test_the_emit_runner_grading_payload_default_is_empty(
    a_native_adapter: NativeAdapter,
) -> None:
    """The default payload is ``{}`` — the runner falls through to the historical dispatch."""
    assert a_native_adapter.emit_runner_grading_payload("calc_basic") == {}


def test_the_preferred_grader_kind_default_is_composite(
    a_native_adapter: NativeAdapter,
) -> None:
    """The default kind is ``composite`` — the shipped default grader kind."""
    assert a_native_adapter.preferred_grader_kind() == "composite"


def test_every_registered_adapter_class_carries_every_declared_slot() -> None:
    """Registry-wide name-presence sweep across every discoverable adapter class.

    Iterates :func:`available_adapters` and asserts each class carries each
    method slot and capability flag by name. Name-only — matching the
    ``@runtime_checkable`` Protocol's signature-blindness — but locks the
    shape for every in-registry adapter (not just :class:`NativeAdapter`),
    catching a future refactor that silently drops a slot from
    :class:`BaseAdapter`.
    """
    names = available_adapters()

    assert names, "expected at least one adapter (NativeAdapter is built-in)"

    for name in names:
        cls = adapter_class(name)
        assert cls is not None, f"{name} discovered but adapter_class returned None"
        for slot in _PROTOCOL_METHOD_SLOTS:
            assert hasattr(cls, slot), f"{name}.{slot} missing"
        for flag in _PROTOCOL_CAPABILITY_FLAGS:
            assert hasattr(cls, flag), f"{name}.{flag} missing"
