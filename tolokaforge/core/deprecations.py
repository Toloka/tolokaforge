"""Schema-shape deprecation aliases for Project-layer config models.

Every alias that renames or relocates a *YAML key* lives here as a named
coercer invoked from the owning model's ``mode="before"`` validator (or,
for path-shaped inputs, from the loader). Keeping them in one module means
the accept-and-warn surface is auditable in a single place and the models
carry only a one-line call.

Scope boundary: the ``RunConfig`` dual-home lifts (``workers``,
``queue_backend``, ``stuck_heuristics``, …) are *not* here. Those are
field-level orchestrator→compute/storage migrations that resolve values
across two live homes, not schema-shape renames; they stay on
``RunConfig`` where the effective-value accessors read them.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

_NETWORK_POLICY_KEY = "network_policy"
_SECURITY_CONTEXT_RENAMES = {"user": "run_as_user", "group": "run_as_group"}


def warn_deprecated(*, legacy: str, canonical: str, detail: str = "") -> None:
    """Emit a uniform ``DeprecationWarning`` for a legacy→canonical rename.

    ``stacklevel=3`` skips this helper and the calling coercer so the
    warning points past the alias plumbing.
    """
    message = f"{legacy} is deprecated; use {canonical} instead."
    if detail:
        message = f"{message} {detail}"
    warnings.warn(message, DeprecationWarning, stacklevel=3)


def coerce_task_packs_alias(values: Any) -> Any:
    """Accept ``evaluation.task_packs`` as an alias for ``evaluation.projects``.

    Emits a ``DeprecationWarning`` when the legacy key appears with a
    non-empty value and no explicit ``projects`` key. When both keys carry
    values the loader keeps ``projects`` and drops ``task_packs`` (with a
    warning naming the collision).
    """
    if not isinstance(values, dict):
        return values
    legacy = values.get("task_packs")
    canonical = values.get("projects")
    if not legacy:
        return values
    if canonical:
        warnings.warn(
            "evaluation.task_packs and evaluation.projects both set; "
            "projects wins. Drop task_packs from the run config.",
            DeprecationWarning,
            stacklevel=2,
        )
        values["task_packs"] = []
        return values
    warnings.warn(
        "evaluation.task_packs is deprecated; use evaluation.projects "
        "instead. task_packs still accepted as an alias for one release.",
        DeprecationWarning,
        stacklevel=2,
    )
    values["projects"] = list(legacy)
    values["task_packs"] = []
    return values


def coerce_flat_stack_fields(data: Any) -> Any:
    """Accept flat ``compose_file`` / ``runner_service`` at an
    :class:`~tolokaforge.runner.models.EnvironmentPatch` top level and
    normalise them under ``stack``. Emits a ``DeprecationWarning`` when the
    flat shape is used; raises when a key is declared both flat and under
    ``stack``.
    """
    if not isinstance(data, dict):
        return data
    legacy_keys = {k for k in ("compose_file", "runner_service") if k in data}
    if not legacy_keys:
        return data
    stack = dict(data.get("stack") or {})
    for key in sorted(legacy_keys):
        if key in stack:
            raise ValueError(
                f"EnvironmentPatch: both flat {key!r} and stack.{key} declared; "
                "the flat form is legacy — declare it only under stack."
            )
        stack[key] = data.pop(key)
    data["stack"] = stack
    warnings.warn(
        "EnvironmentPatch: flat compose_file / runner_service at the "
        "top level is legacy; move under 'stack:'.",
        DeprecationWarning,
        stacklevel=2,
    )
    return data


def coerce_network_policy_case(data: Any) -> Any:
    """Accept uppercase ``network_policy`` enum names (``NO_INTERNET`` …),
    lowercase them to the canonical enum values, and warn. Canonical
    lowercase values pass through untouched.
    """
    if not isinstance(data, dict):
        return data
    value = data.get(_NETWORK_POLICY_KEY)
    if not isinstance(value, str):
        return data
    lowered = value.lower()
    if lowered == value:
        return data
    data[_NETWORK_POLICY_KEY] = lowered
    warn_deprecated(
        legacy=f"network_policy: {value}",
        canonical=f"network_policy: {lowered}",
        detail="Network policy enum values are lowercase.",
    )
    return data


def coerce_security_context_aliases(data: Any) -> Any:
    """Accept ``user`` / ``group`` as aliases for
    :class:`~tolokaforge.runner.models.SecurityContext`'s ``run_as_user`` /
    ``run_as_group``. Renames and warns. A single source declaring both the
    legacy and canonical key with disagreeing values fails loud.
    """
    if not isinstance(data, dict):
        return data
    for legacy, canonical in _SECURITY_CONTEXT_RENAMES.items():
        if legacy not in data:
            continue
        legacy_value = data[legacy]
        if canonical in data and data[canonical] != legacy_value:
            raise ValueError(
                f"SecurityContext: legacy {legacy!r}={legacy_value!r} conflicts with "
                f"{canonical!r}={data[canonical]!r}; declare only {canonical!r}."
            )
        data.pop(legacy)
        data[canonical] = legacy_value
        warn_deprecated(
            legacy=f"SecurityContext.{legacy}",
            canonical=f"SecurityContext.{canonical}",
        )
    return data


def warn_legacy_run_config_dir(config_path: Path) -> None:
    """Emit a ``DeprecationWarning`` when a run config sits under
    ``run_config/`` (singular) instead of ``run_configs/`` (plural).
    """
    warnings.warn(
        f"Run config {config_path} sits under 'run_config/' (singular); the "
        f"canonical directory is 'run_configs/' (plural). Rename the "
        f"directory to remove this warning.",
        DeprecationWarning,
        stacklevel=2,
    )
