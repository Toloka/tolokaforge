"""A minimal conversion adapter for the entry-point discovery smoke test.

``DemoConversionAdapter`` subclasses the concrete :class:`NativeAdapter` (so
every :class:`BaseAdapter` abstract method is already satisfied) and overrides
only the conversion surface. It reads simple JSON source files — each a mapping
with at least a ``name`` key — matched by the configured ``tasks_glob``, and
emits one native task bundle per file plus a single shared-resources marker.
"""

from __future__ import annotations

import glob as glob_module
import json
from pathlib import Path
from typing import Any

from tolokaforge.adapters.base import NativeTaskBundle
from tolokaforge.adapters.native import NativeAdapter


class DemoConversionAdapter(NativeAdapter):
    """Convert JSON source files to native task bundles."""

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self._sources: dict[str, Path] | None = None

    def _discover_sources(self) -> dict[str, Path]:
        if self._sources is None:
            self._sources = {
                Path(match).stem: Path(match)
                for match in glob_module.glob(self.tasks_glob, recursive=True)
            }
        return self._sources

    def get_task_ids(self) -> list[str]:
        return sorted(self._discover_sources().keys())

    def convert_to_native(self, task_id: str) -> NativeTaskBundle:
        source = self._discover_sources()[task_id]
        data = json.loads(source.read_text(encoding="utf-8"))
        name = data["name"]
        return NativeTaskBundle(
            task_config={
                "name": name,
                "description": f"Demo task converted from {source.name}",
                "category": "tool_use",
            },
            grading_config={"combine": {"method": "weighted", "pass_threshold": 1.0}},
            fixtures={"tools": [{"name": "noop"}]},
            metadata={"source_adapter": "demo", "source_file": source.name},
        )

    def write_shared_resources(self, output_dir: Path, bundle: NativeTaskBundle) -> None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "_shared_marker").write_text("ok", encoding="utf-8")
