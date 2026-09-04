"""``RuntimeBackend`` Protocol — the orchestrator's execution surface.

The orchestrator dispatches trials to an execution environment through a
runtime backend. Today's only concrete backend is
:class:`tolokaforge.core.shared_stack_runtime.SharedStackRuntimeBackend` (a thin gRPC
client wrapper around the runner container); this module declares the
Protocol that decouples the orchestrator from that single implementation.

* :class:`RuntimeBackend` — the Protocol the orchestrator depends on.
* :class:`EnvHandle` — opaque per-trial handle returned by
  :meth:`RuntimeBackend.provision`.
* :class:`ProvisionError` — typed failure for the provisioning lifecycle.
* :class:`InMemoryRuntimeBackend` — a non-gRPC implementation used as a
  test fixture and as proof the seam is swappable. Records lifecycle,
  cleanup, and provisioning calls on a :class:`RuntimeBackendCallLog`;
  the per-trial RPC methods raise :class:`NotImplementedError` (tests
  that exercise the runner RPC surface must use :class:`SharedStackRuntimeBackend`
  or mock the methods on the backend instance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.core.models.trajectory import ProvisionStage
from tolokaforge.core.run_display_events import ContainerSnapshot
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints, TrialSpec

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from tolokaforge.core.grading.bundle import GradeBundleManifest
    from tolokaforge.core.models.trajectory import Trajectory
    from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext
    from tolokaforge.core.service_readiness import DiagnosticPayload
    from tolokaforge.runner.models import TaskDescription
    from tolokaforge.tools.registry import ToolResult

__all__ = [
    "EnvHandle",
    "InMemoryRuntimeBackend",
    "IsolationMode",
    "ProvisionError",
    "ProvisionStage",
    "RuntimeBackend",
    "RuntimeBackendCallLog",
]


class IsolationMode(str, Enum):
    """The isolation posture a :class:`RuntimeBackend` provides.

    Every backend advertises its mode via :attr:`RuntimeBackend.isolation_mode`.
    The orchestrator's task-vs-backend compatibility check reads this attribute
    rather than inspecting the concrete class — so a future backend on a
    different substrate (Kubernetes, Modal, ...) only has to set the attribute
    correctly to slot into the enforcement path.

    * ``SHARED_STACK`` — one substrate materialisation shared across every
      trial in the run. Cross-trial state contamination is structural.
    * ``PER_TRIAL_STACK`` — one substrate materialisation per trial.
      Concurrent trials are fully isolated.
    * ``COMPOSED_STACK`` — the composition plan spans more than one scope
      (task-scope-only or multi-scope). The composer materialises run-scope
      stacks at connect-time and trial-scope stacks at provision-time; state
      contamination is scoped per stack, not run-wide.
    """

    SHARED_STACK = "shared_stack"
    PER_TRIAL_STACK = "per_trial_stack"
    COMPOSED_STACK = "composed_stack"


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


class ProvisionError(Exception):
    """Raised when :meth:`RuntimeBackend.provision` or
    :meth:`RuntimeBackend.await_ready` cannot bring the trial environment
    up to the state the manifest declares.

    The provisioner is expected to make a best-effort teardown of anything
    it partially materialised before raising. Callers may call
    :meth:`RuntimeBackend.teardown` again defensively — teardown is
    idempotent.

    ``diagnostic`` carries a structured failure envelope when the readiness
    gate rejects a service (resolved endpoint, probe outcome, docker-side listen
    view); it is ``None`` for every other provisioning failure.
    """

    def __init__(
        self,
        *,
        trial_id: str,
        stage: ProvisionStage,
        reason: str,
        diagnostic: DiagnosticPayload | None = None,
    ) -> None:
        super().__init__(f"[{stage}] trial={trial_id}: {reason}")
        self.trial_id = trial_id
        self.stage = stage
        self.reason = reason
        self.diagnostic = diagnostic


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeBackend(Protocol):
    """The orchestrator's execution surface for trials.

    Declares three surfaces:

    * **Run-level lifecycle** — ``connect`` / ``close`` / ``health_check``.
      Called once per orchestrator run.
    * **Per-trial provisioning** (ADR-0010) — ``provision`` /
      ``await_ready`` / ``endpoints`` / ``teardown``. Materialises and
      tears down a trial's declared environment.
    * **Per-trial RPC operations** (ADR-0013) — ``register_trial`` /
      ``execute_tool`` / ``grade_trial`` / ``get_state`` / ``reset_trial``
      / ``cleanup_trial``. Every method takes ``trial_id`` explicitly;
      callers no longer construct an intermediate wrapper class to bind
      it.

    Every implementation advertises its :attr:`isolation_mode` — the
    orchestrator's task-vs-backend compatibility check reads that attribute
    rather than inspecting the concrete class, so future backends on other
    substrates (Kubernetes, Modal, ...) plug into the enforcement path by
    setting the attribute correctly.
    """

    isolation_mode: IsolationMode
    """The isolation posture this backend provides. Read by the orchestrator
    to refuse runs whose tasks declare an incompatible isolation
    requirement. Substrate-agnostic: any backend that shares state across
    trials sets :attr:`IsolationMode.SHARED_STACK`; any backend that
    materialises an independent substrate per trial sets
    :attr:`IsolationMode.PER_TRIAL_STACK`."""

    advertised_capabilities: frozenset[str]
    """Names from :data:`tolokaforge.core.backend_capabilities.CAPABILITY_REGISTRY`
    this backend honours. Read by
    :func:`tolokaforge.core.backend_capabilities.check_admission` at run
    start to reject runs whose ``compute.capabilities`` request exceeds
    the advertisement."""

    # ---- Run-level lifecycle ----
    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Establish the runtime connection (or no-op for an in-memory impl).

        ``SharedStackRuntimeBackend`` waits for the gRPC server to become healthy
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

    # ---- Per-trial RPC operations (ADR-0013) ----
    # trial_id is the first positional argument for every method — they used
    # to hang off ``DockerRunnerAdapter``, which curried trial_id at
    # construction time; now callers pass trial_id explicitly and there is
    # no wrapper class between them and the runtime.
    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Register a new trial with the runtime service.

        ``trial_spec_json`` is a serialised :class:`TrialSpec` — the runner
        reads ``spec.task`` for tool reconstruction and uses the rest of
        the spec for per-trial execution context. The default
        ``default_tool_timeout_s`` is
        :data:`tolokaforge.core.trial.DEFAULT_TOOL_TIMEOUT_S`; every
        backend uses the same default so a caller who supplies no timeout
        sees identical behaviour across implementations.

        Returns a dict shaped like
        ``{"success": bool, "error": str | None, "tool_schemas": list,
        "num_agent_tools": int, "num_user_tools": int}``.
        """
        ...

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        """Execute a tool call registered for ``trial_id``.

        No layer between the agent loop and the runner names a per-call
        budget: only the runner knows which tool is about to run, so it is
        the one layer that can resolve the budget that tool declares.

        ``executor`` names the caller environment (``"agent"`` or
        ``"user"``); the runtime routes the call to the matching tool
        registry inside the runner service. ``call_id`` is the provider's
        tool-call id, which the runner records so the call can be joined to
        the tool result it produced.

        Raises:
            TrialNotRegisteredError: the runner holds no registration for
                ``trial_id``, so the call reached no tool. Distinct from a
                failed :class:`ToolResult`: there is no tool outcome to
                record, and the trial ends here rather than the agent
                seeing a tool of its own fail.
        """
        ...

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        """Compute the grade for a completed trial.

        ``llm_messages_json`` is the transcript for transcript-rule /
        rubric-judge grading (``None`` when neither component is
        configured). ``grading_components`` narrows the components to
        compute (``None`` / empty = all). ``termination_reason`` is a
        :class:`~tolokaforge.core.models.TerminationReason` value naming how the
        trial ended, so grading can tell a deliberate finish from an exhausted
        budget; ``None`` when the caller reports none.

        Returns
        ``{"success": bool, "error": str | None, "grade": dict | None}``;
        the ``grade`` sub-dict mirrors :class:`tolokaforge.core.models.Grade`.
        """
        ...

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a snapshot of the trial's DB state (debugging + grading).

        ``include_unstable`` toggles inclusion of fields the state hash
        excludes; ``tables`` narrows the snapshot to the named tables
        (empty / ``None`` = all). Returns
        ``{"success": bool, "error": str | None, "state_json": str,
        "stable_hash": str, "full_hash": str}``.
        """
        ...

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        """Reset a trial's state to its registered initial state.

        ``execute_init_actions`` re-runs the manifest's initialisation
        actions after resetting the DB. Returns
        ``{"success": bool, "error": str | None, "state_hash": str}``.
        """
        ...

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        """Forget any prior registration of ``trial_id`` on the runtime.

        Idempotent: cleaning a trial that isn't currently registered
        succeeds. Returns
        ``{"success": bool, "error": str | None}``.

        Called by both the retry-cleanup path (before a per-trial adapter
        exists) and by trial teardown (after a successful trial run).
        Also clears any per-trial state the backend stashed via
        :meth:`remember_trial_inputs`.
        """
        ...

    def remember_trial_inputs(
        self,
        trial_id: str,
        trajectory: Trajectory,
        task_description: TaskDescription,
    ) -> None:
        """Stash the caller's trajectory + task description against
        ``trial_id`` so a subsequent :meth:`build_grade_bundle` call can
        emit them into the bundle.

        Called by the orchestrator's trial-end producer seam right before
        :meth:`build_grade_bundle`. Keeps ``build_grade_bundle`` a
        domain-clean ``(trial_id, out_dir)`` call — the alternative of
        passing the two objects as kwargs would couple the Protocol to
        :class:`~tolokaforge.core.models.trajectory.Trajectory` and
        :class:`~tolokaforge.runner.models.TaskDescription` on every
        implementation. Cleared by :meth:`cleanup_trial` so per-run
        memory stays bounded.

        Backends that do not implement snapshot mode may treat this as a
        no-op (recording the call for tests to inspect) — the orchestrator
        gates the ``build_grade_bundle`` path on the ``snapshot`` config
        so ``remember_trial_inputs`` on a snapshot-incapable backend is
        never followed by a ``build_grade_bundle`` call.
        """
        ...

    def build_grade_bundle(
        self,
        trial_id: str,
        *,
        out_dir: Path,
    ) -> GradeBundleManifest:
        """Produce a grade bundle for ``trial_id`` in ``out_dir``.

        Opt-in. Called by the orchestrator's trial-end producer seam when
        ``RunConfig.grader.snapshot.enabled`` is ``True`` — and only then.

        Backends that support snapshot bundle production materialise the
        bundle by composing reads over the runner's ``SubstrateService``
        (already dialled for live-callback grading) plus the trajectory
        and :class:`~tolokaforge.runner.models.TaskDescription` the caller
        stashed via :meth:`remember_trial_inputs`, then calling
        :func:`~tolokaforge.core.grading.bundle.serialize_grade_bundle`.

        Backends that do not support it raise
        :class:`NotImplementedError`. The orchestrator refuses
        ``grader.snapshot.enabled=true`` at run-start against a backend
        that raises here (the probe runs once during
        :meth:`Orchestrator._validate_snapshot_mode_compatibility`), so
        this method is never called against a backend that opts out.

        ``out_dir`` MUST be empty on entry; the bundle producer's own
        :class:`FileExistsError` fence surfaces otherwise. The caller
        owns lifecycle — the orchestrator writes to a per-trial
        :class:`tempfile.TemporaryDirectory` and cleans up after
        ``store.put``.

        Returns the parsed
        :class:`~tolokaforge.core.grading.bundle.GradeBundleManifest` the
        producer wrote. The bundle's canonical name is
        ``manifest_digest((out_dir / "manifest.json").read_bytes())``.
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

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        """Capture per-service logs for ``handle``'s stack; return a
        ``{service_name: bytes_written}`` map.

        Writes ``docker compose logs`` output to the handle's trial
        ``services/`` dir when the backend has a live per-trial stack and
        (``capture_worthy`` or the backend's on-success policy), then returns
        the byte map for the services that produced output; returns ``{}``
        otherwise. ``capture_worthy`` is the executor's verdict that this
        trial's outcome warrants diagnostics (execution failure or a
        completed-but-red grade). Never raises — best-effort diagnostics
        captured *because* the outcome is already decided. Backends without a
        trial-scoped stack (the shared-stack backend) are documented no-ops
        returning ``{}``.
        """
        ...

    def get_infrastructure_snapshot(self, handle: EnvHandle) -> list[ContainerSnapshot]:
        """Return a per-trial container-state snapshot for ``handle``.

        Called by the orchestrator right after :meth:`await_ready` so the
        display can render an infrastructure sub-panel for the focused
        trial. Backends that do not materialise per-trial substrate
        (the built-in ``SharedStackRuntimeBackend`` in built-in-stack
        mode) return an empty list — the services widget already covers
        that path.

        Must not raise: the orchestrator invokes this on the hot path
        for every trial and a raise would corrupt the runner loop.
        """
        ...


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
    capture_service_logs_calls: list[tuple[str, bool]] = field(default_factory=list)
    remembered_trial_inputs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _InMemoryEnvHandle:
    """Concrete handle type returned by :class:`InMemoryRuntimeBackend`.

    Satisfies the :class:`EnvHandle` Protocol structurally via the
    ``trial_id`` attribute.
    """

    trial_id: str


