"""Per-trial lifecycle event Protocol emitted by the runner into a display.

Lives in ``tolokaforge.core`` (not ``tolokaforge.dx``) so the orchestrator,
conductor, and trial runner import the Protocol without dragging the
terminal front-end + Rich dependency graph into engine-side code paths
(worker container, gRPC runner, cloud-runtime trial-plane).

The Protocol has a no-op default (:data:`_NULL_EVENTS`), so callers that
never build a display can still thread ``events`` through without
conditional branches. The 12 methods bracket the full trial lifecycle:
run-level (``run_started`` / ``run_finished`` / ``phase_changed``),
per-trial boundary events (``trial_started`` / ``trial_provisioned`` /
``trial_progress`` / ``judgment_scored`` / ``trial_completed`` /
``trial_failed``), and the in-flight LLM-call trio
(``llm_call_started`` / ``llm_call_finished`` / ``llm_retry_scheduled``)
that surfaces provider activity *during* a generation so a display can
show progress while a slow attempt or an outer-retry backoff is in
flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, runtime_checkable

LLMCallRole = Literal["agent", "user", "judge", "grader"]

ComponentKind = Literal[
    "docker.service",
    "grpc.client",
    "container",
    "k8s.pod",
    "process",
    "remote",
]
"""What kind of runtime entity a component is.

The kind is transport-agnostic: ``docker.service`` and ``k8s.pod`` are
peers, not one nested inside the other. The panel groups by kind and by
:attr:`ComponentSnapshot.owner` — never by transport-specific fields.
Extend the literal set when a new reporter shape is added; the panel's
renderer falls back to the raw string for unknown kinds.
"""

ComponentPhase = Literal[
    "pending",
    "starting",
    "healthy",
    "degraded",
    "unhealthy",
    "stopped",
    "dead",
]
"""Lifecycle phase of a monitored component.

