"""``EventReactiveController`` — the shape LLM/rule-based interveners take.

Wraps a callback ``(event, binding) → Optional[TrialIntervention]``. Also
implements :class:`~intervener.protocols.EventSink` so the drain loop
delivers events to it. Does not spawn its own thread — all logic runs on
the drain thread when ``on_event`` fires.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from intervener.binding import SessionBinding
from tolokaforge.session import TrialEvent, TrialIntervention

__all__ = ["EventReactiveController"]

Callback = Callable[[TrialEvent, SessionBinding], TrialIntervention | None]


class EventReactiveController:
    def __init__(self, callback: Callback) -> None:
        self._callback = callback
        self._binding: SessionBinding | None = None

    def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
        self._binding = binding

    def stop(self) -> None:
        self._binding = None

    def on_event(self, event: TrialEvent) -> None:
        binding = self._binding
        if binding is None:
            return
        intervention = self._callback(event, binding)
        if intervention is not None:
            binding.submit(intervention)

    def on_terminal(self) -> None:
        return None
