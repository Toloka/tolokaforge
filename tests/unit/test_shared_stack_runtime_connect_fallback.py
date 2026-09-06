"""Locks ``SharedStackRuntimeBackend.connect`` uses ``self.connect_timeout``
when the caller passes no arguments — the orchestrator's ``connect()`` path.

The orchestrator invokes ``runtime_backend.connect()`` with no args at both
call sites (``_construct_runtime_backend`` callers). Before the fallback,
the method-parameter default (30 s) governed regardless of what the factory
plumbed onto the instance from ``OrchestratorConfig.runtime_connect``.
This test locks the fallback so an operator-configured timeout actually
reaches the health-check loop on the primary connect path.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

pytestmark = pytest.mark.unit


def _backend_with_captured_calls(
    monkeypatch: pytest.MonkeyPatch, *, timeout: float, retry_interval: float
) -> tuple[SharedStackRuntimeBackend, list[dict]]:
    backend = SharedStackRuntimeBackend(
        runner_address="sentinel:50051",
        connect_timeout=timeout,
        connect_retry_interval=retry_interval,
    )
    call_log: list[dict] = []

    def fake_connect(timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        call_log.append({"timeout": timeout, "retry_interval": retry_interval})

    monkeypatch.setattr(backend.runner_client, "connect", fake_connect)
    return backend, call_log


def test_no_arg_connect_uses_instance_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, call_log = _backend_with_captured_calls(monkeypatch, timeout=90.0, retry_interval=0.5)
    backend.connect()
    assert call_log == [{"timeout": 90.0, "retry_interval": 0.5}]


def test_explicit_arg_wins_over_instance_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, call_log = _backend_with_captured_calls(monkeypatch, timeout=90.0, retry_interval=0.5)
    backend.connect(timeout=5.0, retry_interval=0.1)
    assert call_log == [{"timeout": 5.0, "retry_interval": 0.1}]


def test_partial_explicit_arg_falls_back_on_the_other(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, call_log = _backend_with_captured_calls(monkeypatch, timeout=90.0, retry_interval=0.5)
    backend.connect(timeout=42.0)
    assert call_log == [{"timeout": 42.0, "retry_interval": 0.5}]
