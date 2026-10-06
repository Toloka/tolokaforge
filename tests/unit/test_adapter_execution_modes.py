"""Unit tests for the ``supported_execution_modes`` adapter capability.

:class:`BaseAdapter` defaults to ``{ENGINE_LOOP}`` — the engine's own turn
loop is the default, not a universal requirement. Adapters that can also hand
a trial to a task-provided agent override to add ``DELEGATED`` (the shipped
opt-ins are the native and terminal-bench adapters); a delegated-only adapter
replaces the set with ``{DELEGATED}``.
"""

from __future__ import annotations

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.execution_mode import ExecutionMode

pytestmark = pytest.mark.unit


class _BareAdapter(BaseAdapter):
    """A subclass that does not override the capability — inherits the default."""


def test_base_defaults_to_engine_loop_only() -> None:
    assert BaseAdapter.supported_execution_modes == frozenset({ExecutionMode.ENGINE_LOOP})


def test_a_bare_subclass_stays_engine_loop_only() -> None:
    assert _BareAdapter.supported_execution_modes == frozenset({ExecutionMode.ENGINE_LOOP})
    assert ExecutionMode.DELEGATED not in _BareAdapter.supported_execution_modes


def test_native_includes_delegated() -> None:
    assert NativeAdapter.supported_execution_modes == frozenset(
        {ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED}
    )


def test_terminal_bench_includes_delegated() -> None:
    assert TerminalBenchAdapter.supported_execution_modes == frozenset(
        {ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED}
    )
