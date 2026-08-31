"""Service lifecycle dispatchers — one per :data:`ServiceIsolation` label.

ADR-0044 factors the between-trials service cycle into three closed
labels — ``shared`` (persist), ``reset`` (seed-restore), ``ephemeral``
(rm+recreate). Each dispatcher owns the transitions for its label
against a docker-compose stack. A composer resolves the dispatcher for
a service at cycle time via :data:`DISPATCHER_REGISTRY`.

The three built-in dispatchers register themselves at import time, the
same idiom :mod:`tolokaforge.runtime.reset_recipes` uses for its
:data:`RECIPE_REGISTRY`. A missing label at dispatch time is a typed
:class:`ProvisionError` with ``stage="cycle"`` — the composer catches
and re-raises with the stack + scope enriched onto the reason.

Failure text for the ``reset`` dispatcher matches today's
:meth:`PerTrialRuntimeBackend._apply_reset_recipes` verbatim in its
``reason`` string — the composer supplies the outer ``stage`` /
``trial_id`` when it wraps the per-trial provision path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from tolokaforge.core.composition_runtime import (
    ComposeMaterialiser,
    ServiceLifecycleDispatcher,
    StackHandle,
)
from tolokaforge.core.docker_compose_materialiser import _DockerComposeStackHandle
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.runner.models import ServiceIsolation, ServiceSpec
from tolokaforge.runtime.reset_recipes import dispatch as recipe_dispatch

__all__ = [
    "DISPATCHER_REGISTRY",
    "EphemeralDispatcher",
    "ResetDispatcher",
    "SharedDispatcher",
    "dispatch",
]


@dataclass(frozen=True)
class SharedDispatcher:
    """``isolation="shared"`` — the service persists across trials.

    ``cycle`` is a no-op by construction: a shared service is expected to
    survive from run start to run teardown, so between-trial cycling
    would violate the label's contract.
    """

    isolation: ClassVar[ServiceIsolation] = "shared"

    def cycle(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        materialiser: ComposeMaterialiser,
        *,
        seeds: Mapping[str, SeedRef],
    ) -> None:
        return None


@dataclass(frozen=True)
class ResetDispatcher:
    """``isolation="reset"`` — restore the service from its named seed.

    Delegates to :func:`tolokaforge.runtime.reset_recipes.dispatch`, which
    resolves the seed's kind to a :class:`RecipeDispatcher`. Refusal and
    failure ``reason`` text matches today's
    :meth:`PerTrialRuntimeBackend._apply_reset_recipes` verbatim.
    """

    isolation: ClassVar[ServiceIsolation] = "reset"

    def cycle(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        materialiser: ComposeMaterialiser,
        *,
        seeds: Mapping[str, SeedRef],
    ) -> None:
        typed = _cast_handle(stack_handle)
        if service_spec.reset is None:
            raise ProvisionError(
                trial_id=typed.stack_id,
                stage="cycle",
                reason=(
                    f"service {service_name!r} labelled 'reset' has no "
                    "'reset.seed' pointer — schema validation should have "
                    "rejected the manifest earlier."
                ),
            )
        seed_name = service_spec.reset.seed
        seed = seeds.get(seed_name)
        if seed is None:
            raise ProvisionError(
                trial_id=typed.stack_id,
                stage="cycle",
                reason=(
                    f"service {service_name!r} names seed {seed_name!r} but "
                    f"the backend has no such seed in its registry "
                    f"(available: {sorted(seeds)!r})."
                ),
            )
        try:
            recipe_dispatch(seed, service_name, typed.compose)
        except RuntimeError as exc:
            raise ProvisionError(
                trial_id=typed.stack_id,
                stage="cycle",
                reason=(
                    f"reset recipe for service {service_name!r} "
                    f"(seed {seed_name!r}, kind {seed.kind!r}) failed: {exc}"
                ),
            ) from exc


@dataclass
class EphemeralDispatcher:
    """``isolation="ephemeral"`` — remove + recreate the service.

    Drives a targeted ``docker compose rm -f -v <svc>`` followed by
    ``docker compose up -d --wait <svc>`` against the stack's live
    compose project. The compose command prefix and ``cwd`` come from the
    handle's own :class:`DockerCompose`, so the project name matches the
    one testcontainers created (derived from ``context`` basename); no
    parallel ``-p`` string bookkeeping. :attr:`subprocess_runner` is a
    seam for tests to substitute an in-process fake without patching
    :mod:`subprocess`.
    """

    isolation: ClassVar[ServiceIsolation] = "ephemeral"
    subprocess_runner: Callable[..., subprocess.CompletedProcess[Any]] = field(
        default=subprocess.run
    )

    def cycle(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        materialiser: ComposeMaterialiser,
        *,
        seeds: Mapping[str, SeedRef],
    ) -> None:
        typed = _cast_handle(stack_handle)
        base_cmd = list(typed.compose.docker_compose_command)
        cwd = str(typed.compose.context)
        for verb, verb_args in (
            ("rm", ("-f", "-v", service_name)),
            ("up", ("-d", "--wait", service_name)),
        ):
            cmd = [*base_cmd, verb, *verb_args]
            try:
                self.subprocess_runner(
                    cmd,
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                raise ProvisionError(
                    trial_id=typed.stack_id,
                    stage="cycle",
                    reason=(
                        f"docker compose {verb} failed for ephemeral service "
                        f"{service_name!r} on stack {typed.stack_id!r}: "
                        f"exit={exc.returncode}"
                    ),
                ) from exc


DISPATCHER_REGISTRY: dict[ServiceIsolation, ServiceLifecycleDispatcher] = {}
"""Map from :data:`ServiceIsolation` label to its dispatcher. Populated at
import time by :func:`_register_builtin_dispatchers`; a composer copies
this map at construction so per-instance overrides never mutate the
global."""


def dispatch(
    service_name: str,
    service_spec: ServiceSpec,
    stack_handle: StackHandle,
    materialiser: ComposeMaterialiser,
    *,
    seeds: Mapping[str, SeedRef],
) -> None:
    """Look up the dispatcher for ``service_spec.isolation`` and cycle.

    Refuses with :class:`ProvisionError` (``stage="cycle"``) when the
    label has no registered dispatcher — the closed
    :data:`ServiceIsolation` vocab keeps this off the hot path in
    production; the refusal fires only when a caller passes a registry
    with a hole. The composer catches this and enriches the reason with
    ``(stack_id, service_name, scope)``.
    """
    try:
        dispatcher = DISPATCHER_REGISTRY[service_spec.isolation]
    except KeyError as exc:
        raise ProvisionError(
            trial_id=stack_handle.stack_id,
            stage="cycle",
            reason=(f"no dispatcher registered for isolation label {service_spec.isolation!r}"),
        ) from exc
    dispatcher.cycle(
        service_name,
        service_spec,
        stack_handle,
        materialiser,
        seeds=seeds,
    )


def _cast_handle(handle: StackHandle) -> _DockerComposeStackHandle:
    """Narrow a :class:`StackHandle` to the docker-compose materialiser's
    private handle. Foreign handles raise :class:`TypeError` naming both
    families — a dispatcher must refuse a handle another materialiser
    family produced rather than reach for compose attributes that only
    the docker-compose handle carries.
    """
    if not isinstance(handle, _DockerComposeStackHandle):
        raise TypeError(
            f"ServiceLifecycleDispatcher expected a _DockerComposeStackHandle; "
            f"got {type(handle).__name__}."
        )
    return handle


def _register_builtin_dispatchers() -> None:
    """Populate :data:`DISPATCHER_REGISTRY` with the three shipped
    dispatchers. Runs once at module import time — a composer cannot see
    the built-ins before this returns, and the module has no other
    side-effect surface.
    """
    DISPATCHER_REGISTRY["shared"] = SharedDispatcher()
    DISPATCHER_REGISTRY["reset"] = ResetDispatcher()
    DISPATCHER_REGISTRY["ephemeral"] = EphemeralDispatcher()


_register_builtin_dispatchers()
