"""Helpers for project-level assets (seeds today; more categories
land as the design grows).

The primary API is :func:`compute_seed_digest`, which the CLI's
``tolokaforge assets stamp`` verb uses to write the ``digest``
field on ``assets.seeds.<name>`` entries. The load-time verifier
in :mod:`tolokaforge.core.project_loader` reuses the same helper —
one implementation, no drift between "what stamp writes" and "what
load verifies."
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_HASH_BLOCK_SIZE = 64 * 1024
"""Chunk size for streaming file reads. 64 KiB is large enough that
per-block overhead is negligible on typical seed files, small enough
that pathological multi-GiB dumps stay well within memory budget."""


def compute_seed_digest(path: Path) -> str:
    """Return ``"sha256:<64-hex>"`` for the seed at *path*.

    A **file** hashes its byte stream. A **directory** hashes a
    deterministic tree digest: the sorted sequence of every regular
    file's posix-relative path and content, so the digest is
    independent of filesystem iteration order but flips on any content
    edit or file rename/move. Symlinks and other non-regular files are
    excluded — regular files only.

    Raises ``FileNotFoundError`` when *path* is missing — the CLI
    catches this and prints the offending path so the author can fix
    the project.yaml reference.
    """
    if path.is_dir():
        return _compute_tree_digest(path)
    hasher = hashlib.sha256()
    _stream_file_into(path, hasher)
    return f"sha256:{hasher.hexdigest()}"


def _stream_file_into(path: Path, hasher: hashlib._Hash) -> None:
    """Feed the bytes of the file at *path* into *hasher* in
    :data:`_HASH_BLOCK_SIZE` chunks so a multi-GiB seed doesn't blow
    the memory budget.
    """
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_BLOCK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)


def _compute_tree_digest(root: Path) -> str:
    """Return ``"sha256:<64-hex>"`` over the directory tree at *root*.

    Collects every regular file under *root* (symlinks and non-regular
    files excluded), keyed by its posix-relative path, sorts by that
    path, then folds each ``(path, per-file-sha256)`` pair into one
    sha256. Sorting makes the digest independent of the OS's directory
    iteration order; feeding the relative path alongside the content
    hash makes a rename/move flip the digest even when no bytes change.
    """
    entries: list[tuple[str, Path]] = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        entries.append((candidate.relative_to(root).as_posix(), candidate))
    entries.sort(key=lambda entry: entry[0])

    tree_hasher = hashlib.sha256()
    for relative_path, file_path in entries:
        tree_hasher.update(relative_path.encode("utf-8"))
        tree_hasher.update(b"\0")
        file_hasher = hashlib.sha256()
        _stream_file_into(file_path, file_hasher)
        tree_hasher.update(file_hasher.hexdigest().encode("ascii"))
        tree_hasher.update(b"\n")
    return f"sha256:{tree_hasher.hexdigest()}"
