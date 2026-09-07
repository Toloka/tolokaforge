"""Registry-wide guard: every registered adapter carries the grading contract by name.

Iterates :func:`~tolokaforge.adapters.available_adapters` and asserts each
class carries every method slot and capability flag
:class:`~tolokaforge.adapters.grading_contract.AdapterGradingContract`
declares. Name-only — matching the ``@runtime_checkable`` Protocol's
signature-blindness — but locks the shape for every in-registry adapter
class, catching a future refactor that silently drops a slot from
:class:`~tolokaforge.adapters.base.BaseAdapter` without at least one
entry-plugin dropping it too.

The per-adapter suite in
:class:`~tolokaforge.testing.adapters.AdapterGradingContractSuite` is the
tighter lock the Protocol cannot enforce alone; this file is the outer
boundary that names the whole registry.
"""

from __future__ import annotations

import pytest

from tolokaforge.adapters import adapter_class, available_adapters

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


def test_every_registered_adapter_class_carries_every_declared_slot() -> None:
    names = available_adapters()

    assert names, "expected at least one adapter (NativeAdapter is built-in)"

    for name in names:
        cls = adapter_class(name)
        assert cls is not None, f"{name} discovered but adapter_class returned None"
        for slot in _PROTOCOL_METHOD_SLOTS:
            assert hasattr(cls, slot), f"{name}.{slot} missing"
        for flag in _PROTOCOL_CAPABILITY_FLAGS:
            assert hasattr(cls, flag), f"{name}.{flag} missing"