Transitions are event-driven; the reporter fires
:meth:`RunDisplayEvents.component_status_changed` at every phase edge.
``degraded`` / ``unhealthy`` / ``dead`` auto-expand the component's log
tail beneath its row in the panel; ``healthy`` collapses the tail again.
"""


class ComponentSnapshot(TypedDict):
    """Wire shape for a single component's status.

    Fired via :meth:`RunDisplayEvents.component_registered` on first sight
    and :meth:`RunDisplayEvents.component_status_changed` on every
    subsequent transition. The panel keys on :attr:`id` — repeated fires
    with the same id update the same row in place, so per-attempt polling
    updates the ``detail`` field without scrolling the log stream.

    - ``id`` — stable identity, conventionally
      ``"{namespace}/{kind}/{instance}"``. Build via
      :func:`build_component_id`.
    - ``kind`` — the reporter's transport-agnostic label
      (:data:`ComponentKind`).
    - ``phase`` — current lifecycle phase (:data:`ComponentPhase`).
    - ``detail`` — one-line adornment for the current phase
      (``"attempt 7, elapsed=6.2s/30s"`` while probing;
      ``"port 50051 reachable"`` on healthy).
    - ``owner`` — grouping key for the widget (``"engine"``,
      ``"trial/tool_use/0"``, ``"worker/3"``). ``None`` means "no group".
    """

    id: str
    kind: ComponentKind
    phase: ComponentPhase
    detail: str | None
    owner: str | None


def build_component_id(namespace: str, kind: ComponentKind, instance: str) -> str:
    """Construct a canonical component id: ``"{namespace}/{kind}/{instance}"``.

    Every reporter should route through this helper so the panel's id-key
    invariant (one row per component) stays consistent across reporters.
    """
    return f"{namespace}/{kind}/{instance}"


class ServiceSnapshot(TypedDict):
    """One row of the panel's service-status widget.

    Populated by the orchestrator from :meth:`EngineStack.get_status` (or
    an equivalent for task-declared stacks) and passed via
    :meth:`RunDisplayEvents.phase_changed`'s ``services`` argument.

    - ``name`` — service name from the compose file.
    - ``status`` — container lifecycle: ``"created"`` / ``"starting"`` /
      ``"running"`` / ``"exited"`` / ``"not_created"`` / etc.
    - ``ports`` — mapping of container-port → host-port.
    - ``role`` — ``"engine"`` (built-in ``EngineStack`` service) or
      ``"task"`` (task-declared compose service).
    """

    name: str
    status: str
    ports: dict[int, int]
    role: str


class ContainerSnapshot(TypedDict):
    """One row of the focused-trial infrastructure sub-panel.

    Populated by the runtime backend at provision-complete time and passed
    via :meth:`RunDisplayEvents.trial_provisioned`'s ``containers``
    argument.

    - ``name`` — container's Docker name.
    - ``service`` — compose service that owns the container.
    - ``state`` — Docker state: ``"running"`` / ``"exited"`` / etc.
    - ``health`` — health-probe result (``"healthy"`` / ``"unhealthy"`` /
      ``"starting"``), or ``None`` when the compose service declared no
      health probe.
    - ``ports`` — mapping of container-port → host-port for containers
      that publish any.
    """

    name: str
    service: str
    state: str
    health: str | None
    ports: dict[int, int]


@runtime_checkable
class RunDisplayEvents(Protocol):
    """Per-trial lifecycle events the runner emits into a display.

    Every method is kwarg-only so a future field addition does not break
    positional callers. Implementations must not raise — a raise would
    corrupt the runner loop. :data:`_NULL_EVENTS` is the default sink.
    """

    def run_started(self, *, total_trials: int, initial_completed: int) -> None:
        """Fired once when the orchestrator has primed its trial queue."""

    def trial_started(
        self,
        *,
        trial_id: str,
        task_id: str,
        trial_index: int,
        total_index: int,
        agent_model: str | None = None,
        user_model: str | None = None,
    ) -> None:
        """Fired when a worker leases a trial and enters provisioning.

        ``trial_index`` is the task-local trial number (0..repeats-1);
        ``total_index`` is the run-wide trial number (0..total_trials-1)
        so the display can render a global ``[N/M]`` prefix.
        ``agent_model`` / ``user_model`` carry the ``provider/name``
        identity of the two in-process LLM roles when the orchestrator
        knows them, so the display can label per-role call events
        without a lookup.
        """

    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        """Fired after each LLM generation inside the trial's agent loop."""

    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None:
        """Fired on a terminal, non-retryable success."""

    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        """Fired on terminal failure (retryable-exhausted or hard raise)."""

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        """Fired after the rubric judge populates ``trajectory.grade``."""

    def run_finished(self, *, output_dir: Path) -> None:
        """Fired at the very end of ``Orchestrator.run()``."""

    def phase_changed(
        self,
        *,
        phase: str,
        detail: str | None = None,
        services: list[ServiceSnapshot] | None = None,
    ) -> None:
        """Fired at pipeline milestones BEFORE and after :meth:`run_started`.

        Purpose: give the panel a chance to render "Starting services…"
        during the 10-30s Docker startup window that used to display
        ``0/0 · 0 running``. ``phase`` values are documented literals:

        - ``"loading_tasks"`` — before adapter loads task manifests.
        - ``"starting_services"`` — before ``service_stack.start_all()``.
        - ``"services_ready"`` — after the service health check passes.
        - ``"connecting_runtime"`` — before ``runtime_backend.connect()``.
        - ``"priming_queue"`` — before the trial pool starts leasing.

        ``detail`` is an optional one-line adornment (e.g. container count).
        ``services`` carries a structured snapshot of the built-in
        ``EngineStack`` at the transition — declared (``status="created"``)
        on ``starting_services``, live snapshot on ``services_ready``.
        Implementations must not raise.
        """

    def trial_provisioned(
        self,
        *,
        trial_id: str,
        containers: list[ContainerSnapshot],
        endpoints: dict[str, str],
    ) -> None:
        """Fired after ``runtime_backend.await_ready(handle)`` returns.

        Carries the per-trial infrastructure state so the focused-trial
        pane can render an "Infrastructure" sub-panel. ``containers`` is
        the list produced by
        :meth:`RuntimeBackend.get_infrastructure_snapshot`;
        ``endpoints`` maps service name → resolved URL for services the
        agent talks to (runner / db / rag / …). Empty ``containers`` is
        legal when the backend is the built-in ``EngineStack`` — the
        services widget already covers that path.
        """

    def llm_call_started(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
    ) -> None:
        """Fired immediately before an in-process LLM attempt hits the wire.

        ``attempt`` is the 1-indexed outer-retry attempt number of the
        current ``LLMClient.generate`` call — attempt 1 for the initial
        try, attempt >1 after an ``llm_retry_scheduled`` backoff.
        Exactly one ``llm_call_finished`` follows each start for the
        same ``(trial_id, role, provider, model, attempt)`` tuple.
        """

    def llm_call_finished(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
        duration_s: float,
        error: str | None,
    ) -> None:
        """Fired when an in-process LLM attempt returns or raises.

        ``duration_s`` is monotonic wall-clock for the attempt (transport
        timeouts, key rotation, and synthetic-envelope detection are all
        inside the same attempt). ``error`` is ``None`` on success or
        ``str(exc)`` when the attempt raised — a failed attempt that is
        about to be retried surfaces here first, then via
        ``llm_retry_scheduled``.
        """

    def llm_retry_scheduled(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
        next_attempt_in_s: float,
        reason: str,
    ) -> None:
        """Fired inside the outer-retry ``before_sleep`` hook.

        ``attempt`` is the attempt that just failed; the next attempt
        starts after ``next_attempt_in_s`` seconds of tenacity backoff.
        ``reason`` is ``str(exc)`` for the exception that triggered the
        retry, so the display can show why a call is stalling. Never
        fires after the final attempt — a terminal failure surfaces via
        ``llm_call_finished`` with ``error`` set, followed by whatever
        the caller does with the reraised exception.
        """

    def component_registered(self, *, snapshot: ComponentSnapshot) -> None:
        """Announce a new component the display should start tracking.

        First-sight fire for a component id. Subsequent updates reuse
        :meth:`component_status_changed` — the id keys the row. Idempotent
        on repeat: implementations MUST tolerate multiple registrations
        of the same id (last snapshot wins).
        """

    def component_status_changed(self, *, snapshot: ComponentSnapshot) -> None:
        """Update a component's phase / detail without adding a new row.

        Fired on every lifecycle transition and every ``detail``-only
        refresh (e.g. per-probe-attempt updates). The panel keys on
        ``snapshot["id"]``; unknown ids are treated as an implicit
        register.
        """

    def component_log_appended(
        self,
        *,
        component_id: str,
        level: str,
        message: str,
        ts: float,
    ) -> None:
        """Attach a log line to a specific component's tail buffer.

        Kept distinct from the panel's general log ring so component
        chatter never scrolls above the panel. The tail is rendered only
        while the component is in ``degraded`` / ``unhealthy`` / ``dead``
        — healthy components stay one compact row. ``ts`` is monotonic
        wall-clock (``time.time()``); ``level`` matches Python's
        ``logging`` level names (``"INFO"``, ``"WARNING"``, ``"ERROR"``).
        """

    def component_unregistered(self, *, component_id: str) -> None:
        """Drop a component from the display's tracking set.

        Optional — long-lived components can stay registered for the
        life of the run. Called at teardown so the widget doesn't carry
        stopped-and-cleaned-up rows forward. The tail buffer is dropped
        alongside the row.
        """


