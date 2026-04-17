"""Unit tests for GlobalRateLimiter thread-safe rate limiting."""

import threading
import time

import pytest

from tolokaforge.core.rate_limiter import GlobalRateLimiter

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRateLimiterConstructor:
    """Validate constructor argument checks."""

    def test_constructor_rejects_zero(self) -> None:
        """requests_per_second=0 must raise ValueError."""
        with pytest.raises(ValueError, match="must be > 0"):
            GlobalRateLimiter(requests_per_second=0)

    def test_constructor_rejects_negative(self) -> None:
        """Negative requests_per_second must raise ValueError."""
        with pytest.raises(ValueError, match="must be > 0"):
            GlobalRateLimiter(requests_per_second=-5)

    def test_constructor_accepts_positive(self) -> None:
        """Positive requests_per_second should construct without error."""
        limiter = GlobalRateLimiter(requests_per_second=10)
        assert limiter._min_interval_s == pytest.approx(0.1)

    def test_constructor_accepts_fractional(self) -> None:
        """Fractional rates (e.g., 0.5 rps) should be accepted."""
        limiter = GlobalRateLimiter(requests_per_second=0.5)
        assert limiter._min_interval_s == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# acquire() behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRateLimiterAcquire:
    """Verify acquire() timing and blocking behaviour."""

    def test_acquire_returns_immediately_first_call(self) -> None:
        """First acquire() should return almost instantly."""
        limiter = GlobalRateLimiter(requests_per_second=2)
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"First acquire took {elapsed:.3f}s, expected < 0.1s"

    def test_acquire_enforces_rate_limit(self) -> None:
        """Two rapid acquire() calls at 2 rps should take ≥ 0.5s total."""
        limiter = GlobalRateLimiter(requests_per_second=2)  # 0.5s interval
        start = time.monotonic()
        limiter.acquire()
        limiter.acquire()
        elapsed = time.monotonic() - start
        # Second call must wait ~0.5s; allow tolerance
        assert elapsed >= 0.4, f"Two acquires took {elapsed:.3f}s, expected >= 0.4s"

    def test_high_rate_allows_fast_calls(self) -> None:
        """At 1000 rps the interval is 1ms — 5 calls should be near-instant."""
        limiter = GlobalRateLimiter(requests_per_second=1000)
        start = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"5 acquires at 1000 rps took {elapsed:.3f}s"

    def test_thread_safety(self) -> None:
        """Multiple threads calling acquire() concurrently should not crash."""
        limiter = GlobalRateLimiter(requests_per_second=100)
        errors: list[Exception] = []

        timestamps: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                for _ in range(5):
                    limiter.acquire()
                    with lock:
                        timestamps.append(time.monotonic())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert len(timestamps) == 20, f"Expected 20 acquire calls, got {len(timestamps)}"
