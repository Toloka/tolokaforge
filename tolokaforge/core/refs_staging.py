"""Stage exactly the files a trial references, for the runner's read-only mount (ADR-0072).

A description that delivers its data by reference names each file by a ``pack:``
address relative to the task's directory (:attr:`TaskDescription.tool_artifact_refs`;
``initial_state.tables_ref`` names one of those entries). The engine used to mount
the whole task directory into the trial's runner, so the runner, and any tool or
check running in it, could read every file of the task, referenced or not. The
engine now builds a per-trial directory holding only the referenced files, at
their ``pack:`` paths, and mounts that instead, at the same path in the container.

Each file is a hard link to the task's copy when the filesystem allows it, which
costs no bytes and no time whatever the world's size, and a copy otherwise (the
staging directory on another filesystem, or a link the kernel refuses), logged as a
warning with its bytes: the composer puts the staging directory on the task
directory's filesystem where it can, so a copy into a memory-backed temporary
filesystem stays the exception. On overlayfs a link of a file in a lower layer
copies it up first: counted as linked, it still costs the copy. A copy keeps the
task file's mode, readable by every user, for a runner that runs as another one. Hard links
share the task file's inode, so nothing here changes a file's mode or contents; the
mount is read-only.

What is checked here is what is cheap and has to hold before a container starts:
the address resolves inside the task's directory, names a regular file, and has
the size the description records. The bytes themselves are checked once, by the
runner, which hashes each file while it copies it out of the mount
(``tool_artifacts._copy_verified_refs``): that check stays the one that decides,
and this one only refuses early, listing every file at fault.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tolokaforge.runner.models import ArtifactRef

__all__ = [
    "ENGINE_DIR_NAME",
    "REFS_DIR_NAME",
    "RefsStagingError",
    "StagedRefs",
    "is_engine_path",
    "mark_owner",
    "stage_referenced_files",
    "sweep_stale_staging",
]

logger = logging.getLogger(__name__)

ENGINE_DIR_NAME = ".tolokaforge"
"""The hidden directory the engine keeps its own files in beside a task, or in a run's
directory; every walk of a pack's files skips it (:func:`is_engine_path`)."""

REFS_DIR_NAME = "refs"
"""Under :data:`ENGINE_DIR_NAME`: one directory per trial's staged references."""

_OWNER_FILE = ".owner"

STALE_AFTER_S = 24 * 60 * 60
"""A trial's staging directory left this long is stale whoever owns it."""

_COPY_ERRNOS = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP})
"""A refused link the staging copies the file for: another filesystem, links not
allowed (protected hard links, a filesystem without them), or the inode's link limit."""


def is_engine_path(relative: Path | str) -> bool:
    """Whether a path relative to a pack walk's root lies under :data:`ENGINE_DIR_NAME`."""
    return ENGINE_DIR_NAME in Path(relative).parts


def mark_owner(root: Path) -> None:
    """Record this process as the owner of a trial's staging ``root``, for the sweep."""
    (root / _OWNER_FILE).write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname()}), encoding="utf-8"
    )


