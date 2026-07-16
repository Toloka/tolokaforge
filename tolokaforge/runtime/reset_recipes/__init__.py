"""Reset-recipe registry — one dispatcher per :data:`SeedKind`.

A service labelled ``reset`` in an :class:`EnvironmentManifest` names a
seed from ``project.assets.seeds``. The seed's ``kind`` selects a
:class:`RecipeDispatcher` from :data:`RECIPE_REGISTRY`; the backend
calls the dispatcher's :meth:`RecipeDispatcher.apply` at the reset seam
(per-trial provision for :class:`PerTrialRuntimeBackend`, between-trial
hook for :class:`SharedStackRuntimeBackend`).

The registry is populated at import time by each recipe module below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tolokaforge.core.models import SeedKind, SeedRef

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose


@runtime_checkable
class RecipeDispatcher(Protocol):
    """Applies a named seed to a service on a live compose stack.

    ``apply`` is called once per reset event (per trial in per-trial
    mode, at trial boundaries in shared mode). Implementations are
    expected to be idempotent — a re-run against the same substrate
    lands the service in the same state.
    """

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None: ...


RECIPE_REGISTRY: dict[SeedKind, RecipeDispatcher] = {}
"""Map from seed kind to its dispatcher. Populated at import time by
each recipe module (:mod:`sql_dump`, :mod:`filesystem_dir`,
:mod:`redis_dump`, :mod:`bare`)."""


def dispatch(
    seed: SeedRef,
    service_name: str,
    compose: DockerCompose,
) -> None:
    """Look up the recipe for ``seed.kind`` and apply it.

    Raises :class:`KeyError` when the seed kind has no registered
    dispatcher — this only happens if a new :data:`SeedKind` literal is
    added without a matching recipe module import.
    """
    dispatcher = RECIPE_REGISTRY[seed.kind]
    dispatcher.apply(seed, service_name, compose)


def _register_builtin_dispatchers() -> None:
    """Import each shipped recipe module so its module-level dispatcher
    registers itself in :data:`RECIPE_REGISTRY`. Runs once at package
    import time — recipes cannot be looked up before this returns.
    """
    from tolokaforge.runtime.reset_recipes import bare, filesystem_dir, redis_dump, sql_dump

    # References silence linters and keep the modules alive for their
    # side-effect (registry entry) even under aggressive dead-code passes.
    _ = (bare, filesystem_dir, redis_dump, sql_dump)


_register_builtin_dispatchers()

__all__ = ["RECIPE_REGISTRY", "RecipeDispatcher", "dispatch"]
