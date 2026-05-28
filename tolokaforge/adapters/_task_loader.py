"""Task-yaml loading with optional shared-domain merge.

This module is the single entry point for turning a ``task.yaml`` path into a
validated :class:`TaskConfig`. It exists so that every code path that loads a
task — :class:`NativeAdapter`, :class:`FrozenMcpCoreAdapter`, the
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

from tolokaforge.core.models import TaskConfig


def load_task_yaml(task_path: Path) -> tuple[TaskConfig, Path]:
    """Read ``task.yaml``, apply any ``domain:`` merge, return the validated config.

    All relative-path fields on the returned :class:`TaskConfig` are expressed
    relative to ``effective_task_dir``. For the flat layout that is just the
    task.yaml parent (so no rewriting is needed); for the shared-domain
    layout it is the domain root, so paths from both the domain dict and the
    case dict are normalised to that frame.

    Args:
        task_path: Absolute or relative path to ``task.yaml``.

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
    # so this step leaves it untouched for ``_apply_domain`` to consume.
    if task_root != task_path.parent:
        _rewrite_task_paths(task_data, task_path.parent, task_root)

    task_data = _apply_domain(task_path, task_data, task_root)

    return TaskConfig(**task_data), task_root


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


def _apply_domain(task_path: Path, task_data: dict, task_root: Path) -> dict:
    """Deep-merge the ``domain:`` ref into *task_data* if one is present.

    Domain-side path fields are rewritten from the domain file's parent dir
    into the *task_root* frame before merge so downstream callers see one
    consistent set of paths. The ``domain`` key is stripped from the returned
    dict.
    """
    domain_ref = task_data.get("domain")
    if not domain_ref or not isinstance(domain_ref, str):
        return task_data

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
    merged = _deep_merge_task(domain_data, task_data)
    merged.pop("domain", None)
    return merged


def _deep_merge_task(domain: dict, task: dict) -> dict:
    """Deep-merge *domain* into *task*; task values win on conflict.

    Nested dicts merge recursively. Lists and scalars from the task side
    replace the domain side — split a structure across both files only if its
    inner values are identical for every case; otherwise keep it whole on one
    side.
    """
    result: dict = dict(domain)
    for key, val in task.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(val, dict):
            result[key] = _deep_merge_task(existing, val)
        else:
            result[key] = val
    return result


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
