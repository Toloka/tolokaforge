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
- :func:`synthesize_default_project` — build a minimal ``ProjectConfig``
  for a pack that ships no ``project.yaml``.
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

import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from tolokaforge.core.assets import compute_seed_digest
from tolokaforge.core.deprecations import (
    POST_M9_STRICT_FLIP_ISSUE,
    canonicalize_actor_config,
    source_context,
    warn_deprecated,
    warn_legacy_run_config_dir,
)
from tolokaforge.core.grading.unknown_keys import suggest_closest_field
from tolokaforge.core.models import (
    EnvironmentManifest,
    EnvironmentPatch,
    GradingCombineConfig,
    JudgeCustomization,
    ProjectConfig,
    ServiceSpec,
    TaskDefaults,
)

# ── Config construction ─────────────────────────────────────────────────

_ConfigT = TypeVar("_ConfigT", bound=BaseModel)


def construct_config(
    model: type[_ConfigT], data: dict[str, Any], *, source: Path, section: str = ""
) -> _ConfigT:
    """Construct a Project-layer config model, warning on each unknown
    top-level key before the model's ``extra="ignore"`` config drops it.

    A key in *data* that is not a field of *model* raises a
    ``DeprecationWarning`` naming the file basename, the key, the closest
    schema match (via the shared :func:`suggest_closest_field`), and a
    ``(tracked in #<n>)`` suffix pointing at the strict-flip follow-up
    issue so users can plan against a concrete retirement schedule. The
    key is then silently dropped. A genuine validation failure (type
    mismatch, missing required field) propagates unchanged.

    Scope: only top-level keys are checked. An unknown key nested inside a
    sub-model is dropped without a warning — restoring the recursive scan
    is deferred to the strict-flip follow-up (see the tracker suffix).
    """
    known = set(model.model_fields)
    for key in data:
        if key not in known:
            warnings.warn(
                _unknown_key_line(model, key, source, section),
                DeprecationWarning,
                stacklevel=2,
            )
    with source_context(source):
        return model(**data)


def _unknown_key_line(model: type[BaseModel], key: str, source: Path, section: str) -> str:
    where = source.name + (f" ({section})" if section else "")
    return (
        f"unknown key '{key}' in {where}"
        f"{suggest_closest_field(model, key)}"
        f"(tracked in #{POST_M9_STRICT_FLIP_ISSUE})"
    )


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


# ── Load ───────────────────────────────────────────────────────────────


