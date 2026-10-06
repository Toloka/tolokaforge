"""Discovery of Inspect AI tasks for the tolokaforge adapter.

Enumerates ``@task`` functions in a task pack using Inspect's public
``list_tasks`` API and records the (name, file) address each one is run by, so the
adapter can translate every Inspect task into a tolokaforge ``TaskConfig`` without
executing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InspectTaskInfo:
    """One discovered Inspect task, addressable as ``<file>@<name>``."""

    task_id: str
    name: str
    file: Path
    task_dir: Path
    attribs: dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        """The ``inspect eval`` target for this task."""
        return f"{self.file}@{self.name}"


def discover_inspect_tasks(
    pack_dir: Path, globs: list[str] | None = None
) -> dict[str, InspectTaskInfo]:
    """Return ``{task_id: InspectTaskInfo}`` for every Inspect task under ``pack_dir``.

    ``globs`` narrows the scan (relative to ``pack_dir``); an empty/omitted value
    scans the whole pack. Task ids are the Inspect task names; on a name collision
    across files the later file's relative parent is prefixed to keep ids unique.
    """
    from inspect_ai import list_tasks

    pack_dir = Path(pack_dir).resolve()
    infos = list_tasks(globs or [], absolute=True, root_dir=pack_dir)

    out: dict[str, InspectTaskInfo] = {}
    for info in infos:
        file = Path(info.file).resolve()
        task_id = info.name
        if task_id in out:
            rel = (
                file.parent.relative_to(pack_dir) if file.is_relative_to(pack_dir) else file.parent
            )
            task_id = f"{rel}/{info.name}"
        out[task_id] = InspectTaskInfo(
            task_id=task_id,
            name=info.name,
            file=file,
            task_dir=file.parent,
            attribs=dict(info.attribs or {}),
        )
    return out
