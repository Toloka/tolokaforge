"""Out-of-tree ``RuntimeBackend`` plug-in, installed (not workspace-linked) by
``tests/integration/test_plugin_discovery.py`` to prove a downstream package's
entry point becomes discoverable end-to-end.

Registers ``fixture_backend`` in the ``tolokaforge.runtime_backends`` group via
this package's own ``pyproject.toml``.
"""

from __future__ import annotations

from tolokaforge.core.runtime import InMemoryRuntimeBackend


class FixtureRuntimeBackend(InMemoryRuntimeBackend):
    """Minimal downstream backend — reuses the in-memory fixture's behaviour."""


def fixture_backend_factory(ctx: object) -> FixtureRuntimeBackend:
    """Build the fixture backend. Ignores the build context (nothing to seed)."""
    return FixtureRuntimeBackend()
