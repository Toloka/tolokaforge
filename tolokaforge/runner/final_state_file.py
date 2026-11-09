"""A trial's final state as a file in a mounted directory, not a message (ADR-0069).

Under ``compute.trial_data_delivery: mount`` the engine bind-mounts a per-trial writable
host directory into the runner service at :data:`FINAL_STATE_MOUNT_PATH` and
names it by :data:`FINAL_STATE_DIR_ENV_VAR`. ``GetState`` with
``state_by_reference`` then writes the state there (:func:`write_state_file`)
and answers with a :class:`StateFile` — the file's path relative to the
directory, its SHA-256 and its size — instead of the JSON on the wire. The host
checks the file against those (:func:`verify_state_file`) without parsing it,
and copies it into the trial bundle (:func:`copy_state_file`), checking again
as it copies.

Both ends import this module, so the mount path, the variable and the file's
name are spelled once.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FINAL_STATE_DIR_ENV_VAR",
    "FINAL_STATE_FILENAME",
    "FINAL_STATE_MOUNT_PATH",
    "FinalStateFileError",
    "StateFile",
    "copy_state_file",
    "final_state_dir_from_env",
    "resolve_state_file",
    "verify_state_file",
    "write_state_file",
]

FINAL_STATE_MOUNT_PATH = "/tolokaforge/final-state"
"""Where the engine mounts the trial's final-state directory in the runner service."""

FINAL_STATE_DIR_ENV_VAR = "TOLOKAFORGE_FINAL_STATE_DIR"
"""Names the final-state directory to the runner; set by the engine with the mount."""

FINAL_STATE_FILENAME = "final_state.json"
"""The state file's name, in the runner's directory and in the trial bundle."""

_CHUNK_BYTES = 1024 * 1024


class FinalStateFileError(Exception):
    """A state file is missing, outside its directory, or not the bytes it was reported as."""


@dataclass(frozen=True)
class StateFile:
    """A state file and what it must hash to.

    ``path`` is relative to the final-state directory on the wire, and absolute
    on the host once :func:`resolve_state_file` has placed it under the host's
    side of the mount.
    """

    path: str
    sha256: str
    size: int

    def as_ref(self, name: str = FINAL_STATE_FILENAME) -> dict[str, Any]:
        """The reference ``env.yaml`` records for the file once it is in the bundle as *name*."""
        return {"file": name, "sha256": self.sha256, "size": self.size}


def final_state_dir_from_env() -> Path | None:
    """The final-state directory the engine mounted into this runner, or ``None``."""
    value = os.environ.get(FINAL_STATE_DIR_ENV_VAR, "").strip()
    return Path(value) if value else None


def write_state_file(directory: Path, state: Mapping[str, Any]) -> StateFile:
    """Write *state* to *directory* as one JSON object, hashing while it writes.

    The object is written one top-level key (one table) at a time, each with
    the C encoder, so the writer never holds more than one table's text; the
    bytes are those ``json.dumps(state)`` returns, the ``state_json`` an inline
    answer would carry. The file is written under a temporary name and renamed
    into place, so a reader never sees a partial file under
    :data:`FINAL_STATE_FILENAME`.
    """
    digest = hashlib.sha256()
    size = 0
    tmp = directory / f".{FINAL_STATE_FILENAME}.partial"
    try:
        with tmp.open("wb") as out:

            def put(text: str) -> None:
                nonlocal size
                data = text.encode("utf-8")
                digest.update(data)
                size += len(data)
                out.write(data)

            put("{")
            for index, (key, value) in enumerate(state.items()):
                # One-entry objects, unwrapped: the key is coerced and the
                # separators chosen exactly as ``json.dumps(state)`` would.
                put((", " if index else "") + json.dumps({key: value})[1:-1])
            put("}")
        os.replace(tmp, directory / FINAL_STATE_FILENAME)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return StateFile(path=FINAL_STATE_FILENAME, sha256=digest.hexdigest(), size=size)


def resolve_state_file(directory: Path, reported: StateFile) -> StateFile:
    """*reported* with its relative path placed under *directory*.

    Raises:
        FinalStateFileError: the path is not a plain relative path, or leaves
            *directory*.
    """
    rel = reported.path
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        raise FinalStateFileError(
            f"the runner reported state file {rel!r}, which is not a relative path inside "
            "its final-state directory"
        )
    root = directory.resolve()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise FinalStateFileError(
            f"the runner's state file {rel!r} resolves outside the final-state directory"
        )
    return StateFile(path=str(candidate), sha256=reported.sha256, size=reported.size)


def verify_state_file(state_file: StateFile) -> None:
    """Check that the file at ``state_file.path`` is the bytes it was reported as.

    Reads the file in 1 MiB chunks and never holds it.

    Raises:
        FinalStateFileError: the file is missing, or its size or SHA-256 differs.
    """
    path = Path(state_file.path)
    if not path.is_file():
        raise FinalStateFileError(f"the state file {path} does not exist")
    actual_size = path.stat().st_size
    if actual_size != state_file.size:
        raise FinalStateFileError(
            f"the state file {path} is {actual_size} bytes; the runner reported {state_file.size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    if digest.hexdigest() != state_file.sha256:
        raise FinalStateFileError(
            f"the state file {path} hashes to {digest.hexdigest()}; the runner reported "
            f"{state_file.sha256}"
        )


def copy_state_file(state_file: StateFile, destination: Path) -> None:
    """Copy the file at ``state_file.path`` to *destination*, checking it as it copies.

    The copy is written beside *destination* under a temporary name and renamed
    into place only once its size and SHA-256 match; on a mismatch nothing is
    left at *destination*.

    Raises:
        FinalStateFileError: the source is missing or is not the bytes reported.
    """
    source = Path(state_file.path)
    if not source.is_file():
        raise FinalStateFileError(f"the state file {source} does not exist")
    tmp = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            for chunk in iter(lambda: src.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
        if size != state_file.size or digest.hexdigest() != state_file.sha256:
            raise FinalStateFileError(
                f"the state file {source} changed after it was captured: it now measures "
                f"{size} bytes with SHA-256 {digest.hexdigest()}, not {state_file.size} bytes "
                f"with {state_file.sha256}"
            )
        os.replace(tmp, destination)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