def sweep_stale_staging(base: Path, *, now: float | None = None) -> list[Path]:
    """Remove the trial staging directories under ``base`` an engine left behind; the removed.

    A directory is stale when it is older than :data:`STALE_AFTER_S`, or when its
    owner, recorded by :func:`mark_owner`, was a process on this host that is gone (an
    engine killed before its teardown). A directory of another host, or one without an
    owner, is judged by its age alone. Failures are logged, never raised.
    """
    removed: list[Path] = []
    moment = time.time() if now is None else now
    host = socket.gethostname()
    try:
        entries = list(base.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            stale = moment - entry.stat().st_mtime > STALE_AFTER_S
            if not stale:
                stale = _owner_is_gone(entry / _OWNER_FILE, host)
            if stale:
                shutil.rmtree(entry)
                removed.append(entry)
        except OSError:
            logger.warning("could not sweep the stale staging directory %s", entry, exc_info=True)
    if removed:
        logger.info("swept %d stale reference staging director(ies) under %s", len(removed), base)
    return removed


def _owner_is_gone(marker: Path, host: str) -> bool:
    try:
        owner = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if owner.get("host") != host or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


_REFUSALS_SHOWN = 10

_DIR_MODE = 0o755
"""The staging tree's directories: readable by a runner image that runs as another user."""


class RefsStagingError(ValueError):
    """A referenced file cannot be staged: missing, outside the task, or of another size."""


@dataclass(frozen=True)
class StagedRefs:
    """What :func:`stage_referenced_files` put in the staging directory."""

    directory: Path
    linked: int
    copied: int
    copied_bytes: int = 0


def stage_referenced_files(
    task_dir: Path, refs: Mapping[str, ArtifactRef], staging: Path
) -> StagedRefs:
    """Fill ``staging`` with the files ``refs`` address, at their ``pack:`` paths.

    ``staging`` must not exist yet; it is created with every directory the paths
    need. Several entries may address the same file, which is staged once.

    Raises:
        RefsStagingError: an address resolves outside ``task_dir``, names no
            regular file, or names one of another size than its entry records.
            Every entry is checked before the raise, and nothing is staged.
    """
    root = task_dir.resolve()
    if not root.is_dir():
        raise RefsStagingError(f"the task directory {task_dir} is not a directory on this machine")
    wanted: dict[str, ArtifactRef] = {}
    for ref in refs.values():
        wanted.setdefault(ref.pack_path, ref)
    refusals: list[str] = []
    sources: dict[str, Path] = {}
    for pack_path, ref in sorted(wanted.items()):
        source = (root / pack_path).resolve()
        if not source.is_relative_to(root):
            refusals.append(f"pack:{pack_path} resolves outside the task directory")
        elif not source.is_file():
            refusals.append(f"pack:{pack_path} is not a file in the task directory")
        elif (size := source.stat().st_size) != ref.size:
            refusals.append(f"pack:{pack_path} has {size} bytes, the description {ref.size}")
        else:
            sources[pack_path] = source
    if refusals:
        shown = "; ".join(refusals[:_REFUSALS_SHOWN])
        more = (
            f"; and {len(refusals) - _REFUSALS_SHOWN} more"
            if len(refusals) > _REFUSALS_SHOWN
            else ""
        )
        raise RefsStagingError(
            f"{len(refusals)} of {len(wanted)} referenced file(s) do not match the task's "
            f"description under {task_dir}: {shown}{more}. The task's files changed after it "
            "was described, or this is not the task's directory."
        )
    try:
        linked, copied, copied_bytes = _stage(staging, sources, wanted)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, RefsStagingError):
            raise
        if isinstance(exc, (OSError, shutil.Error)):
            raise RefsStagingError(
                f"staging the referenced files under {staging} failed: {exc}"
            ) from exc
        raise
    if copied:
        logger.warning(
            "staged %d referenced file(s) in %s: %d linked, %d copied (%d bytes): the staging "
            "directory is on another filesystem than the task's, or a link was refused",
            len(sources),
            staging,
            linked,
            copied,
            copied_bytes,
        )
    return StagedRefs(directory=staging, linked=linked, copied=copied, copied_bytes=copied_bytes)


def _stage(
    staging: Path, sources: Mapping[str, Path], wanted: Mapping[str, ArtifactRef]
) -> tuple[int, int, int]:
    """Link or copy each source into ``staging``; ``(linked, copied, copied bytes)``."""
    staging.mkdir(mode=_DIR_MODE)
    staging.chmod(_DIR_MODE)
    linked = copied = copied_bytes = 0
    for pack_path, source in sources.items():
        target = staging / pack_path
        _make_parents(staging, target.parent)
        if os.path.lexists(target):
            raise RefsStagingError(
                f"pack:{pack_path} lands on {target}, which another reference already staged: "
                "two addresses that differ only in case name one file on this filesystem"
            )
        try:
            os.link(source, target)
            linked += 1
            continue
        except OSError as exc:
            if exc.errno not in _COPY_ERRNOS:
                raise RefsStagingError(
                    f"pack:{pack_path} cannot be linked from {source} to {target}: {exc}"
                ) from exc
        try:
            shutil.copyfile(source, target)
        except shutil.SameFileError as exc:
            raise RefsStagingError(
                f"pack:{pack_path}: {source} and {target} are the same file"
            ) from exc
        # The runner may run as another user: keep the task file's mode, readable by all.
        target.chmod((source.stat().st_mode & 0o777) | 0o444)
        copied += 1
        copied_bytes += wanted[pack_path].size
    return linked, copied, copied_bytes


def _make_parents(staging: Path, directory: Path) -> None:
    """Create ``directory`` and its parents under ``staging`` with :data:`_DIR_MODE`."""
    missing: list[Path] = []
    while directory != staging and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir(mode=_DIR_MODE)
        path.chmod(_DIR_MODE)
