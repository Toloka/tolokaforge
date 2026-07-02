"""``RuntimeBackend`` Protocol — the orchestrator's execution surface.

The orchestrator dispatches trials to an execution environment through a
runtime backend. Today's only concrete backend is
:class:`tolokaforge.core.docker_runtime.DockerRuntime` (a thin gRPC
client wrapper around the runner container); this module declares the
Protocol that decouples the orchestrator from that single implementation.

* :class:`RuntimeBackend` — the Protocol the orchestrator depends on.
* :class:`EnvHandle` — opaque per-trial handle returned by
  :meth:`RuntimeBackend.provision`.
* :class:`ProvisionError` — typed failure for the provisioning lifecycle.
* :class:`InMemoryRuntimeBackend` — a non-gRPC implementation used as a
  test fixture and as proof the seam is swappable. Records lifecycle,
  cleanup, and provisioning calls on a :class:`RuntimeBackendCallLog`;
  the ``executor_client`` stub raises :class:`NotImplementedError` on
  RPC methods (callers that need a real RPC must use
  :class:`DockerRuntime` or mock ``RunnerClient`` directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from tolokaforge.core.trial import EnvEndpoints, TrialSpec

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from tolokaforge.core.docker_runtime import RunnerClient

__all__ = [
    "EnvHandle",
    "InMemoryRuntimeBackend",
    "ProvisionError",
    "ProvisionStage",
    "RuntimeBackend",
    "RuntimeBackendCallLog",
]


# ---------------------------------------------------------------------------
# EnvHandle — opaque per-trial handle
# ---------------------------------------------------------------------------


@runtime_checkable
class EnvHandle(Protocol):
    """Opaque per-trial handle returned by :meth:`RuntimeBackend.provision`.

    Each backend defines the handle's internal shape (compose project name,
    pod identity, remote-lease id). Callers may only read :attr:`trial_id`;
    everything else is backend-private.

    The typing is deliberately narrow so future backends can serialise the
    handle across a process boundary (a remote provisioner returning a
    small dict) without breaking the Protocol.
    """

    trial_id: str


# ---------------------------------------------------------------------------
# ProvisionError — typed failure for the provisioning lifecycle
# ---------------------------------------------------------------------------


ProvisionStage = Literal["provision", "await_ready"]


class ProvisionError(Exception):
    """Raised when :meth:`RuntimeBackend.provision` or
    :meth:`RuntimeBackend.await_ready` cannot bring the trial environment
    up to the state the manifest declares.

    The provisioner is expected to make a best-effort teardown of anything
    it partially materialised before raising. Callers may call
    :meth:`RuntimeBackend.teardown` again defensively — teardown is
    idempotent.
    """

    def __init__(self, *, trial_id: str, stage: ProvisionStage, reason: str) -> None:
        super().__init__(f"[{stage}] trial={trial_id}: {reason}")
        self.trial_id = trial_id
        self.stage = stage
        self.reason = reason


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeBackend(Protocol):
    """The orchestrator's execution surface for trials.

    Declares the run-level lifecycle, the per-trial provisioning surface,
    and the one trial-state operation that runs *before* a per-trial
    adapter exists (the retry-cleanup path). Per-trial operations after
    registration flow through :class:`DockerRunnerAdapter`, which is
    constructed from :attr:`executor_client`.
    """

    # ---- Run-level lifecycle ----
    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Establish the runtime connection (or no-op for an in-memory impl).

        ``DockerRuntime`` waits for the gRPC server to become healthy
        under a timeout / retry loop; in-memory implementations record
        the call and return immediately.

        Idempotent. Calling ``connect()`` on an instance that is already
        connected re-runs the health check but does not re-create the
        underlying connection. Callers (including tests injecting
        a pre-connected backend) can rely on this — the orchestrator
        invokes ``connect()`` unconditionally at the start of every run.
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

    # ---- Per-trial provisioning surface (ADR-0010) ----
    def provision(self, spec: TrialSpec) -> EnvHandle:
        """Bring up the trial's environment; return an opaque handle.

        Reads ``spec.task.environment_manifest`` to materialise the
        services the manifest declares. When ``environment_manifest`` is
        ``None``, the backend returns a handle pointing at whatever
        shared-stack view it keeps (backwards-compat).

        A backend that cannot enforce a declared property of the manifest
        (``read_only`` mount, ``network`` mode, ``resources`` cap,
        ``security_context`` field) MUST raise :class:`ProvisionError`
        with ``stage="provision"`` rather than silently degrade.

        On partial-startup failure the backend is expected to attempt a
        best-effort teardown of anything already brought up before
        raising. Callers may still call :meth:`teardown` defensively —
        teardown is idempotent.
        """
        ...

    def await_ready(self, handle: EnvHandle) -> None:
        """Block until every service in ``handle`` passes its health probe.

        Raises :class:`ProvisionError` with ``stage="await_ready"`` on
        timeout. The handle remains valid after the failure; callers
        must invoke :meth:`teardown` to clean up the containers that were
        brought up before the probe timed out.
        """
        ...

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        """Resolve per-trial service URLs for ``handle``.

        Does NOT block on readiness; that is :meth:`await_ready`'s job.
        Returns a fully populated :class:`EnvEndpoints`; when the
        manifest is ``None``, returns the run-wide shared endpoints.
        """
        ...

    def teardown(self, handle: EnvHandle) -> None:
        """Stop and remove the resources the handle references.

        Idempotent — calling on an already-torn-down handle is a no-op,
        not an error. Best-effort — logs but does not raise if a
        resource is already gone.
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
    provisioned_trials: list[str] = field(default_factory=list)
    await_ready_calls: list[str] = field(default_factory=list)
    endpoints_calls: list[str] = field(default_factory=list)
    torn_down_trials: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _InMemoryEnvHandle:
    """Concrete handle type returned by :class:`InMemoryRuntimeBackend`.

    Satisfies the :class:`EnvHandle` Protocol structurally via the
    ``trial_id`` attribute.
    """

    trial_id: str


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
            f"Access to InMemoryRuntimeBackend.executor_client.{name} is not "
            "implemented. Tests that exercise the runner RPC surface must use "
            "DockerRuntime or mock RunnerClient directly."
        )


