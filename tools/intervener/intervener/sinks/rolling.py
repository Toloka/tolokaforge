"""``RollingEventsSink`` — bounded in-memory buffer of the last N events.

Pair with any :class:`InteractiveTool` that needs "give me the recent
events" (e.g. :class:`~intervener.tools.reference.AnalyzeTool`,
:class:`~intervener.tools.reference.ContextTool`). Callers construct one,
add it to their participant's ``sinks=[…]`` list, and read from
``sink.events`` when building a :class:`ToolContext`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from tolokaforge.session import TrialEvent

__all__ = ["RollingEventsSink"]


class RollingEventsSink:
    """Keeps the most recent ``maxlen`` events (default 200). Oldest fall off."""

    def __init__(self, maxlen: int = 200) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be > 0")
        self._events: deque[TrialEvent] = deque(maxlen=maxlen)

    @property
    def events(self) -> list[TrialEvent]:
        """Snapshot of the current buffer, oldest first. Copy — safe to iterate."""
        return list(self._events)

    def __iter__(self) -> Iterable[TrialEvent]:
        return iter(list(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def on_event(self, event: TrialEvent) -> None:
        self._events.append(event)

    def on_terminal(self) -> None:
        return None
