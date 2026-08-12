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

:func:`write_corpus_directory` writes what an entry's ``corpus`` has to resolve to,
so a fixture pack's pointer is a real directory rather than a nominal path — the
load refuses one that is not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.grading.corpus_curation import CORPUS_MANIFEST_FILENAME, CorpusManifest
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


def write_corpus_directory(directory: Path, *, criterion: str, parts: Sequence[str] = ()) -> Path:
    """Write a corpus at ``directory`` holding no bundle; return it.

    What makes a directory a corpus is the ``corpus.yaml`` ``tolokaforge curate`` writes,
    so the fixture writes a real manifest — of a curation that admitted nothing, which is
    the whole of what resolving a pointer reads. ``parts`` writes the multi-part shape
    instead: one corpus per named subdirectory, the parent carrying no manifest of its own.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not parts:
        (directory / CORPUS_MANIFEST_FILENAME).write_text(_manifest(criterion))
        return directory
    for part in parts:
        half = directory / part
        half.mkdir(parents=True, exist_ok=True)
        (half / CORPUS_MANIFEST_FILENAME).write_text(_manifest(criterion))
    return directory


def _manifest(criterion: str) -> str:
    return yaml.safe_dump(
        CorpusManifest(
            criterion=criterion,
            task_ids=[],
            curated_from=[],
            curated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundles=[],
            excluded=[],
        ).model_dump(mode="json"),
        sort_keys=False,
    )
