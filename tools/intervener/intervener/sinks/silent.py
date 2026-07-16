"""``SilentSink`` — /dev/null for events. Metrics-only participants pair one
of these with an event-reactive controller that accumulates its own stats."""

from __future__ import annotations

from tolokaforge.session import TrialEvent

__all__ = ["SilentSink"]


class SilentSink:
    def on_event(self, event: TrialEvent) -> None:
        return None

    def on_terminal(self) -> None:
        return None
