"""Per-service isolation resolver.

Substrate-agnostic helpers that layer a task-side
:class:`EnvironmentPatch` over a project-side one to produce the merged
per-service isolation map an :class:`EnvironmentManifest` carries.

Two entry points:

- :func:`resolve_service_isolation` — return the effective
  :data:`ServiceIsolation` label for a single compose service.
- :func:`resolve_service_specs` — return the full merged
  ``{service_name: ServiceSpec}`` map with ``ephemeral`` defaults filled
  for every compose service missing from both patches.

Both consume :class:`EnvironmentPatch` inputs only — no filesystem I/O,
no Pydantic construction of :class:`EnvironmentManifest`. The
project loader calls into these after its dict-level merge; runtime
backends may call them directly against an already-resolved manifest's
compose-service list.
"""

from __future__ import annotations

from collections.abc import Iterable

from tolokaforge.runner.models import (
    EnvironmentPatch,
    ServiceIsolation,
    ServiceSpec,
)

_DEFAULT_ISOLATION: ServiceIsolation = "ephemeral"
"""Fallback label for a compose service that neither the project nor the
task declares. Mirrors :attr:`EnvironmentManifest.requires_per_trial`'s
safety default — an unlabelled service opts into fresh-per-trial
materialisation."""


def resolve_service_isolation(
    project_env: EnvironmentPatch | None,
    task_env: EnvironmentPatch | None,
    compose_service: str,
) -> ServiceIsolation:
    """Return the effective isolation label for ``compose_service``.

    Precedence: task-side patch wins over project-side patch. When
    neither side names the service, the helper returns ``ephemeral``
    (the unlabelled default from ADR-0018's amendment).
    """
    task_spec = _service_from_patch(task_env, compose_service)
    if task_spec is not None:
        return task_spec.isolation
    project_spec = _service_from_patch(project_env, compose_service)
    if project_spec is not None:
        return project_spec.isolation
    return _DEFAULT_ISOLATION


def resolve_service_specs(
    project_env: EnvironmentPatch | None,
    task_env: EnvironmentPatch | None,
    compose_service_names: Iterable[str],
) -> dict[str, ServiceSpec]:
    """Return the merged ``{service_name: ServiceSpec}`` map for every
    name in *compose_service_names*.

    Task-side entries win over project-side entries per service (deep
    merge at the map level, not inside a single :class:`ServiceSpec`
    — a task that overrides a service replaces the project's full
    :class:`ServiceSpec` for that name so a mismatched ``reset``
    sibling can't leak through). Services absent from both sides fill
    with ``ServiceSpec(isolation="ephemeral")``.

    The plural form is what :func:`tolokaforge.core.project_loader.resolve`
    consumes; :func:`resolve_service_isolation` is the single-service
    convenience for callers that walk a manifest's already-resolved
    services map.
    """
    resolved: dict[str, ServiceSpec] = {}
    for name in compose_service_names:
        task_spec = _service_from_patch(task_env, name)
        if task_spec is not None:
            resolved[name] = task_spec
            continue
        project_spec = _service_from_patch(project_env, name)
        if project_spec is not None:
            resolved[name] = project_spec
            continue
        resolved[name] = ServiceSpec(isolation=_DEFAULT_ISOLATION)
    return resolved


def _service_from_patch(
    patch: EnvironmentPatch | None,
    compose_service: str,
) -> ServiceSpec | None:
    if patch is None or patch.services is None:
        return None
    return patch.services.get(compose_service)
