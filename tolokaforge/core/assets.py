"""Helpers for project-level assets (seeds today; more categories
land as the design grows).

The primary API is :func:`compute_seed_digest`, which the CLI's
``tolokaforge assets stamp`` verb uses to write the ``digest``
field on ``assets.seeds.<name>`` entries. The load-time verifier
that lands with the reset-recipe milestone will reuse the same
helper — one implementation, no drift between "what stamp writes"
and "what load verifies."
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_HASH_BLOCK_SIZE = 64 * 1024
"""Chunk size for streaming file reads. 64 KiB is large enough that
per-block overhead is negligible on typical seed files, small enough
that pathological multi-GiB dumps stay well within memory budget."""


def compute_seed_digest(path: Path) -> str:
    """Return ``"sha256:<64-hex>"`` for the file at *path*.

    Streams the file in :data:`_HASH_BLOCK_SIZE` chunks so a
    multi-GiB seed doesn't blow the memory budget. Raises
    ``FileNotFoundError`` when the file is missing — the CLI catches
    this and prints the offending path so the author can fix the
    project.yaml reference.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_BLOCK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
