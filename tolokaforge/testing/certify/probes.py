"""Per-capability probe registration for the certification seam.

Out-of-tree consumers (notably the ``tolokaforge-models`` wheel)
register callable probe bodies for a
:class:`~tolokaforge.testing.certify._capability.Capability` — optionally
scoped to one ``model_id`` — via :func:`register_probe`. Lookups go
through :func:`get_probe`, which prefers a model-specific registration
over the capability-wide default.

The registry is a plain module-level dict populated at import time by
decorator application. It is deliberately not shared with
:mod:`tolokaforge.core.plugin_registry` — that module discovers
entry-point plugins across installed distributions, while this registry
is populated in-process by decorated Python callables. The failure
shape on a duplicate registration is intentionally identical: a
:class:`RuntimeError` naming both offenders, so the two seams read the
same way to an operator debugging a bad install.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ._capability import Capability
from .certificate import ModelCertificate

__all__ = ["get_probe", "register_probe", "registered_probes"]

_ProbeCallable = Callable[..., Any]
_RegistryKey = tuple[Capability, str | None]
_F = TypeVar("_F", bound=_ProbeCallable)

_REGISTRY: dict[_RegistryKey, _ProbeCallable] = {}


def register_probe(
    capability: Capability,
    *,
    model_id: str | None = None,
) -> Callable[[_F], _F]:
    """Register a probe body for ``capability``, optionally scoped to one
    ``model_id``.

    Called for its side effect: the returned decorator installs the
    wrapped callable into the module-level registry keyed by
    ``(capability, model_id)``. Passing ``model_id=None`` registers a
    default probe used when no model-specific override exists.

    A duplicate ``(capability, model_id)`` registration raises
    :class:`RuntimeError` — the certification seam obeys the same
    fail-loud discipline as
    :mod:`tolokaforge.core.plugin_registry`.
    """

    def decorator(func: _F) -> _F:
        key: _RegistryKey = (capability, model_id)
        if key in _REGISTRY:
            existing = _REGISTRY[key]
            raise RuntimeError(
                f"Duplicate probe registration for capability={capability.value!r}, "
                f"model_id={model_id!r}: {existing!r} is already registered; "
                f"cannot register {func!r}. Every "
                "(capability, model_id) key admits at most one probe."
            )
        _REGISTRY[key] = func
        return func

    return decorator


def get_probe(
    capability: Capability,
    cert: ModelCertificate,
) -> _ProbeCallable | None:
    """Return the probe registered for ``(capability, cert.model_id)``.

    Falls back to the ``(capability, None)`` default when no
    model-specific probe exists. Returns ``None`` when neither is
    registered — the shared suite body treats this as "no override; use
    the parametrised-pytest default".
    """
    specific = _REGISTRY.get((capability, cert.model_id))
    if specific is not None:
        return specific
    return _REGISTRY.get((capability, None))


def registered_probes() -> Mapping[_RegistryKey, _ProbeCallable]:
    """Read-only snapshot of the current registry."""
    return dict(_REGISTRY)


def _clear_registry_for_tests() -> None:
    """Reset the registry between tests.

    Called only from the certify unit-test fixtures — cross-test
    contamination would silently make a lookup pass that fails in
    isolation.
    """
    _REGISTRY.clear()