class InMemoryRuntimeBackend:
    """Non-gRPC :class:`RuntimeBackend` implementation.

    Records lifecycle, cleanup, and provisioning calls on :attr:`call_log`.
    Exposes a stub :attr:`executor_client` that raises on RPC method access.

    Constructor knobs let orchestrator-level tests exercise the failure
    branches of the provisioning contract without a real substrate:

    * ``fail_provision_after_service`` — when set, :meth:`provision`
      raises :class:`ProvisionError` with ``stage="provision"`` after
      recording that the named service in the manifest was reached. A
      best-effort :meth:`teardown` is called before the raise so the
      call log captures that pattern.
    * ``await_ready_times_out`` — when ``True``, :meth:`await_ready`
      raises :class:`ProvisionError` with ``stage="await_ready"``. The
      handle stays valid; the caller must still call :meth:`teardown`.

    Substrate-specific failure modes (e.g. "container already gone" on
    teardown) are not simulated here — those live in the concrete
    backend's own test module because the in-memory backend has no
    substrate to be in an anomalous state.
    """

    def __init__(
        self,
        *,
        fail_provision_after_service: str | None = None,
        await_ready_times_out: bool = False,
    ) -> None:
        self.call_log = RuntimeBackendCallLog()
        self.executor_client: Any = _UnusableExecutorClient()
        self._fail_provision_after_service = fail_provision_after_service
        self._await_ready_times_out = await_ready_times_out

    # ---- Run-level lifecycle ----
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

    # ---- Per-trial provisioning ----
    def provision(self, spec: TrialSpec) -> EnvHandle:
        trial_id = spec.trial_id
        self.call_log.provisioned_trials.append(trial_id)
        handle = _InMemoryEnvHandle(trial_id=trial_id)
        manifest = spec.task.environment_manifest
        if self._fail_provision_after_service is not None and manifest is not None:
            service_names = [s.name for s in manifest.services]
            if self._fail_provision_after_service in service_names:
                # Best-effort teardown of anything materialised so far.
                self.teardown(handle)
                raise ProvisionError(
                    trial_id=trial_id,
                    stage="provision",
                    reason=(
                        f"partial startup — failed after service "
                        f"{self._fail_provision_after_service!r}"
                    ),
                )
        return handle

    def await_ready(self, handle: EnvHandle) -> None:
        self.call_log.await_ready_calls.append(handle.trial_id)
        if self._await_ready_times_out:
            raise ProvisionError(
                trial_id=handle.trial_id,
                stage="await_ready",
                reason="health-probe timeout",
            )

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        self.call_log.endpoints_calls.append(handle.trial_id)
        return EnvEndpoints(
            db_url=f"http://in-memory-db-{handle.trial_id}:5432",
            rag_url=None,
            runner_url=f"http://in-memory-runner-{handle.trial_id}:50051",
        )

    def teardown(self, handle: EnvHandle) -> None:
        self.call_log.torn_down_trials.append(handle.trial_id)
