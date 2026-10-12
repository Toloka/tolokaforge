"""Time that moves only when something waits: the clock and the sleep the retry tests inject."""

from __future__ import annotations

import threading


class FakeTime:
    """``clock`` reads it, ``sleep`` records the wait and moves it on; shared by threads."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds
