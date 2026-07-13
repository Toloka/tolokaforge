"""Project loader — resolves ``project.yaml`` + ``run_configs/*.yaml`` layering.

Turns a run-config file path into an effective ``RunConfig`` by finding the
enclosing ``project.yaml`` (walking up from the config file) and deep-merging
``project.run_defaults`` under the selected run config. Also exposes the
task-side merge (``project.task_defaults`` under each ``task.yaml``) used by
the adapter layer.

Public helpers:

- :func:`find_project_yaml` — walk up from a start path looking for
  ``project.yaml``.
- :func:`load_project_config` — read + validate a ``project.yaml`` file.
- :func:`synthesize_default_project` — build a minimal ``ProjectConfig`` for
  packs that don't ship a ``project.yaml``. Emits an info-level log line so
  the fallback is visible without being a warning.
- :func:`deep_merge` — recursive dict merge; delta wins on conflict.
- :func:`resolve_effective_run_config_data` — apply ``project.run_defaults``
  under a run-config dict.
- :func:`detect_project_layout` — resolve the enclosing project root and
  flag whether the run config sits under the legacy ``run_config/``
  (singular) directory.

The task-side merge is executed inside
:func:`tolokaforge.adapters._task_loader.load_task_yaml`, which imports
``deep_merge`` from here and layers the project ``task_defaults`` under
each task's own fields between the adapter's Domain merge and the
task-yaml delta.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.models import (
    EnvironmentManifest,
    EnvironmentPatch,
    ProjectConfig,
    TaskDefaults,
)

logger = logging.getLogger(__name__)


# ── Filesystem discovery ────────────────────────────────────────────────


PROJECT_FILENAME = "project.yaml"
CANONICAL_RUN_CONFIGS_DIR = "run_configs"
LEGACY_RUN_CONFIG_DIR = "run_config"


def find_project_yaml(start: Path, *, max_depth: int = 8) -> Path | None:
    """Walk up from *start* looking for ``project.yaml``.

    Stops after ``max_depth`` levels so an unusual mount layout can't spin.
    Returns the resolved path to the file, or ``None`` if none was found
    within the depth budget.

    ``start`` may be a file or a directory; the search begins at
    ``start.parent`` if it is a file.
    """
    start = start.resolve()
    current = start if start.is_dir() else start.parent
    for _ in range(max_depth + 1):
        candidate = current / PROJECT_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:  # filesystem root
            return None
        current = current.parent
    return None


def detect_project_layout(config_path: Path) -> tuple[Path | None, bool]:
    """Given a ``--config`` path, return the enclosing project root and a
    flag indicating whether the run config sits under the legacy
    ``run_config/`` (singular) directory.

    - ``project_root`` is the directory containing the discovered
      ``project.yaml``, or ``None`` if no project was found within the
      search budget.
    - ``used_legacy_dir`` is ``True`` when the config path's immediate
      parent directory is named ``run_config/`` (legacy singular) rather
      than ``run_configs/`` (canonical plural).
    """
    config_path = config_path.resolve()
    project_yaml = find_project_yaml(config_path)
    project_root = project_yaml.parent if project_yaml else None
    used_legacy_dir = config_path.is_file() and config_path.parent.name == LEGACY_RUN_CONFIG_DIR
    return project_root, used_legacy_dir


# ── Load + synthesise ──────────────────────────────────────────────────


def load_project_config(path: Path) -> ProjectConfig:
    """Read and validate a ``project.yaml`` file.

    Raises ``FileNotFoundError`` if the file does not exist, ``RuntimeError``
    if the YAML is not a mapping, and ``pydantic.ValidationError`` if the
    contents don't validate against :class:`ProjectConfig`.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"project.yaml not found at {path}")
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"project.yaml at {path} is not a YAML mapping (got {type(data).__name__})"
        )
    # Resolve default_environment.compose_file relative to the project dir
    # so EnvironmentManifest's validator can locate it regardless of CWD.
    _resolve_project_paths(data, path.parent)
    return ProjectConfig(**data)


