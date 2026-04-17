"""Adapter package for harness integration.

Adapters are discovered via ``importlib.metadata`` entry-points in the
``tolokaforge.adapters`` group.  NativeAdapter is always built-in; external
adapters (tau, tlk_mcp_core, …) register themselves through their package
``pyproject.toml`` entry-points and are discovered automatically when
installed.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter, NativeTaskBundle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

# Registry of available adapters — populated by _discover_adapters()
_ADAPTERS: dict[str, type] = {}

# Track entry-point load failures so get_adapter() can report them
_FAILED_ADAPTERS: dict[str, Exception] = {}


def _discover_adapters() -> dict[str, type]:
    """Discover adapter plugins via importlib.metadata entry-points.

    Always includes NativeAdapter and FrozenMcpCoreAdapter (built-in).
    External adapters are loaded from the ``tolokaforge.adapters``
    entry-point group.
    """
    from tolokaforge.adapters.native import NativeAdapter

    adapters: dict[str, type] = {"native": NativeAdapter}

    # Register built-in frozen adapter (does NOT import mcp_core at module level)
    from tolokaforge.adapters.frozen_mcp_core import FrozenMcpCoreAdapter

    adapters["frozen_mcp_core"] = FrozenMcpCoreAdapter

    for ep in importlib.metadata.entry_points(group="tolokaforge.adapters"):
        try:
            adapters[ep.name] = ep.load()
            logger.debug("Loaded adapter entry-point: %s", ep.name)
        except Exception as exc:  # noqa: BLE001
            _FAILED_ADAPTERS[ep.name] = exc
            logger.warning("Adapter entry-point %r failed to load: %s", ep.name, exc, exc_info=True)

    return adapters


def register_adapter(name: str, adapter_cls: type) -> None:
    """Manually register an adapter class (useful for testing)."""
    _ADAPTERS[name] = adapter_cls


def get_adapter(adapter_type: str | None, params: dict[str, Any]) -> BaseAdapter:
    """
    Get adapter instance based on type.

    Args:
        adapter_type: Adapter type ("native", "tau", "tlk_mcp_core", etc.) or None for native
        params: Adapter-specific parameters

    Returns:
        Configured adapter instance

    Raises:
        ValueError: If the requested adapter type is unknown or not installed.
    """
    if adapter_type is None or adapter_type == "native":
        from tolokaforge.adapters.native import NativeAdapter

        return NativeAdapter(params)

    if adapter_type in _ADAPTERS:
        return _ADAPTERS[adapter_type](params)

    # If the adapter was found as an entry-point but failed to load, report the real error
    if adapter_type in _FAILED_ADAPTERS:
        original_exc = _FAILED_ADAPTERS[adapter_type]
        raise ValueError(
            f"Adapter {adapter_type!r} entry-point was found but failed to load: {original_exc}"
        ) from original_exc

    available = sorted(_ADAPTERS.keys())
    raise ValueError(
        f"Unknown adapter type: {adapter_type!r}. "
        f"Available adapters: {available}. "
        f"Install the adapter package or check your configuration."
    )


# Eagerly discover on import
_ADAPTERS = _discover_adapters()

# Public imports
from tolokaforge.adapters.frozen_mcp_core import FrozenMcpCoreAdapter  # noqa: E402
from tolokaforge.adapters.native import NativeAdapter  # noqa: E402

__all__ = [
    "BaseAdapter",
    "AdapterEnvironment",
    "FrozenMcpCoreAdapter",
    "NativeAdapter",
    "NativeTaskBundle",
    "get_adapter",
    "register_adapter",
]
