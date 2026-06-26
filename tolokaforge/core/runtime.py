"""``RuntimeBackend`` Protocol — the orchestrator's execution surface.

The orchestrator dispatches trials to an execution environment through a
runtime backend. Today's only concrete backend is
:class:`tolokaforge.core.docker_runtime.DockerRuntime` (a thin gRPC
client wrapper around the runner container); this module declares the
Protocol that decouples the orchestrator from that single implementation.

* :class:`RuntimeBackend` — the Protocol the orchestrator depends on.
* :class:`InMemoryRuntimeBackend` — a non-gRPC implementation used as a
  test fixture and as proof the seam is swappable. Records lifecycle and
  cleanup calls on a :class:`RuntimeBackendCallLog`; the ``executor_client``
  stub raises :class:`NotImplementedError` on RPC methods (callers that
  need a real RPC must use :class:`DockerRuntime` or mock ``RunnerClient``
  directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from tolokaforge.core.docker_runtime import RunnerClient

__all__ = [
    "InMemoryRuntimeBackend",
    "RuntimeBackend",
    "RuntimeBackendCallLog",
]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeBackend(Protocol):
    """The orchestrator's execution surface for trials.

    Declares the lifecycle the orchestrator drives and the one
    trial-state operation that runs *before* a per-trial adapter exists
    (the retry-cleanup path). Per-trial operations after registration
    flow through :class:`DockerRunnerAdapter`, which is constructed
    from :attr:`executor_client`.
    """

    # ---- Lifecycle ----
    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Establish the runtime connection (or no-op for an in-memory impl).

        ``DockerRuntime`` waits for the gRPC server to become healthy
        under a timeout / retry loop; in-memory implementations record
        the call and return immediately.
        """
        ...

    def close(self) -> None:
        """Release the runtime connection. Idempotent."""
        ...

    def health_check(self) -> bool:
        """Probe whether the runtime is currently usable."""
        ...

    # ---- Retry-cleanup path (called before a per-trial adapter exists) ----
    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        """Forget any prior registration of ``trial_id`` on the runtime.

        Idempotent: cleaning a trial that isn't currently registered
        succeeds. Returns the same shape as
        :meth:`RunnerClient.cleanup_trial` —
        ``{"success": bool, "error": str | None}``.
        """
        ...

    # ---- Per-trial adapter handoff ----
    executor_client: RunnerClient
    """The RPC client used by :class:`DockerRunnerAdapter` for per-trial
    operations after a trial is registered.

    Typed as the concrete :class:`RunnerClient` for now. Promoting it to
    its own Protocol is a follow-up if a non-gRPC backend ever wants to
    fake the runner-RPC surface without the gRPC stack."""


# ---------------------------------------------------------------------------
# InMemoryRuntimeBackend — non-gRPC, test fixture
# ---------------------------------------------------------------------------


@dataclass
class RuntimeBackendCallLog:
    """Records what an :class:`InMemoryRuntimeBackend` was asked to do.

    Tests assert on this directly instead of mocking individual methods.
    Each entry is the method name plus a snapshot of its arguments.
    """

    connect_calls: list[dict[str, Any]] = field(default_factory=list)
    close_calls: int = 0
    health_check_calls: int = 0
    cleanup_trial_calls: list[str] = field(default_factory=list)


class _UnusableExecutorClient:
    """Stub for ``InMemoryRuntimeBackend.executor_client``.

    The in-memory backend is for lifecycle-and-cleanup testing only;
    code that needs to exercise the runner RPC surface (``register_trial``,
    ``execute_tool``, ``grade_trial``, …) must either use
    :class:`DockerRuntime` or mock :class:`RunnerClient` directly.
    Every attribute access raises ``NotImplementedError`` with that
    message so the failure mode is obvious.
    """

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(
            f"InMemoryRuntimeBackend.executor_client.{name}() is not implemented. "
            "Tests that exercise the runner RPC surface must use DockerRuntime "
            "or mock RunnerClient directly."
        )


class InMemoryRuntimeBackend:
    """Non-gRPC :class:`RuntimeBackend` implementation.

    Records lifecycle and cleanup calls on :attr:`call_log`; exposes a
    stub :attr:`executor_client` that raises on RPC method access.
    """

    def __init__(self) -> None:
        self.call_log = RuntimeBackendCallLog()
        self.executor_client: Any = _UnusableExecutorClient()

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        self.call_log.connect_calls.append({"timeout": timeout, "retry_interval": retry_interval})

    def close(self) -> None:
        self.call_log.close_calls += 1

    def health_check(self) -> bool:
        self.call_log.health_check_calls += 1
        return True

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        self.call_log.cleanup_trial_calls.append(trial_id)
        return {"success": True, "error": None}
