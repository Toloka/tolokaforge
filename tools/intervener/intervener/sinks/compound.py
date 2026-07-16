"""``CompoundSink`` — fan out one event stream to N sinks."""

from __future__ import annotations

from collections.abc import Iterable

from intervener.protocols import EventSink
from tolokaforge.session import TrialEvent

__all__ = ["CompoundSink"]


class CompoundSink:
    """Delegates ``on_event`` / ``on_terminal`` to each child sink in order.

    A child sink that raises does not prevent later sinks from receiving the
    same event — a failure in a metrics sink shouldn't blind the durable log.
    Exceptions are swallowed silently; production callers that care about
    error visibility can wrap children with their own logging.
    """

    def __init__(self, children: Iterable[EventSink]) -> None:
        self._children: list[EventSink] = list(children)

    def on_event(self, event: TrialEvent) -> None:
        for child in self._children:
            try:
                child.on_event(event)
            except Exception:
                pass

    def on_terminal(self) -> None:
        for child in self._children:
            try:
                child.on_terminal()
            except Exception:
                pass
