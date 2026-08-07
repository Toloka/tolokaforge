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

from tolokaforge.adapters.base import (
    AdapterEnvironment,
    BaseAdapter,
    DockerStackRequirements,
    NativeTaskBundle,
)
from tolokaforge.runner.models import AdapterType

logger = logging.getLogger(__name__)

# Canonical name of the only built-in adapter (kept as a constant, not a literal).
_NATIVE = AdapterType.NATIVE.value

# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

# Registry of available adapters — written by register_adapter() and by discovery
_ADAPTERS: dict[str, type] = {}

# Whether entry-point discovery has run. A separate flag rather than the registry's
# own emptiness: register_adapter() writes into the registry too, so reading it as
# "discovery happened" lets one early registration suppress discovery for good.
_DISCOVERED = False

# Track entry-point load failures so get_adapter() can report them
_FAILED_ADAPTERS: dict[str, Exception] = {}


def _discover_adapters() -> dict[str, type]:
    """Discover adapter plugins via importlib.metadata entry-points.

    NativeAdapter is the only built-in adapter; all others are entry-point
    plugins loaded from the ``tolokaforge.adapters`` group.
    """
    from tolokaforge.adapters.native import NativeAdapter

    adapters: dict[str, type] = {_NATIVE: NativeAdapter}

    for ep in importlib.metadata.entry_points(group="tolokaforge.adapters"):
        try:
            adapters[ep.name] = ep.load()
            logger.debug("Loaded adapter entry-point: %s", ep.name)
        except Exception as exc:  # noqa: BLE001
            _FAILED_ADAPTERS[ep.name] = exc
            logger.warning("Adapter entry-point %r failed to load: %s", ep.name, exc, exc_info=True)

    return adapters


def _ensure_adapters_discovered() -> None:
    """Discover external adapter plugins lazily on first use.

    Discovery merges into the registry and never replaces it: an explicit
    :func:`register_adapter` outranks an entry-point of the same name, and one written
    before the first discovery has to survive it.
    """
    global _DISCOVERED

    if _DISCOVERED:
        return

    for name, adapter_cls in _discover_adapters().items():
        _ADAPTERS.setdefault(name, adapter_cls)
    _DISCOVERED = True


def available_adapters() -> list[str]:
    """Return the names of all registered adapters.

    Triggers lazy discovery of external plugins on first call so callers do
    not need to know about the discovery mechanism.
    """
    _ensure_adapters_discovered()
    return sorted(_ADAPTERS.keys())


def register_adapter(name: str, adapter_cls: type) -> None:
    """Manually register an adapter class (useful for testing)."""
    _ADAPTERS[name] = adapter_cls


def adapter_class(adapter_type: str) -> type[BaseAdapter] | None:
    """The class registered for *adapter_type*, or ``None`` where none resolves.

    Never raises, unlike :func:`get_adapter`. The static gates ask this about packs
    whose adapter package may not be installed at all, and "nothing to ask" is an
    answer they act on rather than an error: an unknown name and an entry-point that
    failed to load are both ``None`` here.
    """
    _ensure_adapters_discovered()
    return _ADAPTERS.get(adapter_type)


def ensure_registered_adapter(adapter_type: str) -> None:
    """Host-side guard: assert ``adapter_type`` is a registered adapter.

    The adapter set is open and discovered at runtime (entry-point plugins), so
    ``TaskDescription.adapter_type`` is an open string. Validate it here — on the
    host, where the registry is authoritative — to catch typos/misconfig early with
    a clear error. This is intentionally **not** a Pydantic validator: the runner
    deserializes ``TaskDescription`` and must stay permissive (it is
    adapter-agnostic and may not have a given adapter plugin installed).
    """
    known = available_adapters()
    if adapter_type not in known:
        raise ValueError(
            f"Unknown adapter type: {adapter_type!r}. Available adapters: {known}. "
            "Install the adapter package or check the adapter configuration."
        )


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
    if adapter_type is None or adapter_type == _NATIVE:
        from tolokaforge.adapters.native import NativeAdapter

        return NativeAdapter(params)

    _ensure_adapters_discovered()

    if adapter_type in _ADAPTERS:
        return _ADAPTERS[adapter_type](params)

    # If the adapter was found as an entry-point but failed to load, report the real error
    if adapter_type in _FAILED_ADAPTERS:
        original_exc = _FAILED_ADAPTERS[adapter_type]
        raise ValueError(
            f"Adapter {adapter_type!r} entry-point was found but failed to load: {original_exc}"
        ) from original_exc

    raise ValueError(
        f"Unknown adapter type: {adapter_type!r}. "
        f"Available adapters: {available_adapters()}. "
        f"Install the adapter package or check your configuration."
    )


# Public imports
from tolokaforge.adapters.native import NativeAdapter  # noqa: E402

__all__ = [
    "BaseAdapter",
    "AdapterEnvironment",
    "DockerStackRequirements",
    "NativeAdapter",
    "NativeTaskBundle",
    "adapter_class",
    "available_adapters",
    "get_adapter",
    "register_adapter",
]
