"""Per-trial lifecycle event Protocol emitted by the runner into a display.

Lives in ``tolokaforge.core`` (not ``tolokaforge.dx``) so the orchestrator,
conductor, and trial runner import the Protocol without dragging the
terminal front-end + Rich dependency graph into engine-side code paths
(worker container, gRPC runner, cloud-runtime trial-plane).

The reference Rich-bound consumer is :class:`tolokaforge.dx.live_panel.LiveRunDisplay`;
future front-ends (see ADR-0019) implement the same Protocol. The
Protocol has a no-op default (:data:`_NULL_EVENTS`), so callers that
never build a display can still thread ``events`` through without
conditional branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable


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
    ) -> None:
        """Fired when a worker leases a trial and enters provisioning.

        ``trial_index`` is the task-local trial number (0..repeats-1);
        ``total_index`` is the run-wide trial number (0..total_trials-1)
        so the display can render a global ``[N/M]`` prefix.
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


_NULL_EVENTS: RunDisplayEvents = _NullRunDisplayEvents()


__all__ = [
    "ContainerSnapshot",
    "RunDisplayEvents",
    "ServiceSnapshot",
    "_NULL_EVENTS",
    "_NullRunDisplayEvents",
]
