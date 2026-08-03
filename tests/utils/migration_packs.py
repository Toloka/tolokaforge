"""Writing a task directory that a migration declaration is read out of.

Both tiers go through one writer, so the unit tier's hand-built packs and the
canonical tier's copies of shipped packs meet ``inspect_migration_declaration``
through the same on-disk shape: a ``grading.yaml`` and the ``migration.yaml``
beside it. The grading side is passed as **text** so a canonical caller can hand
over a shipped file verbatim and lock what that pack actually declares, rather
than a paraphrase of it.

A ``task_id`` writes the ``task.yaml`` beside them, which is what makes the pack
*resolvable*: ``tolokaforge reconcile`` finds a pack by the id a recorded bundle
carries, so a pack written without one is reachable by the load-time gate and by
nothing that reads a corpus.
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
    task_id: str | None = None,
) -> Path:
    """Write the pack's files into ``directory``; return its ``grading.yaml`` path.

    ``migration`` is dumped rather than indented into a fixture string, because an
    indented string mis-nests its keys and turns a rejection test green having
    rejected the indentation. ``None`` leaves the directory without a sidecar.
    ``task_id`` adds the ``task.yaml`` a corpus's bundles are resolved through.
    """
    directory.mkdir(parents=True, exist_ok=True)
    grading_path = directory / "grading.yaml"
    grading_path.write_text(grading_text)
    if migration is not None:
        (directory / MIGRATION_FILENAME).write_text(yaml.safe_dump(migration))
    if task_id is not None:
        (directory / "task.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task_id,
                    "name": f"fixture pack for {task_id}",
                    "description": "A fixture task directory, resolvable by its task_id.",
                    "initial_state": {},
                    "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                    "actors": {"user": {"mode": "scripted", "scripted_flow": []}},
                    "grading": "grading.yaml",
                }
            )
        )
    return grading_path
