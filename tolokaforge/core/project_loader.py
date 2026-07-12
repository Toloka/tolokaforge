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
- :func:`resolve_effective_task_data` — apply ``project.task_defaults`` under
  a task-yaml dict.
- :func:`detect_project_layout` — resolve the enclosing project root and
  flag whether the run config sits under the legacy ``run_config/``
  (singular) directory.

The task-side merge in this module operates on **dicts** so it plugs into
the existing :mod:`tolokaforge.adapters._task_loader` flow, which merges
domain / task dicts before Pydantic validation.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.models import ProjectConfig, TaskDefaults

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
    """
    env = data.get("default_environment")
    if isinstance(env, dict):
        compose = env.get("compose_file")
        if isinstance(compose, str) and compose:
            resolved = Path(compose)
            if not resolved.is_absolute():
                env["compose_file"] = str((project_dir / resolved).resolve())
    task_defaults = data.get("task_defaults")
    if isinstance(task_defaults, dict):
        prompt = task_defaults.get("system_prompt")
        if isinstance(prompt, str) and prompt:
            resolved = Path(prompt)
            if not resolved.is_absolute():
                task_defaults["system_prompt"] = str((project_dir / resolved).resolve())


def synthesize_default_project(
    *,
    project_root: Path,
    task_defaults: TaskDefaults | None = None,
) -> ProjectConfig:
    """Return a minimal ``ProjectConfig`` for packs without a
    ``project.yaml``.

    Emits an info-level log line so operators can see the fallback took
    effect. Used by the CLI to keep old-shape packs loading while the
    strict-validation milestone hasn't landed yet.
    """
    logger.info(
        "project.yaml not found under %s; using synthesised default (transitional)",
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


def resolve_effective_task_data(
    project: ProjectConfig | None,
    task_data: dict[str, Any],
) -> dict[str, Any]:
    """Layer *project.task_defaults* under *task_data*.

    The task fields (``task_data``) win on conflict. Called by
    :mod:`tolokaforge.adapters._task_loader` after any adapter-side
    Domain merge and before ``TaskConfig`` validation. When *project* is
    ``None`` or its ``task_defaults`` are effectively empty, *task_data*
    is returned unchanged.
    """
    if project is None:
        return dict(task_data)
    # ``exclude_defaults`` skips fields the project author didn't
    # override (None for optionals, {} / [] for containers) so we
    # never merge a schema-default value into the task dict.
    defaults = project.task_defaults.model_dump(exclude_defaults=True)
    if not defaults:
        return dict(task_data)
    return deep_merge(defaults, task_data)


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
