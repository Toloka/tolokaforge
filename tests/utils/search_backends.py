"""Register search backends beside the shipped ones, for a test's duration.

A backend a package ships reaches the engine through ``tolokaforge.search_backends``
entry points; this installs extra entry points in front of ``importlib.metadata``
so the orchestrator side (adapter, stack rule, ``load_tasks``) and the in-process
runner resolve them exactly as they would an installed package's.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from typing import Any

import pytest

from tolokaforge.core import plugin_registry
from tolokaforge.core.plugin_registry import SEARCH_BACKENDS_GROUP


class _EntryPoint:
    def __init__(self, name: str, factory: Callable[..., Any]) -> None:
        self.name = name
        self._factory = factory

        class _Dist:
            name = "tests-search-backends"

        self.dist = _Dist()

    def load(self) -> Callable[..., Any]:
        return self._factory


def register_search_backends(
    monkeypatch: pytest.MonkeyPatch, **factories: Callable[..., Any]
) -> None:
    """Add ``name=factory`` registrations to the shipped ``tolokaforge.search_backends``."""
    real_entry_points = importlib.metadata.entry_points
    shipped = list(real_entry_points(group=SEARCH_BACKENDS_GROUP))
    injected = [_EntryPoint(name, factory) for name, factory in factories.items()]

    def entry_points(**params: Any) -> Any:
        if params == {"group": SEARCH_BACKENDS_GROUP}:
            return [*shipped, *injected]
        return real_entry_points(**params)

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    # A fresh discovery cache for the test's duration; monkeypatch restores the
    # process's own on teardown, so no later test sees the injected names.
    monkeypatch.setattr(plugin_registry, "_discovery_cache", {})
