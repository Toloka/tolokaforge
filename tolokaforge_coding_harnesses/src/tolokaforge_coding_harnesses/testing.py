"""Stand-ins for an installed harness-registry plugin.

``importlib.metadata.EntryPoint`` refuses attribute assignment, so a test
cannot bind a fabricated distribution to a real one. These duck-typed
substitutes carry exactly the attributes
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
from pathlib import Path
from typing import Any


class FakeDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class FakeEntryPoint:
    def __init__(self, name: str, distribution: FakeDistribution | None, module: Any) -> None:
        self.name = name
        self.dist = distribution
        self._module = module

    def load(self) -> Any:
        return self._module


def build_plugin(
    tmp_path: Path,
    monkeypatch: Any,
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


def install_plugins(monkeypatch: Any, *entry_points: FakeEntryPoint) -> None:
    """Make *entry_points* the installed set for the harness-registry group."""
    import importlib.metadata

    from tolokaforge_coding_harnesses import HARNESS_REGISTRY_ENTRY_POINT_GROUP
    from tolokaforge_coding_harnesses._registry import _clear_discovery_cache

    real = importlib.metadata.entry_points

    def _entry_points(**kwargs: Any) -> Any:
        if kwargs.get("group") == HARNESS_REGISTRY_ENTRY_POINT_GROUP:
            return list(entry_points)
        return real(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _entry_points)
    # Discovery serves a warm cache before it ever reads the patched attribute,
    # so leaving the cache in place makes this call a silent no-op.
    _clear_discovery_cache()


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