def load_project_config(path: Path) -> ProjectConfig:
    """Read and validate a ``project.yaml`` file.

    Raises ``FileNotFoundError`` if the file does not exist, ``RuntimeError``
    if the YAML is not a mapping, and ``pydantic.ValidationError`` if the
    contents don't validate against :class:`ProjectConfig`. Every declared
    seed's ``digest`` is verified against the file's bytes after
    construction; a mismatch (or missing file) raises ``RuntimeError``
    naming the seed key, the declared digest, and the actual digest so
    a swap without re-stamping fails loud.

    Scope note: ``${VAR}`` interpolation is **not** applied to
    ``project.yaml`` values — only the run-config load path (see
    :func:`load_effective_run_config`) substitutes placeholders. A
    ``project.yaml`` entry like ``assets.seeds.foo.path:
    "${SEED_ROOT}/foo.sql"`` stays literal. This is a scope choice
    (project.yaml is checked into the repo; the operator's env is
    per-invocation), not a correctness one — extending interpolation
    to the project side is a follow-up if authors ask for it.
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
    _warn_null_stack(data, path)
    # Resolve default_environment.compose_file relative to the project dir
    # so EnvironmentManifest's validator can locate it regardless of CWD.
    _resolve_project_paths(data, path.parent)
    task_defaults = data.get("task_defaults")
    if isinstance(task_defaults, dict):
        with source_context(path):
            canonicalize_actor_config(task_defaults)
    project = construct_config(ProjectConfig, data, source=path)
    _verify_seed_digests(project, path)
    return project


def synthesize_default_project(
    *,
    project_root: Path,
    task_defaults: TaskDefaults | None = None,
) -> ProjectConfig:
    """Return a minimal ``ProjectConfig`` for a pack that ships no
    ``project.yaml``.

    Loaders route through here whenever :func:`find_project_yaml` returns
    ``None`` so downstream code always has a ``ProjectConfig`` to consume.
    Emits a ``DeprecationWarning`` naming the searched root and
    recommending the author add a ``project.yaml``; the pack still loads.
    """
    # Route through warn_deprecated for uniform message shape (in <file> +
    # (tracked in #NNN)); pack-root basename is what the user recognises,
    # and it keeps this consistent with the basename-only policy the rest of
    # M9's warnings follow.
    with source_context(project_root):
        warn_deprecated(
            legacy="Missing project.yaml",
            canonical="a project.yaml at the pack root",
            detail=(
                f"Add a `project.yaml` at the pack root (`name: {project_root.name}` "
                "alone suffices) to remove this warning. A future release will require "
                "project.yaml at load time."
            ),
            stacklevel=2,
        )
    return ProjectConfig(
        name=project_root.name or "synthesised",
        description=None,
        task_defaults=task_defaults or TaskDefaults(),
    )


def _verify_seed_digests(project: ProjectConfig, project_yaml_path: Path) -> None:
    """Read each ``assets.seeds.<name>.path`` and compare its digest to
    the declared ``digest``. A seed path is either a file (byte-stream
    hash) or a directory (deterministic tree hash) — both go through
    :func:`compute_seed_digest`. Raises ``RuntimeError`` on mismatch or
    missing path, naming the seed key, declared digest, and actual
    digest so the author sees the exact fix.
    """
    if project.assets is None:
        return
    for name, seed in project.assets.seeds.items():
        seed_path = Path(seed.path)
        if not seed_path.exists():
            raise RuntimeError(
                f"assets.seeds.{name}.path {seed_path!s} does not exist "
                f"(declared in {project_yaml_path})."
            )
        actual_digest = compute_seed_digest(seed_path)
        if seed.digest != actual_digest:
            raise RuntimeError(
                f"assets.seeds.{name}: digest mismatch for {seed_path!s}. "
                f"Declared: {seed.digest}. Actual: {actual_digest}. "
                "Re-stamp via `tolokaforge assets stamp` or restore the "
                "original file."
            )


def _warn_null_stack(data: dict, path: Path) -> None:
    """Warn (and drop) when ``default_environment.stack`` (or its
    ``compose_file``) is present but explicitly null. A project omits the
    key to declare no environment default; explicit-null is deprecated and
    silently drops the field. Pre-Pydantic so the warning carries the
    ``project.yaml`` basename via :func:`source_context`. Strict rejection
    deferred to a future release (tracked in #533).
    """
    env = data.get("default_environment")
    if not isinstance(env, dict):
        return
    if "stack" in env and env["stack"] is None:
        with source_context(path):
            warn_deprecated(
                legacy="'default_environment.stack: null'",
                canonical="omit the key or declare a stack sub-object",
                detail=(
                    "A project.yaml cannot unset the environment via null — omit the "
                    "key entirely to declare no environment default, or declare a "
                    "stack sub-object explicitly."
                ),
                stacklevel=2,
            )
        env.pop("stack")
    stack = env.get("stack")
    if isinstance(stack, dict) and "compose_file" in stack and stack["compose_file"] is None:
        with source_context(path):
            warn_deprecated(
                legacy="'default_environment.stack.compose_file: null'",
                canonical="omit the key or declare a compose_file path",
                detail=(
                    "There is no engine-default compose file to fall through to. Omit "
                    "the key entirely to declare no substrate pointer, or declare one."
                ),
                stacklevel=2,
            )
        stack.pop("compose_file")


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
    # ``exclude_unset`` drops fields the author never touched, keeping the
    # ones they explicitly wrote — including fields whose value equals the
    # schema default. ``exclude_defaults`` would drop those too, and that
    # silently strips the ``type`` discriminator from
    # ``storage.artifacts`` / ``storage.logs`` (``type: "local"`` matches
    # the ``LocalStorageConfig.type`` default), which then makes
    # ``RunConfig(**merged)`` fail to reconstruct the discriminated union
    # with ``union_tag_not_found``. Any run_config's own fields still win
    # on conflict via ``deep_merge`` below.
    base = project.run_defaults.model_dump(exclude_unset=True)
    return deep_merge(base, run_config_data)


def project_grading_combine(task_defaults: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The ``grading_defaults.combine`` layer inside a project's ``task_defaults``.

    The base layer under each task's own ``grading.yaml.combine``. ``None`` when the
    project sets no grading defaults, which is also the answer for no project at all —
    both mean there is no layer beneath the task's own block.
    """
    if not task_defaults:
        return None
    return (task_defaults.get("grading_defaults") or {}).get("combine")


def resolve_effective_grading_combine(
    project_combine: dict[str, Any] | None,
    task_combine: dict[str, Any] | None,
) -> GradingCombineConfig:
    """Resolve a task's effective ``combine`` from the project defaults
    layered under the task's own ``grading.yaml.combine``.

    ``task_combine`` wins over ``project_combine`` field-by-field;
    ``weights`` merges key-by-key (task key wins, project-only keys
    survive) per the documented config algebra. Any field neither layer
    sets falls through to :class:`GradingCombineConfig`'s own defaults
    (``method="weighted"``, ``weights={}``, ``pass_threshold=0.8``).

    Both inputs are raw dicts — the project defaults arrive as a
    ``model_dump`` view and the task combine as parsed ``grading.yaml`` —
    so neither side needs a model instance.
    """
    merged = deep_merge(project_combine or {}, task_combine or {})
    return GradingCombineConfig(**merged)


def resolve_effective_judge_customization(
    project_customization: dict[str, Any] | None,
    task_customization: dict[str, Any] | None,
) -> JudgeCustomization:
    """Resolve a task's effective judge ``customization`` from the project
    default layered under the task's own ``llm_judge.customization``.

    ``task_customization`` wins field-by-field; a field neither layer sets falls
    through to :class:`JudgeCustomization`'s tri-state default (``None`` =
    faithful). Both inputs are raw parsed dicts, so an unset task key is simply
    absent and never overrides a set project key — the tri-state is preserved
    (mirrors :func:`resolve_effective_grading_combine`; do not merge
    ``model_dump`` views, which would materialise ``None`` and clobber it).
    """
    merged = deep_merge(project_customization or {}, task_customization or {})
    return JudgeCustomization(**merged)


# ── High-level loader entry point ──────────────────────────────────────


# ── ${VAR} interpolation ───────────────────────────────────────────────


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""Match ``${VAR}`` occurrences. Braces are mandatory — bareword
``$VAR`` is not recognised (ambiguity with literal ``$`` is a
tar-pit; explicit braces avoid it). Variable names follow the shell
convention: letter or underscore, then letters / digits / underscores.
"""

_CREDENTIAL_NAME_SUFFIX_PATTERN = re.compile(
    r"(?:_API_KEY|_API_KEYS|_API_BASE|_TOKEN|_SECRET|_PASSWORD" r"|_DSN|_CREDENTIAL[S]?|_PAT)$"
)
"""Variable-name suffixes that mark a placeholder as
credential-shaped. Mirrors the enforcement pattern in
``tests/unit/secrets/test_no_raw_secret_access.py`` so authors
cannot smuggle credentials into a config through ``${VAR}`` —
credentials must flow through ``SecretManager``, never a run-config
placeholder that lands as plaintext in the merged dict."""


def _interpolate_env_vars(
    node: Any,
    *,
    source_path: Path,
    _key_path: tuple[str, ...] = (),
) -> Any:
    """Substitute ``${VAR}`` occurrences in every string value under
    *node* with values from ``os.environ``. Recursive; walks dicts and
    lists; leaves non-string leaves untouched.

    Returns a new tree with substitutions applied — the input is not
    mutated. Missing variables and credential-shaped placeholders
    collect into a single error naming the file, every offending key
    path, and every offending variable so the author fixes them in
    one pass rather than one-at-a-time.

    Scope: only string *values* are interpolated. Dict keys stay
    literal. Numbers, booleans, ``None``, and lists-of-non-strings pass
    through unchanged. Recursion into substituted values does not run —
    if ``${FOO}`` resolves to a string containing ``${BAR}``, only one
    pass happens.

    Rule-2 boundary: variable names ending in credential-shaped
    suffixes (``_API_KEY``, ``_TOKEN``, ``_SECRET``, ``_PASSWORD``,
    ``_DSN``, ``_CREDENTIAL[S]``, ``_PAT``) are rejected at load
    time even if the env var is set. Credentials must go through
    ``SecretManager``, never a plaintext ``${VAR}`` placeholder — the
    merged run-config dict is logged, dumped, and passed through
    many hands, so a secret lifted into it via interpolation escapes
    the single-abstraction invariant.
    """
    missing: list[str] = []
    credentials: list[str] = []
    result = _interpolate_walk(node, source_path, _key_path, missing, credentials)
    errors: list[str] = []
    if credentials:
        errors.append(
            f"credential-shaped variable(s) in ${{...}} placeholders: "
            f"{sorted(set(credentials))}. Credentials must go through "
            "SecretManager, not a run-config placeholder — the merged "
            "dict is plaintext and gets logged / dumped downstream."
        )
    if missing:
        errors.append(
            f"unresolved environment variable(s): {sorted(set(missing))}. "
            "Export them before loading (or supply defaults in the run config)."
        )
    if errors:
        joined = " | ".join(errors)
        raise ValueError(f"Run config {source_path}: {joined}")
    return result


def _interpolate_walk(
    node: Any,
    source_path: Path,
    key_path: tuple[str, ...],
    missing: list[str],
    credentials: list[str],
) -> Any:
    """Depth-first walk used by :func:`_interpolate_env_vars`.
    Splits into a top-level helper so the public entry point owns the
    "raise once with every miss" contract in a single place.
    """
    if isinstance(node, dict):
        return {
            k: _interpolate_walk(v, source_path, (*key_path, str(k)), missing, credentials)
            for k, v in node.items()
        }
    if isinstance(node, list):
        # List-index segments are appended without a leading dot so the
        # rendered path reads ``evaluation.projects[0]`` rather than the
        # spurious ``evaluation.projects.[0]``. The joiner in
        # :func:`_render_key_path` treats ``[N]`` segments specially.
        return [
            _interpolate_walk(item, source_path, (*key_path, f"[{idx}]"), missing, credentials)
            for idx, item in enumerate(node)
        ]
    if isinstance(node, str):
        return _interpolate_string(node, source_path, key_path, missing, credentials)
    return node


def _render_key_path(key_path: tuple[str, ...]) -> str:
    """Render a walker key path for error messages.

    Dict-key segments join with ``.``; list-index segments (``[N]``)
    stay attached to the preceding segment without an intervening dot.
    Returns ``"(root)"`` when the path is empty.
    """
    if not key_path:
        return "(root)"
    parts: list[str] = []
    for segment in key_path:
        if segment.startswith("[") and parts:
            parts[-1] = parts[-1] + segment
        else:
            parts.append(segment)
    return ".".join(parts)


def _interpolate_string(
    value: str,
    source_path: Path,
    key_path: tuple[str, ...],
    missing: list[str],
    credentials: list[str],
) -> str:
    """Substitute every ``${VAR}`` occurrence in *value*. Credential-
    shaped names are rejected before the environment is even queried;
    unknown names record onto *missing* for the collated error. The
    return value only matters when both lists stay empty — callers
    always route the raise through the public entry point."""

    def replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        rendered_path = _render_key_path(key_path)
        if _CREDENTIAL_NAME_SUFFIX_PATTERN.search(var_name):
            credentials.append(f"{rendered_path} → ${{{var_name}}}")
            return match.group(0)
        env_value = os.environ.get(var_name)
        if env_value is None:
            missing.append(f"{rendered_path} → ${{{var_name}}}")
            return match.group(0)
        return env_value

    return _ENV_VAR_PATTERN.sub(replace, value)


def load_effective_run_config(
    config_path: Path,
) -> tuple[dict[str, Any], ProjectConfig]:
    """Load the run-config YAML at *config_path* and layer
    ``project.run_defaults`` under it.

    Returns ``(config_data, project)``:

    - ``config_data`` is the merged dict, ready to feed into
      ``RunConfig(**config_data)``.
    - ``project`` is the enclosing ``ProjectConfig`` loaded from the
      discovered ``project.yaml``, or a synthesised default (with a
      ``DeprecationWarning``) for a pack that ships none.

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
    merged = _interpolate_env_vars(merged, source_path=config_path)
    validate_actor_roster_subset_of_models(project, merged)
    return merged, project


def validate_actor_roster_subset_of_models(
    project: ProjectConfig,
    merged_run_config: dict[str, Any],
) -> None:
    """Every ``actors.<name>`` declaration with ``mode == "llm"`` must
    have a matching entry in the resolved ``models`` dict.

    "Matching entry" means the actor's map-key must appear as a key in
    ``models`` — e.g. an actor at ``actors.user`` with ``mode: llm``
    requires ``models.user`` to be declared. ``ActorSpec`` has no
    ``.model`` field today; the by-key match is the only decidable
    semantic. If a future milestone adds an explicit
    ``actors.<name>.model`` reference the check should follow that.

    Raises ``ValueError`` naming the missing model(s). Runs after
    ``project.run_defaults`` merges into the selected run config —
    that's the only point where a project-side actor roster and the
    run-side model roster are both visible.

    Scope: this check covers ``project.task_defaults.actors`` — the
    roster every task inherits, declared opt-in by the project author.
    A task-level ``TaskConfig.actors`` override is not re-checked here:
    individual ``task.yaml`` files are not loaded at run-config-resolve
    time, and the loader lifts every task's ``user_simulator`` into
    ``actors.user`` (``mode=llm`` by default), so a per-task roster gate
    would fire for tasks that simply rely on the run's ``models.user``.
    The user model each task's simulator uses is resolved from the run's
    ``models`` at trial build.

    A ``None`` roster (project sets no ``actors``) is a no-op. Actor
    entries without ``mode == "llm"`` are ignored — scripted actors
    don't need a model.
    """
    actors = project.task_defaults.actors
    if not actors:
        return
    models = merged_run_config.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError(
            f"`models` in the resolved run config must be a mapping; got "
            f"{type(models).__name__}. Fix the `models:` block before the "
            "actor roster can be checked."
        )
    missing = sorted(
        name for name, spec in actors.items() if spec.mode == "llm" and name not in models
    )
    if missing:
        raise ValueError(
            f"Actor roster references models {missing!r} that are not declared "
            f"under `models`; declared: {sorted(models)!r}."
        )


# ── Environment resolve ────────────────────────────────────────────────


_POLICY_REQUEST_FIELDS = (
    "network_policy",
    "limited_internet_allowlist",
    "security_context_defaults",
)
"""Fields that survive atomic ``stack`` replacement — policy requests
that are substrate-neutral (they describe the trial regardless of
substrate). List-valued members (``limited_internet_allowlist``) replace
outright on merge — the task list wins over the project list, never
unions with it."""

_SERVICE_TREATMENT_FIELDS = ("initial_state", "services")
"""Fields scoped to the reviewed stack — discarded on atomic
``stack`` replacement (the project's opt-outs reviewed the project's
services, not the replacement stack)."""

_ENDPOINT_OVERRIDE_FIELDS = ("runner_port", "db_service", "db_port", "rag_service", "rag_port")
"""Endpoint-resolution overrides carried under ``stack``. Substrate-scoped
— cleared with the rest of ``stack`` on atomic replacement — so a task that
swaps ``stack.compose_file`` drops any project-side endpoint override."""


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
    - Service-treatment fields (``initial_state``, ``services``) are
      discarded — the project's per-service opt-outs reviewed the
      project's services, not the replacement stack.
    - Policy-request fields (``network_policy``,
      ``limited_internet_allowlist``, ``security_context_defaults``)
      survive — substrate-neutral, they describe the trial regardless of
      substrate.

    After manifest construction, every compose service missing from the
    merged ``services`` map is filled with an ``ephemeral`` default so
    downstream consumers see the complete map.

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
    for field in _ENDPOINT_OVERRIDE_FIELDS:
        value = stack.get(field)
        if value is not None:
            manifest_kwargs[field] = value
    for field in (*_SERVICE_TREATMENT_FIELDS, *_POLICY_REQUEST_FIELDS):
        value = merged.get(field)
        if value is None:
            continue
        manifest_kwargs[field] = value

    manifest = EnvironmentManifest(**manifest_kwargs)
    _fill_missing_service_defaults(manifest)
    return manifest


def _fill_missing_service_defaults(manifest: EnvironmentManifest) -> None:
    """Insert an ``ephemeral`` :class:`ServiceSpec` for every compose
    service that lacks a manifest entry after merge.

    In-place on the manifest's ``services`` mapping. Consumers can then
    walk every compose service and read a canonical isolation label
    without a missing-key fallback.
    """
    declared = set(manifest.services)
    compose_services = manifest.load_compose().get("services") or {}
    for name in compose_services:
        if name in declared:
            continue
        manifest.services[name] = ServiceSpec(isolation="ephemeral")


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
