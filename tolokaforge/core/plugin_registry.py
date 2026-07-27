"""Fail-loud entry-point registries for the three orchestrator seams.

External code discovers and loads alternative implementations of the
:class:`~tolokaforge.core.runtime.RuntimeBackend`,
:class:`~tolokaforge.core.trial_grader.TrialGrader`, and
:class:`~tolokaforge.core.conductor.Conductor` Protocols through
``importlib.metadata`` entry-point groups — no in-tree edit, no
monkey-patch. Each entry point resolves to a *factory callable*
(``Callable[[<Context>], <Impl>]``), mirroring the existing
:data:`~tolokaforge.core.conductor.ConductorFactory` idiom; the divergent
impl constructors are adapted to a per-group frozen-dataclass context.

The three groups:

* ``tolokaforge.runtime_backends`` → :data:`RuntimeBackendFactory`
* ``tolokaforge.trial_graders`` → :data:`TrialGraderFactory`
* ``tolokaforge.conductors`` → :data:`~tolokaforge.core.conductor.ConductorFactory`

Discovery is lazy and cached per group; it enumerates ``ep.name`` /
``ep.dist`` **without** calling ``ep.load()``. This splits the fail-loud
policy into two shapes:

* **Duplicate name** — an unresolvable ambiguity — fails at discovery for
  *any* lookup into that group, naming both providing distributions.
* **Broken import** fails only when its own name is requested; a broken
  plug-in never breaks resolution of a healthy sibling, and the import
  error propagates loudly rather than being swallowed.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from tolokaforge.core.conductor import ConductorFactory
from tolokaforge.core.run_display_events import RunDisplayEvents, _NullRunDisplayEvents
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial_grader import TrialGrader

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

    from tolokaforge.core.compose_materialisation import LogCaptureConfig
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import SeedRef
    from tolokaforge.core.trial import EnvironmentManifest

__all__ = [
    "ConductorFactory",
    "DuplicateRegistrationError",
    "RegistryError",
    "RuntimeBackendBuildContext",
    "RuntimeBackendFactory",
    "TrialGraderContext",
    "TrialGraderFactory",
    "UnknownImplementationError",
    "available_conductors",
    "available_runtime_backends",
    "available_trial_graders",
    "load_conductor",
    "load_runtime_backend",
    "load_trial_grader",
]

RUNTIME_BACKENDS_GROUP = "tolokaforge.runtime_backends"
TRIAL_GRADERS_GROUP = "tolokaforge.trial_graders"
CONDUCTORS_GROUP = "tolokaforge.conductors"


# ---------------------------------------------------------------------------
# Fail-loud error surface
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base class for every entry-point registry failure."""


class UnknownImplementationError(RegistryError):
    """No entry point named ``name`` is registered in ``group``.

    The message lists every known registered name so the operator can spot a
    typo or a missing ``pip install`` without reading source.
    """

    def __init__(self, name: str, group: str, known: list[str]) -> None:
        self.name = name
        self.group = group
        self.known = known
        known_str = ", ".join(known) if known else "(none registered)"
        super().__init__(
            f"Unknown implementation {name!r} in entry-point group {group!r}. "
            f"Known names: {known_str}. "
            "Install the providing package or check the configured name."
        )


class DuplicateRegistrationError(RegistryError):
    """Two entry points share ``name`` within ``group``.

    An unresolvable ambiguity: the loader cannot safely pick one for anyone, so
    it fails every lookup into the group and names both providing distributions.
    """

    def __init__(self, name: str, group: str, distributions: tuple[str, str]) -> None:
        self.name = name
        self.group = group
        self.distributions = distributions
        first, second = distributions
        super().__init__(
            f"Duplicate registration of {name!r} in entry-point group {group!r}: "
            f"provided by both {first!r} and {second!r}. "
            "Uninstall or rename one to resolve the ambiguity."
        )


