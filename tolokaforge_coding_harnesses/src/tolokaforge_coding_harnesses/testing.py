"""Stand-ins for an installed harness-registry plugin.

``importlib.metadata.EntryPoint`` refuses attribute assignment, so a test
cannot bind a fabricated distribution to a real one. These frozen substitutes
carry exactly the attributes
:func:`~tolokaforge_coding_harnesses.discover_plugin_harness_registries` reads:
``name``, ``dist`` (``None`` for a programmatically registered entry point),
and ``load()``.

Shipped rather than kept in one suite's test utilities: the entry-point group is
this package's extension contract, so every suite that exercises it — this
package's own, the adapters that consume it, and an out-of-tree bundle's —
fabricates the same installed set through one implementation. ``monkeypatch`` is
taken as a parameter, so importing this module needs no test framework.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _registry

if TYPE_CHECKING:
    import pytest


@dataclass(frozen=True)
class FakeDistribution:
    name: str
    version: str


@dataclass(frozen=True)
class FakeEntryPoint:
    name: str
    dist: FakeDistribution | None
    target: object
    """What ``load()`` answers. A case that only exercises name collisions never
    loads, so any sentinel does; a case that reads the bundle's YAML passes the
    importable package."""

    def load(self) -> object:
        return self.target


def isolate_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give this test its own entry-point discovery cache.

    Discovery answers from the cache before it reads the patched
    ``entry_points`` attribute, so a warm cache makes an injection a silent
    no-op and leaves a case asserting nothing is installed reading a
    neighbour's plug-ins. Substituting the cache rather than clearing it means
    ``monkeypatch`` restores the real one at teardown, so a caller needs no
    teardown of its own.
    """
    monkeypatch.setattr(_registry, "_discovery_cache", {})


def build_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    distribution: str | None,
    harnesses: str,
    version: str = "1.0.0",
) -> FakeEntryPoint:
    """An importable package under *tmp_path* shipping *harnesses* as its registry."""
    root = tmp_path / package
    (root / package).mkdir(parents=True)
    (root / package / "__init__.py").write_text("")
    (root / package / "harnesses.yaml").write_text(harnesses)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, package, raising=False)
    dist = None if distribution is None else FakeDistribution(distribution, version)
    return FakeEntryPoint(package, dist, importlib.import_module(package))


def install_plugins(monkeypatch: pytest.MonkeyPatch, *entry_points: FakeEntryPoint) -> None:
    """Make *entry_points* the installed set for the harness-registry group."""
    import importlib.metadata

    real = importlib.metadata.entry_points

    def _entry_points(**kwargs: Any) -> Any:
        if kwargs.get("group") == _registry.HARNESS_REGISTRY_ENTRY_POINT_GROUP:
            return list(entry_points)
        return real(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _entry_points)
    isolate_discovery(monkeypatch)


def bundle_yaml(name: str, version: str) -> str:
    """A one-harness registry document, the shape a plugin ships."""
    return (
        "harnesses:\n"
        f"  {name}:\n"
        f"    install_source: '@acme/{name}'\n"
        f"    version: '{version}'\n"
        f"    argv_prefix: [{name}]\n"
        f"    argv_suffix: ['--go']\n"
    )
