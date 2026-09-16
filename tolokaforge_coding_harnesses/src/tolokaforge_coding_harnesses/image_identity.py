"""Content identity for the image layer a harness trial runs inside.

The layer bakes engine-authored files into the trial image: the install
script, the middleware proxy, the generated Dockerfile, and whatever a
:class:`~tolokaforge_coding_harnesses.protocols.SkillDelivery` appended. A
tag keyed only on the harness name and its pinned CLI version cannot tell
two builds of different bytes apart, so a caller that reuses an image
because its tag resolves runs code nobody shipped.

:func:`harness_image_content_digest` names the layer by its bytes. Adapters
put the answer in the layered image tag, which makes "this tag resolves
locally" and "this tag was built from these bytes" the same statement.

Same construction as the engine's own image builder
(``tolokaforge.docker.image.Image._compute_content_hash``): sha256 over
path-labelled file bytes, truncated for the tag. Mirrored rather than
imported — this package installs without the engine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

__all__ = ["DIGEST_LENGTH", "harness_image_content_digest"]

DIGEST_LENGTH = 8
"""Hex characters of the sha256 that reach the image tag."""

_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})
_SKIP_SUFFIXES = (".log", ".tmp", ".swp")


def harness_image_content_digest(parts: Mapping[str, Path]) -> str:
    """Digest of the build inputs a harness image layer bakes in.

    *parts* maps the label a path carries in the digest — by convention its
    path inside the build context — to the file or directory on disk. A
    directory contributes every file beneath it in sorted order, labelled
    ``<label>/<path relative to the directory>``. Labels are hashed
    alongside bytes, so adding, removing or renaming a baked-in file moves
    the answer.

    Cache noise is skipped (VCS and virtualenv directories, bytecode caches,
    editor swapfiles, logs) so a file no build reads cannot move the digest.

    Raises:
        FileNotFoundError: A named path does not exist. Every part is
            something the caller just wrote into the build context, so a
            missing one means the digest does not describe that context.
    """
    hasher = hashlib.sha256()
    for label in sorted(parts):
        for rel, path in _digested_files(label, parts[label]):
            hasher.update(f"FILE:{rel}:".encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()[:DIGEST_LENGTH]


def _digested_files(label: str, path: Path) -> list[tuple[str, Path]]:
    """``(label, file)`` pairs *path* contributes, sorted by label."""
    if not path.exists():
        raise FileNotFoundError(
            f"harness image content digest: part {label!r} names {path}, which does "
            "not exist; the digest must describe the build context as written."
        )
    if path.is_file():
        return [(label, path)]
    return [
        (f"{label}/{child.relative_to(path).as_posix()}", child)
        for child in sorted(path.rglob("*"))
        if child.is_file() and not _is_cache_noise(child)
    ]


def _is_cache_noise(path: Path) -> bool:
    if _SKIP_DIR_NAMES.intersection(path.parts):
        return True
    return path.name.endswith(_SKIP_SUFFIXES)