@dataclass
class RateLimitProbeStats:
    """Per-trial 429 accounting accumulated by the rate-limit probe controller.

    Mutable and shared by every role's :class:`LLMCallObservation` within one
    trial: the ``LLMClient`` is shared across concurrent trials, so per-trial
    state has to ride the call rather than live on the client. The trial runner
    owns the instance and copies the totals onto ``Metrics`` at trial end.

    Stays all-zero unless rate-limit probe mode is enabled.
    """

    retries: int = 0
    """429 retries the probe absorbed across every LLM call in the trial."""

    wait_s: float = 0.0
    """Summed fixed-interval sleep the probe scheduled for those retries."""

    first_ts: float | None = None
    """``time.time()`` of the first 429 seen in this trial."""

    last_ts: float | None = None
    """``time.time()`` of the most recent 429 seen in this trial."""

    def record_retry(self, *, wait_s: float, ts: float) -> None:
        self.retries += 1
        self.wait_s += wait_s
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts


@dataclass(frozen=True)
class LLMCallObservation:
    """Per-call context threaded from a trial into ``LLMClient.generate``.

    Bundles the live sink reference with the identity of the call site
    (``trial_id`` + ``role``) so the client can fire the LLM-call trio
    without knowing anything about how the sink is routed. Lives with
    the seam because it references both :class:`RunDisplayEvents` and
    :data:`LLMCallRole`; the LLM client, agent loop, and user simulator
    already import from this module, preserving a one-way dependency
    graph.

    ``probe_stats`` is the trial's shared :class:`RateLimitProbeStats`
    accumulator when rate-limit probe mode is on, ``None`` otherwise.
    """

    events: RunDisplayEvents
    trial_id: str
    role: LLMCallRole
    probe_stats: RateLimitProbeStats | None = None


class _NullRunDisplayEvents:
    """No-op :class:`RunDisplayEvents`.

    Wired as the default on ``OrchestratorDeps.events`` so the orchestrator,
    conductor, and runner never branch on ``events is None`` — they just
    call every method.
    """

    def run_started(self, **_: object) -> None: ...
    def trial_started(self, **_: object) -> None: ...
    def trial_progress(self, **_: object) -> None: ...
    def trial_completed(self, **_: object) -> None: ...
    def trial_failed(self, **_: object) -> None: ...
    def judgment_scored(self, **_: object) -> None: ...
    def run_finished(self, **_: object) -> None: ...
    def phase_changed(self, **_: object) -> None: ...
    def trial_provisioned(self, **_: object) -> None: ...
    def llm_call_started(self, **_: object) -> None: ...
    def llm_call_finished(self, **_: object) -> None: ...
    def llm_retry_scheduled(self, **_: object) -> None: ...
    def component_registered(self, **_: object) -> None: ...
    def component_status_changed(self, **_: object) -> None: ...
    def component_log_appended(self, **_: object) -> None: ...
    def component_unregistered(self, **_: object) -> None: ...


_NULL_EVENTS: RunDisplayEvents = _NullRunDisplayEvents()


__all__ = [
    "ComponentKind",
    "ComponentPhase",
    "ComponentSnapshot",
    "ContainerSnapshot",
    "LLMCallObservation",
    "LLMCallRole",
    "RateLimitProbeStats",
    "RunDisplayEvents",
    "ServiceSnapshot",
    "_NULL_EVENTS",
    "_NullRunDisplayEvents",
    "build_component_id",
]
