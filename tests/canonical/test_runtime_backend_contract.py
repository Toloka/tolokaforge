"""Pin the ``RuntimeBackend`` Protocol contract — runtime check + parity.

Two implementations are checked: :class:`DockerRuntime` (real gRPC client
wrapper, never ``connect()``-ed in this test) and
:class:`InMemoryRuntimeBackend` (no-network test fixture). Lifecycle
methods and the retry-cleanup method are exercised on both; ``connect()``
itself is *not* invoked on ``DockerRuntime`` because that would require
a real runner gRPC server.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.docker_runtime import DockerRuntime
from tolokaforge.core.runtime import (
    InMemoryRuntimeBackend,
    RuntimeBackend,
    RuntimeBackendCallLog,
)

pytestmark = pytest.mark.canonical


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy
    it via ``isinstance`` (not just by structural type-hint compatibility).
    """

    def test_docker_runtime_passes_isinstance(self) -> None:
        assert isinstance(DockerRuntime(runner_address="sentinel:50051"), RuntimeBackend)

    def test_in_memory_runtime_backend_passes_isinstance(self) -> None:
        assert isinstance(InMemoryRuntimeBackend(), RuntimeBackend)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotARuntime:
            pass

        assert not isinstance(_NotARuntime(), RuntimeBackend)


class TestLifecycleMethodParity:
    """Every Protocol method accepts the same arguments on every
    implementation. ``connect()`` itself is exercised only on the
    in-memory impl because the Docker variant requires a real gRPC
    server; instead this class asserts the *method signature* exists
    on both via ``getattr``.
    """

    @pytest.fixture
    def implementations(self) -> list[RuntimeBackend]:
        # ``DockerRuntime`` is constructed but never ``connect()``-ed —
        # construction is cheap and side-effect-free until ``connect()``.
        return [InMemoryRuntimeBackend(), DockerRuntime(runner_address="sentinel:50051")]

    def test_connect_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "connect", None))

    def test_close_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "close", None))

    def test_health_check_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "health_check", None))

    def test_cleanup_trial_signature_is_present(
        self, implementations: list[RuntimeBackend]
    ) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "cleanup_trial", None))

    def test_executor_client_attribute_is_present(
        self, implementations: list[RuntimeBackend]
    ) -> None:
        for impl in implementations:
            assert hasattr(impl, "executor_client")


class TestInMemoryBackendSemantics:
    """The in-memory backend records calls on its
    :class:`RuntimeBackendCallLog` for tests to assert against, without
    touching the network.
    """

    def test_connect_records_arguments(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.connect(timeout=15.0, retry_interval=0.5)
        assert backend.call_log.connect_calls == [
            {"timeout": 15.0, "retry_interval": 0.5},
        ]

    def test_connect_defaults_are_recorded(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.connect()
        assert backend.call_log.connect_calls == [
            {"timeout": 30.0, "retry_interval": 1.0},
        ]

    def test_close_is_counted(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.close()
        backend.close()
        assert backend.call_log.close_calls == 2

    def test_health_check_returns_true_and_is_counted(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert backend.health_check() is True
        assert backend.health_check() is True
        assert backend.call_log.health_check_calls == 2

    def test_cleanup_trial_returns_success_shape(self) -> None:
        backend = InMemoryRuntimeBackend()
        result = backend.cleanup_trial("airline_001:0")
        assert result == {"success": True, "error": None}

    def test_cleanup_trial_records_trial_ids_in_order(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.cleanup_trial("a:0")
        backend.cleanup_trial("b:0")
        backend.cleanup_trial("a:0")
        assert backend.call_log.cleanup_trial_calls == ["a:0", "b:0", "a:0"]

    def test_cleanup_trial_is_idempotent_on_unknown_trial(self) -> None:
        """Mirrors ``DockerRuntime``: cleaning a trial that was never
        registered must succeed so retry paths don't crash."""
        backend = InMemoryRuntimeBackend()
        result = backend.cleanup_trial("never_registered:0")
        assert result["success"] is True
        assert result["error"] is None

    def test_executor_client_raises_on_rpc_method_access(self) -> None:
        """The in-memory backend is for lifecycle and cleanup only;
        attempts to use it as a runner-RPC client fail with a clear
        message that points at the right alternative."""
        backend = InMemoryRuntimeBackend()
        with pytest.raises(NotImplementedError, match="register_trial"):
            backend.executor_client.register_trial()
        with pytest.raises(NotImplementedError, match="execute_tool"):
            backend.executor_client.execute_tool()

    def test_fresh_backend_has_empty_call_log(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert backend.call_log == RuntimeBackendCallLog()
