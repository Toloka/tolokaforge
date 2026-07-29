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

from dataclasses import dataclass, field
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


DEFAULT_PROBE_BUCKET_WIDTH_S = 30
"""Default width of a :class:`RateLimitProbeStats` throughput bucket, seconds.

Integer seconds on purpose — see
:meth:`RateLimitProbeStats.bucket_start` for why the boundary must be an
exact whole second.
"""

DEFAULT_PROBE_MAX_BUCKETS = 4096
"""Default cap on the number of throughput buckets one trial may open.

At the 30 s default that is ~34 h of a two-role trial, far past any real
episode budget. See :meth:`RateLimitProbeStats.bucket_start` for the drop
policy that applies once the cap is reached.
"""


@dataclass
class RateLimitProbeCounters:
    """One accounting bucket: the 429 side and the SUCCESS side of throughput.

    Both censuses live in one counter because the pair is what makes a
    measurement interpretable. The 429 census alone is schedule-dependent and,
    for some providers, silent: a model with no provider pin produced zero 429s
    across four probe runs up to 33k input tokens/s while inflating per-call
    latency 41 %. Goodput (``successes``) and served concurrency
    (``success_duration_s``) catch that; ``retries`` does not.

    ``prompt_tokens`` / ``completion_tokens`` are carried because tokens/s — not
    calls/s — is the quantity that sums across run legs: measured token profiles
    differ ~4x between domains (369,857 vs 89,984 input tokens per trial), so
    calls/s from two different domains are not the same unit.

    Used both for the per-``(role, model)`` cumulative rows and for the
    fixed-width absolute-time buckets, which need exactly the same columns.
    """

    retries: int = 0
    """429 retries the probe absorbed in this bucket."""

    wait_s: float = 0.0
    """Summed retry sleep the probe scheduled for those retries."""

    first_ts: float | None = None
    """``time.time()`` of the first 429 in this bucket.

    The 429 window specifically, not the first event of any kind — the
    success side deliberately does not move it, so
    ``Metrics.rate_limit_first_ts`` keeps meaning "when did this trial first
    get throttled".
    """

    last_ts: float | None = None
    """``time.time()`` of the most recent 429 in this bucket."""

    successes: int = 0
    """LLM calls that returned a result in this bucket.

    One per successful ``LLMClient.generate`` return, counted at completion.
    """

    success_duration_s: float = 0.0
    """Summed client-observed duration of those successful calls.

    Divided by the bucket's wall width this is the Little's-law in-flight
    concurrency actually served — computed on successes only, which is what
    makes it schedule-independent.
    """

    prompt_tokens: int = 0
    """Prompt tokens the provider reported for those successful calls."""

    completion_tokens: int = 0
    """Completion tokens the provider reported for those successful calls."""

    def record_retry(self, *, wait_s: float, ts: float) -> None:
        self.retries += 1
        self.wait_s += wait_s
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    def record_success(
        self, *, duration_s: float, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Record one successful call. Takes no ``ts``.

        The bucket's own key carries the time, and the ``first_ts`` /
        ``last_ts`` pair is reserved for the 429 window (see ``first_ts``).
        """
        self.successes += 1
        self.success_duration_s += duration_s
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens


@dataclass
class RateLimitProbeStats:
    """Per-trial throughput + 429 accounting accumulated by the probe.

    Mutable and shared by every role's :class:`LLMCallObservation` within one
    trial: the ``LLMClient`` is shared across concurrent trials, so per-trial
    state has to ride the call rather than live on the client. The trial runner
    owns the instance and copies the totals onto ``Metrics`` at trial end.

    Accounting is keyed by ``(role, model slug)`` because a trial's roles are
    different models — in an arena config the agent is the model under test and
    the user simulator is a fixed, unrelated one — so a single flat counter
    would blend a measured model's numbers with an unmeasured one's. The flat
    fields are kept as the sum across buckets for consumers that only want the
    trial total. ``Metrics.usage.calls`` cannot substitute: it holds agent calls
    only and carries no role, so per-model goodput is not derivable from it.

    **Two censuses, one recorder.** ``retries`` / ``wait_s`` are the FAILURE
    side; ``successes`` / ``success_duration_s`` / the token counts are the
    SUCCESS side. Both are needed because the 429 census is schedule-dependent
    and, for some providers, silent — see :class:`RateLimitProbeCounters`.

    **Three views of the same events.** The flat fields are the trial total,
    ``by_role_model`` is the cumulative per-model breakdown, and ``by_bucket``
    is the same breakdown resolved into fixed-width absolute-time windows.
    Cumulative totals alone are not sufficient: at a CONSTANT 70-way offered
    concurrency a measured probe's goodput fell from 1.70 to 0.43 successful
    calls/s over ~12 minutes while the rejection rate climbed 66 % -> 86 %. A
    single cumulative counter reports one blended average and hides that
    entirely.

    Not synchronised: one trial's probe-capable calls are strictly sequential
    (``ToolCallingLoop._run_turn`` runs the agent's ``generate``, then the
    simulator's ``reply``), so there is never more than one in flight per
    instance. Concurrency *across* trials is safe because each trial owns its
    own instance.

    Stays all-zero unless rate-limit probe mode is enabled.
    """

    bucket_width_s: int = DEFAULT_PROBE_BUCKET_WIDTH_S
    """Width of one ``by_bucket`` window. See :meth:`bucket_start`."""

    max_buckets: int = DEFAULT_PROBE_MAX_BUCKETS
    """Cap on ``len(by_bucket)``. See :meth:`bucket_start` for the drop policy."""

    retries: int = 0
    """429 retries the probe absorbed across every LLM call in the trial."""

    wait_s: float = 0.0
    """Summed retry sleep the probe scheduled for those retries."""

    first_ts: float | None = None
    """``time.time()`` of the first 429 seen in this trial."""

    last_ts: float | None = None
    """``time.time()`` of the most recent 429 seen in this trial."""

    successes: int = 0
    """Successful LLM calls across every role in the trial."""

    success_duration_s: float = 0.0
    """Summed client-observed duration of those successful calls."""

    prompt_tokens: int = 0
    """Prompt tokens the provider reported for those successful calls."""

    completion_tokens: int = 0
    """Completion tokens the provider reported for those successful calls."""

    by_role_model: dict[tuple[LLMCallRole, str], RateLimitProbeCounters] = field(
        default_factory=dict
    )
    """Cumulative per-``(role, model slug)`` breakdown."""

    by_bucket: dict[tuple[LLMCallRole, str, int], RateLimitProbeCounters] = field(
        default_factory=dict
    )
    """The same breakdown per ``(role, model slug, absolute bucket start)``."""

    dropped_buckets: int = 0
    """Distinct windows that could not be opened because of :attr:`max_buckets`.

    Never silent: a non-zero value means ``by_bucket`` is a truncated prefix of
    the trial and the flat / ``by_role_model`` totals are the only complete
    record. See :meth:`bucket_start`.
    """

    def __post_init__(self) -> None:
        # Bookkeeping for ``dropped_buckets``: the last refused window start per
        # ``(role, model)``, so consecutive refusals inside one window count
        # once. Bounded by the number of roles (two in practice), and
        # deliberately not a dataclass field — it is not part of the value.
        self._dropped_cursor: dict[tuple[LLMCallRole, str], int] = {}

    def bucket_start(self, ts: float) -> int:
        """The absolute-time window *ts* falls in, as an epoch second.

        **The boundary is derived from the Unix epoch, never from run start.**
        That is the entire reason ``by_bucket`` exists. The intended
        measurement runs all seven domain legs SIMULTANEOUSLY, each in its own
        runner process (one process serves one domain), and sums per-leg
        throughput into a global number. Summing is only valid if the legs'
        windows line up, and a run-start-relative boundary would give every
        leg — and every trial inside a leg — its own grid, making the sum
        meaningless. Anchoring on the epoch means two processes on two machines
        with synchronised clocks emit *identical* bucket starts for the same
        wall-clock instant, so a consumer joins on this value directly.

        :attr:`bucket_width_s` is whole seconds for the same reason: an integer
        width keeps every boundary an exact integer epoch, so the serialised
        timestamps match across legs with no float-representation drift.

        **Drop policy.** A recording that lands in an existing window is always
        counted. Once ``len(by_bucket)`` reaches :attr:`max_buckets`, opening a
        *new* window is refused and :attr:`dropped_buckets` counts it; the
        recording still lands in the flat and ``by_role_model`` totals, so
        nothing is lost from those. Refusing new windows rather than evicting
        old ones keeps the retained series a contiguous prefix in absolute
        time: a series with a hole in it would let a cross-leg window-by-window
        sum silently undercount, which is worse than a short series.
        """
        return int(ts // self.bucket_width_s) * self.bucket_width_s

    def record_retry(self, *, role: LLMCallRole, model: str, wait_s: float, ts: float) -> None:
        """Record one absorbed 429 for *role* calling *model*.

        ``role`` and ``model`` are required: every recording site already knows
        both (the ``before_sleep`` hook has the call's observation and the
        client's model name), and defaulting them would silently create an
        unattributable bucket.
        """
        self.retries += 1
        self.wait_s += wait_s
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts
        self._role_model(role, model).record_retry(wait_s=wait_s, ts=ts)
        bucket = self._bucket(role, model, ts)
        if bucket is not None:
            bucket.record_retry(wait_s=wait_s, ts=ts)

    def record_success(
        self,
        *,
        role: LLMCallRole,
        model: str,
        duration_s: float,
        prompt_tokens: int,
        completion_tokens: int,
        ts: float,
    ) -> None:
        """Record one successful call by *role* against *model*.

        *ts* is the call's completion instant, so a call is counted in the
        window it finished in — goodput is completions per window.

        ``role`` and ``model`` are required for the same reason as in
        :meth:`record_retry`.
        """
        self.successes += 1
        self.success_duration_s += duration_s
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self._role_model(role, model).record_success(
            duration_s=duration_s,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        bucket = self._bucket(role, model, ts)
        if bucket is not None:
            bucket.record_success(
                duration_s=duration_s,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

    def _role_model(self, role: LLMCallRole, model: str) -> RateLimitProbeCounters:
        counters = self.by_role_model.get((role, model))
        if counters is None:
            counters = RateLimitProbeCounters()
            self.by_role_model[(role, model)] = counters
        return counters

    def _bucket(self, role: LLMCallRole, model: str, ts: float) -> RateLimitProbeCounters | None:
        """The window counters for this event, or ``None`` when capped."""
        start = self.bucket_start(ts)
        key = (role, model, start)
        counters = self.by_bucket.get(key)
        if counters is not None:
            return counters
        if len(self.by_bucket) >= self.max_buckets:
            self._note_dropped(role, model, start)
            return None
        counters = RateLimitProbeCounters()
        self.by_bucket[key] = counters
        return counters

    def _note_dropped(self, role: LLMCallRole, model: str, start: int) -> None:
        if self._dropped_cursor.get((role, model)) == start:
            return
        self._dropped_cursor[(role, model)] = start
        self.dropped_buckets += 1


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
    "DEFAULT_PROBE_BUCKET_WIDTH_S",
    "DEFAULT_PROBE_MAX_BUCKETS",
    "ComponentKind",
    "ComponentPhase",
    "ComponentSnapshot",
    "ContainerSnapshot",
    "LLMCallObservation",
    "LLMCallRole",
    "RateLimitProbeCounters",
    "RateLimitProbeStats",
    "RunDisplayEvents",
    "ServiceSnapshot",
    "_NULL_EVENTS",
    "_NullRunDisplayEvents",
    "build_component_id",
]
