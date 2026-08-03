"""Writing a task directory that a migration declaration is read out of.

Both tiers go through one writer, so the unit tier's hand-built packs and the
canonical tier's copies of shipped packs meet ``inspect_migration_declaration``
through the same on-disk shape: a ``grading.yaml`` and the ``migration.yaml``
beside it. The grading side is passed as **text** so a canonical caller can hand
over a shipped file verbatim and lock what that pack actually declares, rather
than a paraphrase of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.grading.migration_declaration import MIGRATION_FILENAME


def write_migration_pack(
    directory: Path,
    *,
    grading_text: str,
    migration: Mapping[str, Any] | list[Any] | None = None,
) -> Path:
    """Write the pack's files into ``directory``; return its ``grading.yaml`` path.

    ``migration`` is dumped rather than indented into a fixture string, because an
    indented string mis-nests its keys and turns a rejection test green having
    rejected the indentation. ``None`` leaves the directory without a sidecar.
    """
    directory.mkdir(parents=True, exist_ok=True)
    grading_path = directory / "grading.yaml"
    grading_path.write_text(grading_text)
    if migration is not None:
        (directory / MIGRATION_FILENAME).write_text(yaml.safe_dump(migration))
    return grading_path