def _resolve_project_paths(data: dict, project_dir: Path) -> None:
    """Rewrite relative paths in *data* to absolute paths under
    *project_dir*, in place. No-op if the fields are absent or already
    absolute.

    Two field families are covered:

    - ``default_environment.stack.compose_file`` (canonical) or
      ``default_environment.compose_file`` (legacy flat form) — the
      substrate pointer.
    - Every path field inside ``task_defaults`` that a per-task
      ``task.yaml`` may carry (``system_prompt``, ``grading``,
      ``tools.{agent,user}.mcp_server``, ``initial_state.json_db``,
      ``initial_state.system_prompt``, ``initial_state.filesystem.copy[].from``).
      The task loader's ``_PATH_FIELD_REWRITERS`` is the canonical
      enumeration; this function reuses it so a project-level default
      resolves the same way a task-level value does.
    """
    env = data.get("default_environment")
    if isinstance(env, dict):
        _anchor_stack_compose_file(env, project_dir)
    assets = data.get("assets")
    if isinstance(assets, dict):
        _anchor_seed_paths(assets, project_dir)
    task_defaults = data.get("task_defaults")
    if isinstance(task_defaults, dict):
        _rewrite_task_defaults_paths(task_defaults, project_dir)


def _anchor_seed_paths(assets_data: dict, project_dir: Path) -> None:
    """Rewrite every ``assets.seeds.<name>`` entry so its path is
    absolute under *project_dir*. Handles both authoring shapes: the
    full ``{path, kind, ...}`` dict and the bare-string shorthand.
    In-place; no-op when ``seeds`` is absent or an entry is already
    absolute.

    Runs before Pydantic constructs :class:`SeedRef`, so the model
    receives an anchored path. Bare-string shorthand is preserved as a
    string here — :class:`SeedRef`'s ``mode="before"`` normaliser
    coerces it to the dict form after anchoring.
    """
    seeds = assets_data.get("seeds")
    if not isinstance(seeds, dict):
        return
    for name, entry in seeds.items():
        if isinstance(entry, str):
            resolved = Path(entry)
            if not resolved.is_absolute():
                seeds[name] = str((project_dir / resolved).resolve())
        elif isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str) and path:
                resolved = Path(path)
                if not resolved.is_absolute():
                    entry["path"] = str((project_dir / resolved).resolve())


def _anchor_stack_compose_file(env_patch: dict, anchor_dir: Path) -> None:
    """Rewrite ``env_patch.stack.compose_file`` (or the legacy flat
    ``env_patch.compose_file``) to an absolute path under *anchor_dir*.
    In-place; no-op when the field is absent or already absolute.
    """
    stack = env_patch.get("stack")
    if isinstance(stack, dict):
        compose = stack.get("compose_file")
        if isinstance(compose, str) and compose:
            resolved = Path(compose)
            if not resolved.is_absolute():
                stack["compose_file"] = str((anchor_dir / resolved).resolve())
    compose = env_patch.get("compose_file")
    if isinstance(compose, str) and compose:
        resolved = Path(compose)
        if not resolved.is_absolute():
            env_patch["compose_file"] = str((anchor_dir / resolved).resolve())


def _rewrite_task_defaults_paths(task_defaults: dict, project_dir: Path) -> None:
    """Rewrite every path-bearing field inside ``task_defaults`` from a
    project-relative string to an absolute path under *project_dir*.

    Imports ``_PATH_FIELD_REWRITERS`` from the task loader so the field
    set stays a single declaration — adding a new task-level path field
    on that side automatically covers the project-level default here.
    Silently skips any field whose value is not a relative string.
    """
    from tolokaforge.adapters._task_loader import _PATH_FIELD_REWRITERS

    def rewrite(val: str) -> str:
        resolved = Path(val)
        if resolved.is_absolute():
            return val
        return str((project_dir / resolved).resolve())

    for rewriter in _PATH_FIELD_REWRITERS:
        rewriter(task_defaults, rewrite)


def synthesize_default_project(
    *,
    project_root: Path,
    task_defaults: TaskDefaults | None = None,
) -> ProjectConfig:
    """Return a minimal ``ProjectConfig`` used when a pack does not ship
    a ``project.yaml``.

    Emits an info-level log line so operators can see the synthesised
    fallback took effect. Loaders route through here whenever
    ``find_project_yaml`` returns ``None`` so downstream code always
    has a ``ProjectConfig`` to consume.
    """
    logger.info(
        "project.yaml not found under %s; using synthesised default",
        project_root,
    )
    return ProjectConfig(
        name=project_root.name or "synthesised",
        description=None,
        task_defaults=task_defaults or TaskDefaults(),
    )


