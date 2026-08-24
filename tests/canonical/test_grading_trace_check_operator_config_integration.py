"""The registry IS the dispatch table both trace-check sites read.

Locks the two invariants of the entry-point rewrite in
``tolokaforge/core/grading/trace_checks.py``:

* **Site A — ``_operator_holds`` reads the loader.** Swapping the ``equals``
  entry-point for a callable that returns the opposite boolean flips
  ``_operator_holds("equals", ...)``. Any hard-coded fallback path — a
  module-level ``_OPERATORS`` dict, an ``if name == "equals"`` shortcut —
  would let the shipped boolean survive the swap; this test would fail.

* **Site B — the binding-references walk filters the registry by the
  ``_binding`` suffix.** A registered ``<name>_binding`` appears in
  :func:`_binding_operator_names`; a non-suffixed sibling does not. Any
  fallback to a hand-written binding-operator list would let one of them
  drift.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Any

import pytest

from tolokaforge.core.grading.trace_checks import _binding_operator_names, _operator_holds
from tolokaforge.core.plugin_registry import (
    TRACE_CHECK_OPERATORS_GROUP,
    _clear_discovery_cache,
    load_trace_check_operator,
)

pytestmark = pytest.mark.canonical


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan."""

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


def _inject(
    monkeypatch: pytest.MonkeyPatch,
    *,
    replace: Mapping[str, Any] | None = None,
    add: Mapping[str, Any] | None = None,
) -> None:
    """Replace / add entry points in the trace-check operators group.

    ``replace`` overrides shipped entries by name; ``add`` appends fresh ones.
    Other groups pass through unchanged.
    """
    _clear_discovery_cache()
    shipped = list(importlib.metadata.entry_points(group=TRACE_CHECK_OPERATORS_GROUP))
    replace = replace or {}
    add = add or {}
    kept = [ep for ep in shipped if ep.name not in replace]
    replaced = [_EntryPointStub(name, callable_) for name, callable_ in replace.items()]
    added = [_EntryPointStub(name, callable_) for name, callable_ in add.items()]
    stubs = [*kept, *replaced, *added]

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == TRACE_CHECK_OPERATORS_GROUP:
            return stubs
        return list(importlib.metadata.entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()


def _always_false(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return False


def _always_true(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    return True


def test_operator_holds_reads_the_registered_equals_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site A — the loader path IS the dispatch path.

    Shipped ``equals`` holds for two equal values; swapping the entry point
    for ``_always_false`` flips it. A hard-coded dispatch shortcut would let
    the shipped answer survive the swap.
    """
    try:
        assert _operator_holds("equals", "PAY-1", "PAY-1", {}) is True

        _inject(monkeypatch, replace={"equals": _always_false})
        assert load_trace_check_operator("equals") is _always_false
        assert _operator_holds("equals", "PAY-1", "PAY-1", {}) is False
    finally:
        _clear_discovery_cache()


def test_operator_holds_reads_the_registered_equals_binding_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site A — the binding branch dispatches through the same loader path.

    Swapping ``equals_binding`` for ``_always_true`` flips the verdict over a
    binding environment that would otherwise fail equality.
    """
    try:
        assert _operator_holds("equals_binding", "PAY-2", "case", {"case": "PAY-1"}) is False

        _inject(monkeypatch, replace={"equals_binding": _always_true})
        assert _operator_holds("equals_binding", "PAY-2", "case", {"case": "PAY-1"}) is True
    finally:
        _clear_discovery_cache()


def test_binding_operator_names_filters_the_registry_by_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site B — the walk projects the ``_binding`` suffix over the registry.

    A registered ``<name>_binding`` appears in the walk; a non-suffixed
    sibling does not. Locks Approved Decision #2's convention against a
    hand-written binding-operator list drifting from the entry-point set.
    """
    try:
        assert _binding_operator_names() == ["contains_binding", "equals_binding"]

        _inject(
            monkeypatch,
            add={
                "custom_semver_binding": _always_true,
                "custom_semver_check": _always_true,
            },
        )
        names = _binding_operator_names()

        assert "custom_semver_binding" in names
        assert "custom_semver_check" not in names
        assert names == sorted(names)
        assert set(names) == {"contains_binding", "custom_semver_binding", "equals_binding"}
    finally:
        _clear_discovery_cache()
