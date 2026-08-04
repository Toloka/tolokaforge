"""Pin the ``connect()`` idempotency promise of the production runtime.

The ``RuntimeBackend`` Protocol's ``connect()`` docstring states that
calling on an already-connected instance re-runs the health check but
does not re-create the underlying connection — and the orchestrator
relies on this by invoking ``connect()`` unconditionally at the start
of every run.

This test exercises the only production implementation
(:class:`RunnerClient`, which :class:`SharedStackRuntimeBackend.connect` delegates
to) to prove the contract holds: a second ``connect()`` on an
already-healthy client does not raise, does not recreate the gRPC
channel, and just re-runs the health probe.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient, SharedStackRuntimeBackend

pytestmark = pytest.mark.unit


class TestRunnerClientConnectIdempotency:
    """``RunnerClient.connect()`` is safe to call repeatedly on a healthy
    client. The channel is created once (``if self.channel is None``) and
    subsequent calls re-run the health check.
    """

    def test_second_connect_does_not_recreate_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = GrpcRunnerClient(runner_address="sentinel:50051")

        # Pre-populate the channel + stub as if a previous connect() succeeded.
        existing_channel = MagicMock()
        existing_stub = MagicMock()
        client.channel = existing_channel
        client.stub = existing_stub

        # Force the health check to return True so connect() returns immediately.
        monkeypatch.setattr(client, "health_check", lambda: True)

        # Second connect must not blow away the existing channel.
        client.connect(timeout=1.0, retry_interval=0.01)

        assert client.channel is existing_channel
        assert client.stub is existing_stub

    def test_second_connect_does_not_raise_on_healthy_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orchestrator invokes connect() unconditionally at the start of
        every run. If the runtime is already healthy (e.g. an injected
        pre-connected backend), connect() must return cleanly — not raise."""
        client = GrpcRunnerClient(runner_address="sentinel:50051")
        client.channel = MagicMock()
        client.stub = MagicMock()
        monkeypatch.setattr(client, "health_check", lambda: True)

        # Must not raise.
        client.connect(timeout=1.0, retry_interval=0.01)

    def test_second_connect_reruns_health_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Idempotency means "safe to call again," not "no-op." The Protocol
        docstring promises the health check re-runs; pin that explicitly so a
        future implementation that turns it into a no-op (skipping the probe)
        fails this test loudly."""
        client = GrpcRunnerClient(runner_address="sentinel:50051")
        client.channel = MagicMock()
        client.stub = MagicMock()

        health_check_calls = 0

        def counting_health_check() -> bool:
            nonlocal health_check_calls
            health_check_calls += 1
            return True

        monkeypatch.setattr(client, "health_check", counting_health_check)

        client.connect(timeout=1.0, retry_interval=0.01)
        client.connect(timeout=1.0, retry_interval=0.01)

        assert health_check_calls == 2


class TestGrpcRunnerClientHealthCheckAcceptsDegraded:
    """``GrpcRunnerClient.health_check`` accepts both ``healthy`` and
    ``degraded`` from the runner's ``HealthCheckResponse``.

    The runner's ``HealthCheck`` RPC reports three status values per
    ``docs/GRPC_PROTOCOL.md`` § HealthCheck: ``healthy``, ``degraded``,
    ``unhealthy``. ``degraded`` means the runner's own gRPC surface is up
    and answering but a downstream service (DB, RAG) is unavailable. That
    is the runner's concern to surface via its own per-service warnings —
    it is NOT a signal the client should reject the runner itself for the
    purpose of a connect-time reachability probe. Only ``unhealthy`` or an
    ``RpcError`` are "the runner is dead" states.

    This test pins that semantics against a regression where an earlier
    strict ``status == "healthy"`` check made a downstream-less trial pack
    (a common shape: MB adapter smoke has no ``db-service`` in its per-trial
    compose) fail the 30-attempt connect loop even though the runner's
    ``HealthCheck`` RPC was successfully responding with ``degraded``.
    """

    def _client_with_status(self, status: str, monkeypatch: pytest.MonkeyPatch) -> GrpcRunnerClient:
        client = GrpcRunnerClient(runner_address="sentinel:50051")
        stub = MagicMock()
        response = MagicMock()
        response.status = status
        stub.HealthCheck.return_value = response
        client.stub = stub
        return client

    def test_healthy_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._client_with_status("healthy", monkeypatch)
        assert client.health_check() is True

    def test_degraded_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point of this test class — degraded runners are still
        callable, so health_check must return True."""
        client = self._client_with_status("degraded", monkeypatch)
        assert client.health_check() is True

    def test_unhealthy_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runner explicitly signalling unhealthy is a real fail."""
        client = self._client_with_status("unhealthy", monkeypatch)
        assert client.health_check() is False


class TestSharedStackRuntimeBackendConnectDelegates:
    """``SharedStackRuntimeBackend.connect`` is a one-line wrapper that delegates to
    ``RunnerClient.connect``. The Protocol's idempotency promise rides on
    that delegation; pin it so a future refactor (e.g. adding pre-connect
    side-effects) can't silently weaken the contract.
    """

    def test_connect_calls_through_to_runner_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = SharedStackRuntimeBackend(runner_address="sentinel:50051")

        call_log: list[dict] = []

        def fake_connect(timeout: float = 30.0, retry_interval: float = 1.0) -> None:
            call_log.append({"timeout": timeout, "retry_interval": retry_interval})

        monkeypatch.setattr(runtime.runner_client, "connect", fake_connect)

        runtime.connect(timeout=5.0, retry_interval=0.1)
        runtime.connect()  # second call — must also delegate cleanly

        assert call_log == [
            {"timeout": 5.0, "retry_interval": 0.1},
            {"timeout": 30.0, "retry_interval": 1.0},
        ]
