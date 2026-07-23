"""Locks ``LogRouter.stop()``'s prompt-teardown contract.

``stop()`` closes the docker log stream before joining the streaming
thread. Without the close, quiet-but-healthy containers pay the full
``timeout_s`` ceiling on every teardown because
``for chunk in log_stream:`` blocks on the underlying HTTP socket. The
close unblocks the iterator so the thread exits immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.docker.logging import LogRouter, LogRouterState

pytestmark = pytest.mark.unit


class _BlockingLogStream:
    """Iterator that mimics docker-py's follow=True stream shape."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.close_calls = 0

    def __iter__(self) -> _BlockingLogStream:
        return self

    def __next__(self) -> bytes:
        # Block until close() is called; a real docker stream blocks on
        # the underlying HTTP socket the same way.
        self._closed.wait()
        raise StopIteration

    def close(self) -> None:
        self.close_calls += 1
        self._closed.set()


def _fake_client_with_stream(log_stream: _BlockingLogStream) -> MagicMock:
    fake_container = MagicMock()
    fake_container.logs.return_value = log_stream
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container
    return fake_client


def _wait_for_stream(router: LogRouter, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while router._log_stream is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert router._log_stream is not None, "stream never became visible to stop()"


def test_stop_returns_promptly_on_quiet_stream() -> None:
    router = LogRouter(container_name="quiet", container_id="cid-1")
    stream = _BlockingLogStream()
    fake_client = _fake_client_with_stream(stream)

    with patch("docker.from_env", return_value=fake_client):
        router.start()
        try:
            _wait_for_stream(router)
            start = time.monotonic()
            router.stop(timeout_s=5.0)
            elapsed = time.monotonic() - start
        finally:
            if router.is_running:
                router.stop(timeout_s=5.0)

    assert stream.close_calls >= 1
    assert elapsed < 0.5, f"stop took {elapsed:.3f}s (expected < 0.5s)"
    assert router.state is LogRouterState.STOPPED


def test_stop_closes_stream_before_join() -> None:
    router = LogRouter(container_name="ordered", container_id="cid-2")
    stream = _BlockingLogStream()
    fake_client = _fake_client_with_stream(stream)

    order: list[str] = []
    original_close = stream.close

    def _spy_close() -> None:
        order.append("close")
        original_close()

    stream.close = _spy_close  # type: ignore[method-assign]

    with patch("docker.from_env", return_value=fake_client):
        router.start()
        thread = router._thread
        assert thread is not None
        original_join = thread.join

        def _spy_join(timeout: float | None = None) -> None:
            order.append("join-enter")
            original_join(timeout=timeout)
            order.append("join-exit")

        thread.join = _spy_join  # type: ignore[method-assign]

        try:
            _wait_for_stream(router)
            router.stop(timeout_s=5.0)
        finally:
            if router.is_running:
                router.stop(timeout_s=5.0)

    assert "close" in order
    assert "join-enter" in order
    assert order.index("close") < order.index("join-enter")


def test_stop_swallows_close_errors() -> None:
    router = LogRouter(container_name="rude", container_id="cid-3")
    stream = _BlockingLogStream()
    original_close = stream.close

    def _rude_close() -> None:
        original_close()
        raise RuntimeError("close raised after unblock")

    stream.close = _rude_close  # type: ignore[method-assign]
    fake_client = _fake_client_with_stream(stream)

    with patch("docker.from_env", return_value=fake_client):
        router.start()
        try:
            _wait_for_stream(router)
            router.stop(timeout_s=5.0)
        finally:
            if router.is_running:
                router.stop(timeout_s=5.0)

    assert router.state is LogRouterState.STOPPED


def test_stop_is_a_noop_when_stream_never_opened() -> None:
    router = LogRouter(container_name="never-started", container_id="cid-4")

    # No start() call — state is IDLE, no thread, no stream.
    assert router.state is LogRouterState.IDLE
    router.stop(timeout_s=5.0)

    assert router.state is LogRouterState.IDLE
    assert router._log_stream is None


def test_stop_handles_stream_absent_when_thread_still_starting() -> None:
    """``stop()`` before the thread reaches ``docker_container.logs(...)``."""
    router = LogRouter(container_name="pre-open", container_id="cid-5")
    router._state = LogRouterState.RUNNING
    router._thread = threading.Thread(target=lambda: None, daemon=True)
    router._thread.start()
    router._thread.join()

    # ``_log_stream`` is still None at this point; stop() must not raise.
    router.stop(timeout_s=1.0)

    assert router.state is LogRouterState.STOPPED


def test_setup_logging_is_not_needed_for_stop() -> None:
    # Sanity: nothing about ``stop()`` depends on ``setup_container_logging``.
    logging.getLogger("container").setLevel(logging.INFO)
    router = LogRouter(container_name="setup-free", container_id="cid-6")
    stream = _BlockingLogStream()
    fake_client = _fake_client_with_stream(stream)

    with patch("docker.from_env", return_value=fake_client):
        router.start()
        try:
            _wait_for_stream(router)
            router.stop(timeout_s=5.0)
        finally:
            if router.is_running:
                router.stop(timeout_s=5.0)

    assert stream.close_calls >= 1
