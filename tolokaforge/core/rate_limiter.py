"""Thread-safe global rate limiter utilities."""

from __future__ import annotations

import threading
import time


class GlobalRateLimiter:
    """Simple thread-safe global requests/sec limiter.

    This limiter enforces a minimum interval between acquisitions across all
    workers sharing the same instance.
    """

    def __init__(self, requests_per_second: float):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self._min_interval_s = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed_s = 0.0

    def acquire(self) -> None:
        """Block until the caller can issue the next request."""
        while True:
            wait_s = 0.0
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed_s:
                    self._next_allowed_s = now + self._min_interval_s
                    return
                wait_s = self._next_allowed_s - now
            if wait_s > 0:
                time.sleep(wait_s)
