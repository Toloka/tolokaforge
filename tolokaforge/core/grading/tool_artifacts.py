"""Tool-artifact extraction shared between runner and grader dispatch.

The pack ships every artefact module the trial imports — ``checks.py`` for
custom checks, MCP-server scripts, sibling helpers — as base64-encoded
entries on :attr:`TaskDescription.tool_artifacts` (``{rel_path: b64_content}``).
Both dispatch topologies materialise the same bundle: the runner side
during ``RegisterTrial`` (so tool reconstruction can import the modules
during agent execution), the grader side at grade time (so
``grade_custom_checks`` and any sibling artefact modules resolve).

:func:`extract_tool_artifacts` writes each entry to a fresh temp directory
and prepends both ``extract_dir`` and ``extract_dir / "tools"`` (when the
subdir exists) to ``sys.path`` so ``checks.py``'s ``from mcp_core import
X`` and every adapter's own tool package resolves. Cleanup — path removal,
:func:`purge_modules_under` and ``rmtree`` — is the caller's responsibility;
the runner tracks the directory on ``self._artifact_dirs[trial_id]`` for
teardown, the grader scopes it to a local ``try/finally`` on the grade call.

A description may also deliver artefacts by reference (ADR-0068, ADR-0072): each
entry of :attr:`TaskDescription.tool_artifact_refs` names a file of the task's
pack by its ``pack:`` address, resolved here under the task directory the engine
mounted read-only into the runner, with the SHA-256 and size the adapter
computed. :func:`extract_tool_artifacts` copies each such file into the
same directory as the inline entries, hashing while it copies, and refuses the
whole set when any file is missing, leaves the mount or does not match. Only a
process that was handed the mount (:data:`TOOL_ARTIFACT_REFS_DIR_ENV_VAR`) can
resolve references; everywhere else they are refused, never skipped.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_ARTIFACT_REFS_DIR_ENV_VAR",
    "TOOL_ARTIFACT_REFS_MOUNT_PATH",
    "ToolArtifactRefError",
    "extract_tool_artifacts",
    "file_sha256",
    "purge_modules_under",
    "tool_artifact_refs_dir_from_env",
]

TOOL_ARTIFACT_REFS_MOUNT_PATH = "/tolokaforge/tool-artifact-refs"
"""Where the engine mounts a task's reference root inside the runner container."""

TOOL_ARTIFACT_REFS_DIR_ENV_VAR = "TOLOKAFORGE_TOOL_ARTIFACT_REFS_DIR"
"""Set by the engine on a runner it gave a reference root, to that root's path
in the runner. Unset, the process holds no root and refuses references."""

_COPY_CHUNK_BYTES = 1024 * 1024
_REFUSALS_SHOWN = 5


class ToolArtifactRefError(ValueError):
    """Artefacts delivered by reference could not be resolved, or did not match."""


class _ToolArtifactRef(Protocol):
    """What resolution reads off a :class:`~tolokaforge.runner.models.ArtifactRef`."""

    @property
    def pack_path(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def size(self) -> int: ...


def tool_artifact_refs_dir_from_env() -> Path | None:
    """The reference root this process was given, or ``None`` when it was given none."""
    raw = os.environ.get(TOOL_ARTIFACT_REFS_DIR_ENV_VAR, "").strip()
    return Path(raw) if raw else None


def file_sha256(path: Path) -> tuple[str, int]:
    """``(lowercase hex SHA-256, size in bytes)`` of the file at ``path``, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def extract_tool_artifacts(
    trial_id: str,
    artifacts: dict[str, str],
    refs: Mapping[str, _ToolArtifactRef] | None = None,
    refs_dir: Path | None = None,
) -> Path:
    """Materialise ``artifacts`` to a temp directory and register it on ``sys.path``.

    Each ``artifacts`` entry maps a relative path to base64-encoded file
    content; ``extract_tool_artifacts`` decodes, writes each to a sibling of
    the returned dir, and prepends ``extract_dir`` (for packages living at
    the artefact root, e.g. ``mcp_core/``) plus ``extract_dir / "tools"``
    (for adapter layouts placing their packages under a ``tools/`` subdir)
    to ``sys.path`` when non-empty. Callers must remove the entries from
    ``sys.path`` and ``shutil.rmtree`` the directory when the trial's
    artefacts are no longer needed.

    ``refs`` (ADR-0068) are copied from ``refs_dir`` into the same directory,
    each verified against its SHA-256 and size. A non-empty ``refs`` without a
    ``refs_dir`` raises, as does any reference that is missing, leaves
    ``refs_dir`` or does not match; the directory is removed before the
    :class:`ToolArtifactRefError` propagates and nothing is put on ``sys.path``.
    """
    safe_trial_id = trial_id.replace(":", "_").replace("/", "_")
    if refs and refs_dir is None:
        raise ToolArtifactRefError(
            f"{len(refs)} tool artefact(s) of {trial_id} are delivered by reference, but this "
            f"process was given no reference root ({TOOL_ARTIFACT_REFS_DIR_ENV_VAR} is unset). "
            "References are resolved only by the runner of a trial-scope stack the engine "
            "materialised with the task's directory mounted (compute.trial_data_delivery: "
            "mount); a grader_rpc or queue grader, or a snapshot grade bundle, cannot read them."
        )
    extract_dir = Path(tempfile.mkdtemp(prefix=f"tolokaforge-artifacts-{safe_trial_id}-"))

    for rel_path, b64_content in artifacts.items():
        out_path = extract_dir / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(b64_content)
        out_path.write_bytes(content)

    if refs and refs_dir is not None:
        try:
            _copy_verified_refs(trial_id, refs, refs_dir, extract_dir)
        except BaseException:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise

    extract_str = str(extract_dir)
    if extract_str not in sys.path:
        sys.path.insert(0, extract_str)

    tools_dir = extract_dir / "tools"
    if tools_dir.exists():
        tools_str = str(tools_dir)
        if tools_str not in sys.path:
            sys.path.insert(0, tools_str)

    logger.info(
        "Extracted %d inline and %d referenced artifacts to %s, added to sys.path",
        len(artifacts),
        len(refs or {}),
        extract_dir,
    )

    return extract_dir


def _copy_verified_refs(
    trial_id: str,
    refs: Mapping[str, _ToolArtifactRef],
    refs_dir: Path,
    extract_dir: Path,
) -> None:
    """Copy every reference from ``refs_dir`` into ``extract_dir``, or raise naming each refusal.

    Each file is hashed while it is copied, so its bytes are read once and never
    held whole in memory. Every reference is tried before the raise, so an
    author repairing a pack sees the whole list, not the first entry.
    """
    root = refs_dir.resolve()
    if not root.is_dir():
        raise ToolArtifactRefError(
            f"the tool-artefact reference root {refs_dir} of {trial_id} is not a directory "
            "in this process; the engine mounts it into the trial's runner service"
        )
    refusals: list[str] = []
    for target, ref in refs.items():
        refusal = _copy_one_verified_ref(root, ref, extract_dir / target)
        if refusal is not None:
            refusals.append(f"{target}: {refusal}")
    if not refusals:
        return
    shown = "; ".join(refusals[:_REFUSALS_SHOWN])
    more = (
        f"; and {len(refusals) - _REFUSALS_SHOWN} more" if len(refusals) > _REFUSALS_SHOWN else ""
    )
    raise ToolArtifactRefError(
        f"{len(refusals)} of {len(refs)} tool artefact reference(s) of {trial_id} do not "
        f"resolve under {refs_dir} to the bytes the task was described with: {shown}{more}. "
        "The task's files changed after it was described, or the mounted directory is "
        "not the task's."
    )


def _copy_one_verified_ref(root: Path, ref: _ToolArtifactRef, out_path: Path) -> str | None:
    """Copy one reference to ``out_path``; why it was refused, or ``None`` when it matched."""
    source = (root / ref.pack_path).resolve()
    if not source.is_relative_to(root):
        return f"pack:{ref.pack_path} resolves outside the reference root"
    if not source.is_file():
        return f"pack:{ref.pack_path} is not a file under the reference root"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as src, out_path.open("wb") as dst:
        while chunk := src.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            dst.write(chunk)
            size += len(chunk)
    actual = digest.hexdigest()
    if actual == ref.sha256 and size == ref.size:
        return None
    out_path.unlink()
    return (
        f"pack:{ref.pack_path} has sha256 {actual} and {size} bytes, "
        f"expected sha256 {ref.sha256} and {ref.size} bytes"
    )


def purge_modules_under(root: Path) -> list[str]:
    """Drop from ``sys.modules`` every module loaded from under ``root``; their names, sorted.

    A module the trial imported in process — a tool module, a helper of
    ``checks.py``, a pinned tool library (ADR-0060) — stays cached after its
    directory is removed, and a later trial that ships another version of the
    same module under a fresh directory would be served the stale one. Called
    wherever an extracted directory is torn down, before the ``rmtree``.

    Only a module whose ``__file__`` or ``__path__`` lies under ``root`` — as
    given, or resolved through symlinks — qualifies; nothing loaded from
    elsewhere is touched.
    """
    prefixes = tuple({f"{os.fspath(root).rstrip(os.sep)}{os.sep}", f"{root.resolve()}{os.sep}"})
    purged: list[str] = []
    for name, module in list(sys.modules.items()):
        locations: list[str] = []
        file = getattr(module, "__file__", None)
        if isinstance(file, str):
            locations.append(file)
        path = getattr(module, "__path__", None)
        if path is not None:
            try:
                locations.extend(os.fspath(entry) for entry in path)
            except TypeError:
                pass
        if any(location.startswith(prefixes) for location in locations):
            del sys.modules[name]
            purged.append(name)
    if purged:
        logger.debug("Purged %d module(s) loaded from %s: %s", len(purged), root, purged)
    return sorted(purged)