# ---------------------------------------------------------------------------
# Seam types — per-group contexts + factory aliases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeBackendBuildContext:
    """Everything a runtime-backend factory may need to build its backend.

    The union of what any built-in needs — a factory is free to ignore fields
    it does not use (the ``in_memory`` factory reads none of them).
    """

    runner_address: str
    env_manifest: EnvironmentManifest | None
    run_id: str
    seeds: dict[str, SeedRef]
    log_capture: LogCaptureConfig | None
    events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)


@dataclass(frozen=True)
class TrialGraderContext:
    """Dependencies a trial-grader factory receives from the orchestrator."""

    runtime_backend: RuntimeBackend
    logger: StructuredLogger


RuntimeBackendFactory = Callable[[RuntimeBackendBuildContext], RuntimeBackend]
TrialGraderFactory = Callable[[TrialGraderContext], TrialGrader]


# ---------------------------------------------------------------------------
# Discovery internals — one generic scan + resolve shared across the groups
# ---------------------------------------------------------------------------

_discovery_cache: dict[str, dict[str, EntryPoint]] = {}


def _distribution_name(ep: EntryPoint) -> str:
    dist = ep.dist
    return dist.name if dist is not None else "<unknown distribution>"


def _discover(group: str) -> dict[str, EntryPoint]:
    """Return a cached ``name → EntryPoint`` map for ``group``.

    Enumerates names and distributions without importing any target
    (``ep.load()`` is deferred to :func:`_load`). Raises
    :class:`DuplicateRegistrationError` before caching, so a group with a
    duplicate re-raises on every lookup rather than caching a partial map.
    """
    cached = _discovery_cache.get(group)
    if cached is not None:
        return cached

    mapping: dict[str, EntryPoint] = {}
    for ep in importlib.metadata.entry_points(group=group):
        existing = mapping.get(ep.name)
        if existing is not None:
            raise DuplicateRegistrationError(
                ep.name,
                group,
                (_distribution_name(existing), _distribution_name(ep)),
            )
        mapping[ep.name] = ep

    _discovery_cache[group] = mapping
    return mapping


def _load(group: str, name: str) -> object:
    """Resolve ``name`` in ``group`` to its factory, importing the target now.

    An unknown name raises :class:`UnknownImplementationError`; a target that
    raises on ``.load()`` propagates that exception unchanged.
    """
    mapping = _discover(group)
    ep = mapping.get(name)
    if ep is None:
        raise UnknownImplementationError(name, group, sorted(mapping))
    return ep.load()


def _clear_discovery_cache() -> None:
    """Drop the cached per-group maps so the next lookup re-scans.

    The registries cache discovery on first scan; tests that inject different
    entry-point sets call this to restore isolation between cases.
    """
    _discovery_cache.clear()


# ---------------------------------------------------------------------------
# Public loaders + name listings
# ---------------------------------------------------------------------------


def load_runtime_backend(name: str) -> RuntimeBackendFactory:
    """Resolve a registered runtime-backend name to its factory callable."""
    return cast(RuntimeBackendFactory, _load(RUNTIME_BACKENDS_GROUP, name))


def load_trial_grader(name: str) -> TrialGraderFactory:
    """Resolve a registered trial-grader name to its factory callable."""
    return cast(TrialGraderFactory, _load(TRIAL_GRADERS_GROUP, name))


def load_conductor(name: str) -> ConductorFactory:
    """Resolve a registered conductor name to its factory callable."""
    return cast(ConductorFactory, _load(CONDUCTORS_GROUP, name))


def available_runtime_backends() -> list[str]:
    """Sorted names registered in the ``tolokaforge.runtime_backends`` group."""
    return sorted(_discover(RUNTIME_BACKENDS_GROUP))


def available_trial_graders() -> list[str]:
    """Sorted names registered in the ``tolokaforge.trial_graders`` group."""
    return sorted(_discover(TRIAL_GRADERS_GROUP))


def available_conductors() -> list[str]:
    """Sorted names registered in the ``tolokaforge.conductors`` group."""
    return sorted(_discover(CONDUCTORS_GROUP))
