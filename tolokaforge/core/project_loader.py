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

    Two field families are covered:

    - ``default_environment.compose_file`` — the substrate pointer.
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
        compose = env.get("compose_file")
        if isinstance(compose, str) and compose:
            resolved = Path(compose)
            if not resolved.is_absolute():
                env["compose_file"] = str((project_dir / resolved).resolve())
    task_defaults = data.get("task_defaults")
    if isinstance(task_defaults, dict):
        _rewrite_task_defaults_paths(task_defaults, project_dir)


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
            f"Run config {config_path} is not a YAML mapping " f"(got {type(config_data).__name__})"
        )

    project_root, used_legacy_dir = detect_project_layout(config_path)
    if used_legacy_dir:
        warn_legacy_run_config_dir(config_path)
    if project_root is not None:
        project = load_project_config(project_root / PROJECT_FILENAME)
    else:
        project = synthesize_default_project(project_root=config_path.parent)
    merged = resolve_effective_run_config_data(project, config_data)
    return merged, project


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
