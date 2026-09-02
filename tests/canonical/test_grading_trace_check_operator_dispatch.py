"""``load_trace_check_operator`` discovery + ``_operator_holds`` dispatch.

Locks the two seams the trace-check operator design commits to:

1. :func:`~tolokaforge.core.plugin_registry.load_trace_check_operator` resolves
   an operator registered via ``importlib.metadata`` entry-points. The
   dispatch case injects a synthetic entry-point pointing at
   :func:`tests.utils.trace_check_operator_demo.is_positive_number` under the
   group ``tolokaforge.trace_check_operators`` and asserts the loader returns
   the demo callable. No wheel pollution: the demo stays discoverable only
   under the monkeypatched mapping.

2. **Evaluator dispatch through the resolved callable.**
   :func:`~tolokaforge.core.grading.trace_checks._operator_holds` invokes
   the resolved custom operator by name — proving the registry-lookup path
   IS the dispatch path.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest

from tests.utils.trace_check_operator_demo import is_positive_number
from tolokaforge.core.grading.trace_checks import _operator_holds
from tolokaforge.core.plugin_registry import (
    TRACE_CHECK_OPERATORS_GROUP,
    _clear_discovery_cache,
    load_trace_check_operator,
)

pytestmark = pytest.mark.canonical


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan.

    Enumerates ``name`` / ``dist`` and returns ``value`` on ``load()`` — the
    surface :func:`discover_entry_points` reads.
    """

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


def _inject_demo_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the demo operator alongside the shipped names."""
    _clear_discovery_cache()
    shipped = list(importlib.metadata.entry_points(group=TRACE_CHECK_OPERATORS_GROUP))
    injected = _EntryPointStub("test_trace_operator", is_positive_number)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == TRACE_CHECK_OPERATORS_GROUP:
            return [*shipped, injected]
        return list(importlib.metadata.entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()


def test_loader_resolves_the_monkeypatched_demo_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_demo_operator(monkeypatch)
    try:
        resolved = load_trace_check_operator("test_trace_operator")
        assert resolved is is_positive_number
    finally:
        _clear_discovery_cache()


def test_operator_holds_dispatches_through_the_registered_demo_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_operator_holds`` reaches the plug-in operator by name.

    A positive number holds; a non-positive one does not. Proves the dispatch
    site itself reads the entry-point registry — no hard-coded fallback path.
    """
    _inject_demo_operator(monkeypatch)
    try:
        assert _operator_holds("test_trace_operator", 3, None, {}) is True
        assert _operator_holds("test_trace_operator", 0, None, {}) is False
        assert _operator_holds("test_trace_operator", -5, None, {}) is False
        assert _operator_holds("test_trace_operator", "abc", None, {}) is False
    finally:
        _clear_discovery_cache()


def test_operator_holds_preserves_the_none_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` value short-circuits before the registered operator is called.

    Every operator but ``exists`` is false on ``None``; the gate is a dispatch
    invariant declared once in ``_operator_holds`` rather than repeated inside
    every operator. Locks that the ``None`` gate is declared once in
    ``_operator_holds``, not duplicated inside every registered operator.
    """
    _inject_demo_operator(monkeypatch)
    try:
        assert _operator_holds("test_trace_operator", None, 0, {}) is False
    finally:
        _clear_discovery_cache()
