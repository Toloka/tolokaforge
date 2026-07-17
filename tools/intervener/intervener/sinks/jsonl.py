"""``JsonlSink`` — one JSON object per event, appended to a stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from tolokaforge.session import TrialEvent

__all__ = ["JsonlSink"]


class JsonlSink:
    """Writes one JSON line per event.

    Accepts either an open text stream or a filesystem path. When given a
    path, opens the file in append mode on construction and closes it in
    ``on_terminal``.
    """

    def __init__(self, target: TextIO | Path | str) -> None:
        if isinstance(target, str | Path):
            self._path: Path | None = Path(target)
            self._stream: TextIO = self._path.open("a", encoding="utf-8")
            self._owns_stream = True
        else:
            self._path = None
            self._stream = target
            self._owns_stream = False

    def on_event(self, event: TrialEvent) -> None:
        record = event.model_dump(mode="json")
        self._stream.write(json.dumps(record, sort_keys=False))
        self._stream.write("\n")
        self._stream.flush()

    def on_terminal(self) -> None:
        if self._owns_stream:
            try:
                self._stream.close()
            except Exception:
                pass
