"""``TimerController`` — fire an intervention on a fixed interval.

Useful for chaos testing and demos. Callback receives the tick number so it
can vary the intervention (e.g. inject a different message each tick).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from intervener.binding import SessionBinding
from tolokaforge.session import TrialIntervention

__all__ = ["TimerController"]

TickCallback = Callable[[int, SessionBinding], TrialIntervention | None]


class TimerController:
    def __init__(self, interval_seconds: float, callback: TickCallback) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self._interval = interval_seconds
        self._callback = callback
        self._binding: SessionBinding | None = None
        self._terminal: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
        self._binding = binding
        self._terminal = terminal
        self._thread = threading.Thread(target=self._run, name="timer-controller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._binding = None

    def _run(self) -> None:
        assert self._terminal is not None
        tick = 0
        while not self._terminal.wait(timeout=self._interval):
            if self._binding is None:
                return
            intervention = self._callback(tick, self._binding)
            if intervention is not None:
                self._binding.submit(intervention)
            tick += 1