# ── Deep merge ─────────────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *delta* into *base*; *delta* wins on conflict.

    Nested dicts merge key-by-key. Lists and scalars from *delta* replace
    the corresponding value in *base* — split a structure across both
    sides only when its inner values are identical for every case;
    otherwise keep it whole on one side. Returns a new dict; neither
    input is mutated.
    """
    result: dict[str, Any] = dict(base)
    for key, val in delta.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(val, dict):
            result[key] = deep_merge(existing, val)
        else:
            result[key] = val
    return result


# ── Effective config resolvers ─────────────────────────────────────────


def resolve_effective_run_config_data(
    project: ProjectConfig | None,
    run_config_data: dict[str, Any],
) -> dict[str, Any]:
    """Layer *project.run_defaults* under *run_config_data*.

    Returns a new dict shaped for ``RunConfig(**...)``. The selected run
    config's fields win on conflict; anything the run config omits
    inherits from ``run_defaults``. When *project* is ``None`` or the
    project declares no ``run_defaults``, *run_config_data* is returned
    unchanged.
    """
    if project is None or project.run_defaults is None:
        return dict(run_config_data)
    # ``exclude_defaults`` drops fields whose value equals the schema
    # default (None for optionals, {} / [] for containers). Every
    # dropped key therefore adds no information — merging them in
    # would just repeat the schema default at merge time.
    base = project.run_defaults.model_dump(exclude_defaults=True)
    return deep_merge(base, run_config_data)


# ── High-level loader entry point ──────────────────────────────────────


def load_effective_run_config(
    config_path: Path,
) -> tuple[dict[str, Any], ProjectConfig]:
    """Load the run-config YAML at *config_path* and layer
    ``project.run_defaults`` under it.

    Returns ``(config_data, project)``:

    - ``config_data`` is the merged dict, ready to feed into
      ``RunConfig(**config_data)``.
    - ``project`` is the enclosing ``ProjectConfig`` (loaded from the
      discovered ``project.yaml``) or a synthesised default for packs
      that don't ship one.

    Emits a ``DeprecationWarning`` when the run config sits under the
    legacy ``run_config/`` (singular) directory.

    Every CLI subcommand that constructs a ``RunConfig`` from disk
    should route through here so that ``project.run_defaults`` reaches
    every code path uniformly. Direct ``yaml.safe_load`` + ``RunConfig``
    construction skips the merge and produces a config that behaves
    differently between ``run`` and any other subcommand.
    """
    config_path = Path(config_path).resolve()
    with config_path.open() as f:
        config_data = yaml.safe_load(f) or {}
    if not isinstance(config_data, dict):
        raise RuntimeError(
            f"Run config {config_path} is not a YAML mapping (got {type(config_data).__name__})"
        )

    project_root, used_legacy_dir = detect_project_layout(config_path)
    if used_legacy_dir:
        warn_legacy_run_config_dir(config_path)
    if project_root is not None:
        project = load_project_config(project_root / PROJECT_FILENAME)
    else:
        project = synthesize_default_project(project_root=config_path.parent)
    merged = resolve_effective_run_config_data(project, config_data)
    validate_actor_roster_subset_of_models(project, merged)
    return merged, project


def validate_actor_roster_subset_of_models(
    project: ProjectConfig,
    merged_run_config: dict[str, Any],
) -> None:
    """Every ``actors.<name>`` declaration with ``mode == "llm"`` must
    have a matching entry in the resolved ``models`` dict.

    Raises ``ValueError`` naming the missing model(s). Runs after
    ``project.run_defaults`` merges into the selected run config —
    that's the only point where a project-side actor roster and the
    run-side model roster are both visible.

    A ``None`` roster (project sets no ``actors``) is a no-op. Actor
    entries without ``mode == "llm"`` are ignored — scripted actors
    don't need a model. This is a schema-time cross-check; runtime
    binding lives in the actor rename milestone.
    """
    actors = project.task_defaults.actors
    if not actors:
        return
    models = merged_run_config.get("models") or {}
    if not isinstance(models, dict):
        return
    missing = sorted(
        name for name, spec in actors.items() if spec.mode == "llm" and name not in models
    )
    if missing:
        raise ValueError(
            f"Actor roster references models {missing!r} that are not declared "
            f"under `models`; declared: {sorted(models)!r}."
        )


# ── Environment resolve ────────────────────────────────────────────────


_POLICY_REQUEST_FIELDS = ("network_policy", "security_context_defaults")
"""Fields that survive atomic ``stack`` replacement — policy requests
that are substrate-neutral (they describe the trial regardless of
substrate)."""

_SERVICE_TREATMENT_FIELDS = ("initial_state", "isolation")
"""Fields that are scoped to the reviewed stack — discarded on atomic
``stack`` replacement (the project's opt-outs reviewed the project's
services, not the replacement stack)."""


def resolve(
    project_env: EnvironmentPatch | None,
    task_env: EnvironmentPatch | None,
) -> EnvironmentManifest | None:
    """Bind a project-side and task-side :class:`EnvironmentPatch` pair
    to an :class:`EnvironmentManifest`.

    Deep-merges the two patches (task wins on conflict), then materialises
    the manifest — the point where the disk-touching validators
    (compose-file existence, safety checks) run. Returns ``None`` when
    neither side declares an environment; raises ``ValueError`` when the
    merged patch has no ``compose_file`` (the manifest would be
    unconstructible).

    Atomic ``stack`` replacement — the trigger is the presence of the
    ``compose_file`` key on the task's ``stack`` patch, never path
    identity. When it fires:

    - The task's ``stack`` replaces the project's ``stack`` outright —
      clean slate of ``inputs`` and ``runner_service`` (a foreign
      compose file's ``${var}`` slots must never silently capture
      inherited values).
    - Service-treatment fields (``initial_state``, root ``isolation``)
      are discarded — the project's opt-outs reviewed the project's
      services, not the replacement stack.
    - Policy-request fields (``network_policy``,
      ``security_context_defaults``) survive — substrate-neutral, they
      describe the trial regardless of substrate.

    Anchoring: ``stack.compose_file`` paths must already be absolute
    when this runs. The project loader and the task loader anchor them
    to the file that declared them before ``ProjectConfig`` /
    ``TaskConfig`` are constructed; this function assumes that
    invariant.
    """
    if project_env is None and task_env is None:
        return None

    project_data = _dump_patch(project_env)
    task_data = _dump_patch(task_env)

    merged = _merge_env_patches(project_data, task_data)

    stack = merged.get("stack") or {}
    compose_file = stack.get("compose_file")
    if not compose_file:
        raise ValueError(
            "EnvironmentPatch resolve produced no compose_file — either the "
            "project's default_environment or the task's environment_manifest "
            "must declare `stack.compose_file`."
        )

    manifest_kwargs: dict[str, Any] = {
        "compose_file": compose_file,
        "stack_inputs": dict(stack.get("inputs") or {}),
    }
    runner_service = stack.get("runner_service")
    if runner_service:
        manifest_kwargs["runner_service"] = runner_service
    for field in (*_SERVICE_TREATMENT_FIELDS, *_POLICY_REQUEST_FIELDS):
        value = merged.get(field)
        if value is None:
            continue
        manifest_kwargs[field] = value

    return EnvironmentManifest(**manifest_kwargs)


def _dump_patch(patch: EnvironmentPatch | None) -> dict[str, Any]:
    """Return a merge-friendly dict view of *patch*. Empty dict when
    *patch* is ``None``; drops fields left at their patch defaults
    (all ``None``) so that later layers cleanly overwrite unset ones.
    """
    if patch is None:
        return {}
    return patch.model_dump(exclude_none=True, mode="python")


def _merge_env_patches(
    project: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge two patch dicts with the atomic-``stack`` rule.

    Standard deep-merge (task wins) applies to every field except
    ``stack``. The atomic rule fires only when the task declares
    ``stack.compose_file``: the task's ``stack`` replaces the project's
    entirely, and service-treatment fields are dropped so they don't
    silently extend from a reviewed stack to an unreviewed one.
    """
    task_stack = task.get("stack")
    stack_replacement = isinstance(task_stack, dict) and "compose_file" in task_stack
    if not stack_replacement:
        return deep_merge(project, task)

    merged: dict[str, Any] = {"stack": dict(task_stack)}
    for field in _SERVICE_TREATMENT_FIELDS:
        if field in task:
            merged[field] = task[field]
    for field in _POLICY_REQUEST_FIELDS:
        if field in task:
            merged[field] = task[field]
        elif field in project:
            merged[field] = project[field]
    return merged


# ── Legacy alias warnings ──────────────────────────────────────────────


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