class InMemoryRuntimeBackend:
    """Recording test-fixture — records every :class:`RuntimeBackend`
    method call on :attr:`call_log` and never talks to Docker or gRPC.
    Production code never constructs this class; contract tests and
    orchestrator-level tests inject it to exercise the Protocol surface
    without a real backend.

    Named ``InMemory`` for consistency with the codebase's
    ``InMemory{ProtocolName}`` test-fixture prefix (see also
    :class:`~tolokaforge.core.trial_artifact_writer.InMemoryArtifactWriter`,
    :class:`~tolokaforge.core.conductor.InMemoryConductor`). "InMemory"
    here reads as "no external state" rather than literal
    in-memory-data storage — the class records call history in a dict
    for tests to assert against.

    Non-gRPC by design. Records lifecycle, cleanup, and provisioning
    calls on :attr:`call_log`. The per-trial RPC methods raise
    :class:`NotImplementedError` — tests that exercise the RPC surface
    must use :class:`SharedStackRuntimeBackend` or mock the methods on
    the backend instance.

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

    isolation_mode: IsolationMode = IsolationMode.SHARED_STACK
    """Test fixture keeps the shared-stack posture by default so tests that
    inject it against tasks with no isolation requirement continue to work.
    Tests exercising the per-trial short-circuit at the top of
    :meth:`Orchestrator._verify_isolation_compatibility` can override the
    attribute on the instance."""

    advertised_capabilities: frozenset[str] = frozenset()
    """Test fixture advertises nothing by default; tests exercising the
    admission gate override on the instance."""

    def __init__(
        self,
        *,
        fail_provision_after_service: str | None = None,
        await_ready_times_out: bool = False,
        isolation_mode: IsolationMode = IsolationMode.SHARED_STACK,
        advertised_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self.call_log = RuntimeBackendCallLog()
        self._fail_provision_after_service = fail_provision_after_service
        self._await_ready_times_out = await_ready_times_out
        self.isolation_mode = isolation_mode
        self.advertised_capabilities = advertised_capabilities

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
            service_names = set(manifest.load_compose()["services"])
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

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        self.call_log.capture_service_logs_calls.append((handle.trial_id, capture_worthy))
        return {}

    def get_infrastructure_snapshot(self, handle: EnvHandle) -> list[ContainerSnapshot]:
        """Return a synthetic single-container snapshot for tests.

        The in-memory backend has no real substrate, so the snapshot is
        deterministic: one ``ContainerSnapshot`` per trial identifying
        the in-memory runner. Tests that exercise the panel's infra
        rendering can rely on this shape without standing up docker.
        """
        return [
            ContainerSnapshot(
                name=f"in-memory-runner-{handle.trial_id}",
                service="runner",
                state="running",
                health="healthy",
                ports={50051: 50051},
            )
        ]

    # ---- Per-trial RPC operations (ADR-0013) ----
    # The in-memory backend has no runner service to talk to; every RPC
    # method raises with a pointer to the SharedStackRuntimeBackend alternative.
    # Tests that exercise the RPC surface must use ``SharedStackRuntimeBackend`` or
    # mock the methods on this instance directly.
    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "InMemoryRuntimeBackend.register_trial is not implemented. "
            "Tests that exercise the runner RPC surface must use "
            "SharedStackRuntimeBackend or mock the method on the backend instance."
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> Any:
        raise NotImplementedError(
            "InMemoryRuntimeBackend.execute_tool is not implemented. "
            "Tests that exercise the runner RPC surface must use "
            "SharedStackRuntimeBackend or mock the method on the backend instance."
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "InMemoryRuntimeBackend.grade_trial is not implemented. "
            "Tests that exercise the runner RPC surface must use "
            "SharedStackRuntimeBackend or mock the method on the backend instance."
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "InMemoryRuntimeBackend.get_state is not implemented. "
            "Tests that exercise the runner RPC surface must use "
            "SharedStackRuntimeBackend or mock the method on the backend instance."
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        raise NotImplementedError(
            "InMemoryRuntimeBackend.reset_trial is not implemented. "
            "Tests that exercise the runner RPC surface must use "
            "SharedStackRuntimeBackend or mock the method on the backend instance."
        )

    def remember_trial_inputs(
        self,
        trial_id: str,
        trajectory: Trajectory,
        task_description: TaskDescription,
    ) -> None:
        """Record the call on :attr:`call_log`; no state is kept.

        The in-memory backend does not implement
        :meth:`build_grade_bundle`, so the stashed inputs are never
        consulted. Tests can assert on
        :attr:`RuntimeBackendCallLog.remembered_trial_inputs` to prove
        the orchestrator drove the seam.
        """
        del trajectory, task_description
        self.call_log.remembered_trial_inputs.append(trial_id)

    def build_grade_bundle(
        self,
        trial_id: str,
        *,
        out_dir: Path,
    ) -> GradeBundleManifest:
        del trial_id, out_dir
        raise NotImplementedError(
            "InMemoryRuntimeBackend.build_grade_bundle is not implemented. "
            "Tests that exercise the snapshot bundle producer surface must use "
            "SharedStackRuntimeBackend or PerTrialRuntimeBackend, or mock the "
            "method on the backend instance."
        )


def in_memory_runtime_backend_factory(
    ctx: RuntimeBackendBuildContext,
) -> InMemoryRuntimeBackend:
    """Build an :class:`InMemoryRuntimeBackend` from a build context.

    The recording fixture has no substrate to seed or capture, so it reads
    none of the context fields and takes the default ``isolation_mode``.
    """
    return InMemoryRuntimeBackend()
