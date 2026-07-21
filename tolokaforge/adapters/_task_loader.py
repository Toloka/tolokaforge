"""Task-yaml loading with optional shared-domain merge.

This module is the single entry point for turning a ``task.yaml`` path into a
validated :class:`TaskConfig`. It exists so that every code path that loads a
task — :class:`NativeAdapter`, the ``tlk_mcp_core`` adapter, the
``tolokaforge validate`` CLI, and ``tolokaforge adapter validate`` — applies
the same domain-layout merge.

The shared-domain layout lets a directory of related task cases reuse one
``_shared/domain.yaml`` file::

    <domain>/
        _shared/
            domain.yaml          # category, tools, system_prompt, mcp_server
            mcp_server.py
            system_prompt.md
        testcases/
            case_a/
                task.yaml        # domain: ../../_shared/domain.yaml
                grading.yaml
                initial_state.json
            case_b/
                task.yaml
                ...

When a ``task.yaml`` carries a ``domain:`` ref, :func:`load_task_yaml`:

1. Reads the referenced YAML.
2. Rewrites every relative path field in the domain dict so it is expressed
   relative to the *task.yaml* parent dir (not the *domain.yaml* parent dir).
3. Deep-merges the rewritten domain into the task; per-case keys win on
   conflict.
4. Strips the ``domain:`` key.
5. Validates the merged dict via :class:`TaskConfig`.
6. Returns ``(TaskConfig, effective_task_dir)``. The effective task dir is the
   *domain root* (``<domain>``) for shared-domain tasks so that downstream
   consumers — notably :meth:`NativeAdapter._bundle_task_artifacts` — pick up
   the ``_shared/`` siblings, not just the case dir.

A flat ``<task>/task.yaml`` without a ``domain:`` ref returns
``(TaskConfig, task_path.parent)`` — the legacy behaviour.

This module composes existing types only — :class:`Path`, :mod:`yaml`,
:class:`TaskConfig`. It does **not** introduce a new abstraction.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.deprecations import (
    canonicalize_actor_config,
    source_context,
    warn_deprecated,
)
from tolokaforge.core.models import TaskConfig, TaskDefaults
from tolokaforge.core.project_loader import construct_config, deep_merge

# Keys that live on ``project.task_defaults`` but are not ``TaskConfig`` fields.
# They reach the engine through their own seams (``grading_defaults`` via
# ``NativeAdapter.get_grading_config``, ``continue_prompt`` via turn logic), so
# merging them into a task dict would fail its ``extra="forbid"`` validation.
_PROJECT_SCOPED_DEFAULT_KEYS = frozenset(TaskDefaults.model_fields) - frozenset(
    TaskConfig.model_fields
)


def validate_grading_yaml(grading_path: Path) -> None:
    """Validate a task's ``grading.yaml``, failing loud on schema breaks.

    Run by ``tolokaforge validate`` so a malformed grading block — most notably
    the pre-Stage-2 free-text ``llm_judge.rubric: str`` / removed
    ``output_schema`` shape — is rejected at validate time with a clear
    migration message (raised by :class:`LLMJudgeConfig`), not only at run time.

    A missing grading file is not an error here: ``load_task_yaml`` already
    validates the ``grading`` path field, and some adapters synthesise grading
    config without an on-disk file.

    Raises:
        ValueError / pydantic.ValidationError: If the grading block is invalid.
    """
    if not grading_path.exists():
        return

    with open(grading_path) as f:
        grading_data = yaml.safe_load(f) or {}

    if not isinstance(grading_data, dict):
        raise RuntimeError(
            f"Grading file {grading_path} is not a YAML mapping (got {type(grading_data).__name__})"
        )

    # The rubric migration lives on the canonical LLMJudgeConfig, so validate the
    # llm_judge block directly — independent of the surrounding grading schema,
    # which varies by adapter.
    # Validate when a rubric is present (the new gate) or when the relocated
    # ``model_ref`` lingers (so its loud migration error surfaces at validate
    # time rather than only at run time).
    llm_judge = grading_data.get("llm_judge")
    if isinstance(llm_judge, dict) and (llm_judge.get("rubric") or "model_ref" in llm_judge):
        from tolokaforge.runner.models import LLMJudgeConfig

        LLMJudgeConfig(**llm_judge)


def load_task_yaml(
    task_path: Path,
    *,
    project_task_defaults: dict[str, Any] | None = None,
) -> tuple[TaskConfig, Path]:
    """Read ``task.yaml``, apply any ``domain:`` merge, return the validated config.

    All relative-path fields on the returned :class:`TaskConfig` are expressed
    relative to ``effective_task_dir``. For the flat layout that is just the
    task.yaml parent (so no rewriting is needed); for the shared-domain
    layout it is the domain root, so paths from both the domain dict and the
    case dict are normalised to that frame.

    Args:
        task_path: Absolute or relative path to ``task.yaml``.
        project_task_defaults: Optional ``project.task_defaults`` dict from
            the enclosing project. Only its ``TaskConfig``-shaped keys are
            layered into the task dict — above the adapter's Domain merge and
            below the task's own fields. Project-scoped-only keys
            (``grading_defaults``, ``continue_prompt``) are excluded here and
            reach the engine through their own seams. Precedence, low to high:
            adapter Domain bundle → project defaults → task.yaml. Task fields
            win on conflict; project defaults win over the Domain bundle where
            they overlap and the task doesn't set the field.

    Returns:
        ``(task_config, effective_task_dir)``. ``effective_task_dir`` is the
        domain root for shared-domain tasks, or ``task_path.parent`` for the
        flat layout.

    Raises:
        FileNotFoundError: If ``task_path`` does not exist.
        RuntimeError: If the ``domain:`` ref cannot be resolved or read, or if
            the YAML at either path is not a top-level mapping.
        pydantic.ValidationError: If the merged dict fails :class:`TaskConfig`
            validation (re-raised by Pydantic).
    """
    task_path = Path(task_path)
    task_root = _detect_task_root(task_path)

    with open(task_path) as f:
        task_data = yaml.safe_load(f)

    if not isinstance(task_data, dict):
        raise RuntimeError(
            f"Task file {task_path} is not a YAML mapping (got {type(task_data).__name__})"
        )

    # Rewrite case-side paths into the task_root frame *before* the domain
    # merge so we never double-rewrite domain-supplied paths. No-op for the
    # flat layout (task_root == task.yaml parent) so legacy callers see no
    # change. The ``domain`` ref itself is not in ``_PATH_FIELD_REWRITERS``
    # so this step leaves it untouched for ``_load_domain_dict`` to consume.
    if task_root != task_path.parent:
        _rewrite_task_paths(task_data, task_path.parent, task_root)

    # Load the adapter's Domain bundle (if any) but don't merge yet — the
    # merge order matters: `domain` sits below `project_task_defaults`,
    # which sits below `task.yaml`. Later layers win on conflict.
    domain_data = _load_domain_dict(task_path, task_data, task_root)
    task_data.pop("domain", None)

    # Lift each layer's legacy ``user_simulator`` into ``actors.user`` before
    # the merge, so a legacy layer and a canonical layer both speak
    # ``actors.user`` and ``deep_merge`` composes them field-by-field. Run
    # per-layer: a post-merge coercer sees both keys deposited by different
    # layers and could not tell a cross-layer override from a same-source
    # conflict. The task-side ``source_context`` names task.yaml so any
    # emitted ``DeprecationWarning`` carries the right basename; domain-
    # side coercion runs inside ``_load_domain_dict`` where the domain
    # file path is in scope; project-side coercion already ran in
    # ``load_project_config`` (this second call is idempotent).
    with source_context(task_path):
        canonicalize_actor_config(task_data)
    # domain_data already canonicalised inside _load_domain_dict (with the
    # domain file path in source_context); project_task_defaults already
    # canonicalised inside load_project_config. Both calls here were
    # redundant duplicates that could re-fire warnings without source
    # context — removed.
    _ = project_task_defaults  # no-op: canonicalisation happens upstream

    # Build the precedence chain from lowest to highest. ``deep_merge``
    # is delta-wins, so the second argument overrides the first on conflict.
    base = domain_data
    if project_task_defaults:
        task_shaped_defaults = {
            key: value
            for key, value in project_task_defaults.items()
            if key not in _PROJECT_SCOPED_DEFAULT_KEYS
        }
        base = deep_merge(base, task_shaped_defaults)
    task_data = deep_merge(base, task_data)

    # Auto-pick a sibling ``grading.yaml`` when no layer set ``grading``. An
    # explicit ``grading:`` from any layer always wins. The absolute path is
    # layout-independent and survives ``task_dir / task.grading`` joins
    # downstream unchanged, so it needs no ``_PATH_FIELD_REWRITERS`` entry.
    if not task_data.get("grading"):
        sibling_grading = task_path.parent / "grading.yaml"
        if sibling_grading.exists():
            task_data["grading"] = str(sibling_grading.resolve())

    # Resolve environment_manifest.compose_file to an absolute path
    # against the task root so ``EnvironmentManifest``'s file-existence
    # validator can locate the file regardless of the CWD at load time.
    # No-op if the manifest is absent or the path is already absolute.
    _resolve_environment_manifest_paths(task_data, task_root, task_path)

    task = construct_config(TaskConfig, task_data, source=task_path)
    task._source_dir = task_root
    return task, task_root


def _resolve_environment_manifest_paths(task_data: dict, task_root: Path, task_path: Path) -> None:
    """Rewrite ``environment_manifest.stack.compose_file`` (canonical)
    or the legacy flat ``environment_manifest.compose_file`` to an
    absolute path when the value is a task-relative string. In-place
    edit on the ``task_data`` dict before Pydantic constructs
    :class:`TaskConfig`, so ``EnvironmentPatch``'s legacy-shape
    normaliser sees an anchored path.

    Raises :class:`RuntimeError` with the offending ``task_path`` when
    the field is present but shaped wrong — matches the loader's
    fail-loud pattern for corrupt YAML instead of deferring to a
    generic Pydantic error at ``TaskConfig`` construction, which loses
    the file / field context.
    """
    if "environment_manifest" not in task_data:
        return
    manifest = task_data["environment_manifest"]
    if not isinstance(manifest, dict):
        raise RuntimeError(
            f"Task file {task_path}: 'environment_manifest' must be a YAML mapping "
            f"(got {type(manifest).__name__})"
        )

    if "stack" in manifest and manifest["stack"] is None:
        with source_context(task_path):
            warn_deprecated(
                legacy="'environment_manifest.stack: null'",
                canonical="omit the key or declare a stack sub-object",
                detail=(
                    "A task cannot unset the environment out from under a project "
                    "that declares one. Omit the key entirely to inherit the project's "
                    "stack, or declare a stack sub-object explicitly."
                ),
                stacklevel=2,
            )
        # Drop the null key so the loader treats it as unset (inherit-from-project).
        manifest.pop("stack")

    stack = manifest.get("stack")
    if isinstance(stack, dict) and "compose_file" in stack:
        compose_file = stack["compose_file"]
        if compose_file is None:
            with source_context(task_path):
                warn_deprecated(
                    legacy="'environment_manifest.stack.compose_file: null'",
                    canonical="omit the key or declare a compose_file path",
                    detail=(
                        "A task cannot unset the substrate pointer; there is no "
                        "engine-default compose file to fall through to. Omit the "
                        "key entirely to inherit the project's compose_file."
                    ),
                    stacklevel=2,
                )
            # Drop the null key so the loader treats it as unset (inherit-from-project).
            stack.pop("compose_file")
        else:
            if not isinstance(compose_file, str):
                raise RuntimeError(
                    f"Task file {task_path}: "
                    f"'environment_manifest.stack.compose_file' must be a string "
                    f"(got {type(compose_file).__name__})"
                )
            resolved = Path(compose_file)
            if not resolved.is_absolute():
                resolved = (task_root / resolved).resolve()
            stack["compose_file"] = str(resolved)

    if "compose_file" in manifest:
        compose_file = manifest["compose_file"]
        if not isinstance(compose_file, str):
            raise RuntimeError(
                f"Task file {task_path}: 'environment_manifest.compose_file' must be a "
                f"string (got {type(compose_file).__name__})"
            )
        resolved = Path(compose_file)
        if not resolved.is_absolute():
            resolved = (task_root / resolved).resolve()
        manifest["compose_file"] = str(resolved)


def _detect_task_root(task_path: Path) -> Path:
    """Return the effective task directory for a discovered ``task.yaml``.

    For the shared-domain layout ``<domain>/testcases/<case>/task.yaml`` the
    task root is ``<domain>`` so shared files (e.g. ``_shared/mcp_server.py``)
    are bundled alongside case-specific files. For the legacy flat layout
    ``<task>/task.yaml`` the task root is the parent of ``task.yaml``.
    """
    parent = task_path.parent
    if parent.parent.name == "testcases":
        return parent.parent.parent
    return parent


def _load_domain_dict(task_path: Path, task_data: dict, task_root: Path) -> dict:
    """Return the ``domain:`` ref's contents with paths rewritten into the
    task-root frame. Returns ``{}`` when the task has no ``domain`` ref.

    The caller is responsible for merging the returned dict into the
    task's own dict. Splitting load from merge lets the caller layer
    additional sources (e.g. ``project.task_defaults``) between the
    domain bundle and the task's own fields, with the correct
    precedence.
    """
    domain_ref = task_data.get("domain")
    if not domain_ref or not isinstance(domain_ref, str):
        return {}

    domain_path = (task_path.parent / domain_ref).resolve()
    if not domain_path.exists():
        raise RuntimeError(
            f"Domain file referenced by {task_path} not found: {domain_path} "
            f"(ref: {domain_ref!r}) — fix the 'domain:' path in the task.yaml"
        )

    try:
        with open(domain_path) as f:
            domain_data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read domain file {domain_path} referenced by {task_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(domain_data, dict):
        raise RuntimeError(
            f"Domain file {domain_path} referenced by {task_path} is not a YAML "
            f"mapping (got {type(domain_data).__name__})"
        )

    _rewrite_task_paths(domain_data, domain_path.parent, task_root)
    # Lift legacy top-level ``user_simulator`` inside the domain layer to
    # ``actors.user`` here (rather than at the caller) so any
    # ``DeprecationWarning`` names this domain file, not the referring
    # task.yaml. Idempotent: a canonical layer is left untouched.
    with source_context(domain_path):
        canonicalize_actor_config(domain_data)
    return domain_data


def _rewrite_path(value: str, from_dir: Path, to_dir: Path) -> str:
    """Express *value* (interpreted relative to *from_dir*) as a path relative
    to *to_dir*. Raises :class:`ValueError` if it is not expressible (e.g.
    cross-drive on Windows)."""
    abs_path = (from_dir / value).resolve()
    try:
        return Path(os.path.relpath(abs_path, to_dir.resolve())).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Cannot express {abs_path} relative to {to_dir.resolve()} "
            f"(original value {value!r} from {from_dir}). "
            "Paths must live inside the domain root."
        ) from exc


# Declarative list of relative-path fields in a task.yaml mapping. Each entry
# is (selector, applier). The selector is a dotted path of dict keys; the
# applier knows how to walk and rewrite the leaf — most fields are simple
# strings, but ``initial_state.filesystem.copy`` is a list of dicts where
# only the ``from`` key is a path.
_FieldRewriter = Callable[[dict, Callable[[str], str]], None]


def _rewrite_string_field(*keys: str) -> _FieldRewriter:
    """Build a rewriter that walks ``data[k1][k2]…[kn]`` and rewrites it if
    the leaf is a non-empty string."""

    def rewriter(data: dict, rewrite: Callable[[str], str]) -> None:
        node: Any = data
        for key in keys[:-1]:
            if not isinstance(node, dict):
                return
            node = node.get(key)
            if node is None:
                return
        if not isinstance(node, dict):
            return
        leaf = keys[-1]
        val = node.get(leaf)
        if isinstance(val, str) and val:
            node[leaf] = rewrite(val)

    return rewriter


def _rewrite_filesystem_copy(data: dict, rewrite: Callable[[str], str]) -> None:
    """Walk ``initial_state.filesystem.copy[].from`` (list of dicts)."""
    initial = data.get("initial_state")
    if not isinstance(initial, dict):
        return
    fs = initial.get("filesystem")
    if not isinstance(fs, dict):
        return
    copy_spec = fs.get("copy")
    if not isinstance(copy_spec, list):
        return
    for entry in copy_spec:
        if not isinstance(entry, dict):
            continue
        src = entry.get("from")
        if isinstance(src, str) and src:
            entry["from"] = rewrite(src)


_PATH_FIELD_REWRITERS: tuple[_FieldRewriter, ...] = (
    _rewrite_string_field("grading"),
    _rewrite_string_field("system_prompt"),
    _rewrite_string_field("tools", "agent", "mcp_server"),
    _rewrite_string_field("tools", "user", "mcp_server"),
    _rewrite_string_field("initial_state", "json_db"),
    _rewrite_string_field("initial_state", "system_prompt"),
    _rewrite_filesystem_copy,
)


def _rewrite_task_paths(task_data: dict, from_dir: Path, to_dir: Path) -> None:
    """Rewrite every relative-path field in *task_data* from *from_dir*-relative
    to *to_dir*-relative.

    Mutates *task_data* in place. Path fields are enumerated by
    :data:`_PATH_FIELD_REWRITERS` rather than detected heuristically — adding a
    new path field to :class:`TaskConfig` requires a one-line addition there.

    ``initial_state.json_db`` is only rewritten when it is a string. The dict
    form is an inline state literal and has no path to resolve.
    """

    def rewrite(val: str) -> str:
        return _rewrite_path(val, from_dir, to_dir)

    for rewriter in _PATH_FIELD_REWRITERS:
        rewriter(task_data, rewrite)
