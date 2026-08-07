"""RunConfig + orchestrator / compute / storage / observability sub-configs.

Everything a run declares at the ``run_config.yaml`` level — the model
map, orchestrator knobs, task-pack evaluation surface, compute /
storage / observability blocks, and the ``run_defaults`` inheritance
base — plus the rate-limit probe budget invariant.
"""

import warnings
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from tolokaforge.core.deprecations import coerce_task_packs_alias
from tolokaforge.core.models.model_config import ModelConfig

# The probe's bucketing defaults live next to the accumulator that applies them
# (``run_display_events`` has no ``core.models`` dependency, so this direction
# is the only one that does not create a cycle).
from tolokaforge.core.run_display_events import (
    DEFAULT_PROBE_BUCKET_WIDTH_S,
    DEFAULT_PROBE_MAX_BUCKETS,
)
from tolokaforge.docker.config import DockerConfig

__all__ = [
    "ComputeConfig",
    "DOCKER_RUNTIME_ALIAS_TARGET",
    "DockerConfig",
    "EngineConfig",
    "EvaluationConfig",
    "HarnessAdapterConfig",
    "LEGACY_DOCKER_RUNTIME_ALIAS",
    "LocalDockerComputeConfig",
    "LocalStorageConfig",
    "LoggingConfig",
    "MetricsConfig",
    "ObservabilityConfig",
    "OrchestratorConfig",
    "QueueStorageConfig",
    "RATE_LIMIT_PROBE_ATTEMPT_CEILING_S",
    "RATE_LIMIT_PROBE_MIN_EPISODE_S",
    "RateLimitProbeConfig",
    "RunConfig",
    "RunDefaults",
    "S3StorageConfig",
    "StorageBackend",
    "StorageConfig",
    "StuckHeuristics",
    "TimeoutConfig",
    "TracingConfig",
    "TypeSenseConfig",
    "USER_REPLY_MAX_ATTEMPTS",
    "validate_rate_limit_probe_budget",
]


class TimeoutConfig(BaseModel):
    """Timeout configuration"""

    model_config = {"extra": "ignore"}

    turn_s: int = 60
    episode_s: int = 1800


RATE_LIMIT_PROBE_MIN_EPISODE_S = 3600
"""Smallest run-level episode budget a rate-limit probe run may declare.

A probe absorbs 429s by sleeping, and episode wall-time counts that sleep, so
a probe on the default 1800 s budget dies on the episode timeout instead of
measuring the provider. One hour is the floor below which the mode cannot do
its job."""


RATE_LIMIT_PROBE_ATTEMPT_CEILING_S = 737.0
"""Nominal worst-case wall time of ONE upstream attempt, seconds.

``(DEFAULT_API_TIMEOUT_RETRIES + 1) x DEFAULT_API_CALL_TIMEOUT_S`` plus the inner
``wait_exponential(multiplier=1, min=1, max=5)`` backoff between those six
attempts (1 + 2 + 4 + 5 + 5 s) — i.e. ``6 x 120 + 17`` — exactly as
``LLMClient._call_completion_with_timeout_retry`` documents. Restated here
instead of imported because ``core/llm/client.py`` imports this module;
``tests/unit/llm/test_rate_limit_probe_retry.py`` locks the two together so
drift fails CI.

Nominal, not hard, and deliberately so: ``DEFAULT_API_CALL_WALL_TIMEOUT_S`` is
``None`` by default and the per-call ``timeout`` is a per-read httpx timeout that
a slowly streamed response keeps resetting, so a single attempt has no true
ceiling until a preset sets ``api_call_wall_timeout_s``. A preset raising
``api_call_timeout_s`` above the default likewise raises the real ceiling. Both
are pre-existing engine properties that a probe-off run shares;
:func:`validate_rate_limit_probe_budget` uses the nominal value so that the
probe's *own* knobs are bounded against a stated reference rather than against
zero."""


USER_REPLY_MAX_ATTEMPTS = 3
"""How many generations one user turn may cost before the trial is refused.

The user-reply guard (:mod:`tolokaforge.core.actors.reply_guard`) discards a
reply a detector flags and regenerates it, so a single turn can issue up to this
many simulator calls. It is declared here, away from the guard that reads it,
because ``reply_guard`` imports this package for
:class:`~tolokaforge.core.models.trajectory.ReplyDefect` — declaring it beside
the guard and importing it back would close an import cycle.

A module constant rather than a run-config field on purpose:
:func:`validate_rate_limit_probe_budget` multiplies the simulator's per-call
budget by it, so an operator able to raise it from a run config could defeat
that invariant from the config file — the same failure mode that makes
``retry_interval_s`` one of the knobs the invariant below reads."""


class RateLimitProbeConfig(BaseModel):
    """Rate-limit probe mode: 429s retry at a FIXED interval until a generous
    per-call wall-clock budget is spent, so a probe run's goodput measures the
    provider's served throughput instead of dying on 429s.

    OFF unless ``enabled`` is True; when off, every retry controller in the
    engine keeps its default bounded-exponential behaviour.

    The fixed interval is the point: a blocked client polls
    ``1 / retry_interval_s`` times per second, so blocked client-time is
    recoverable from the 429 count. Exponential backoff hides a different
    wait behind every retry and makes that arithmetic non-invertible.
    ``jitter_fraction`` decorrelates blocked clients without disturbing that
    arithmetic — it is symmetric, so the mean interval is unchanged.

    A probe run's latency metrics are structurally invalid —
    ``Metrics.latency_total_s`` is trial wall time, which includes 429 sleep —
    so a probe run must never produce a leaderboard number.
    """

    model_config = {"extra": "ignore"}

    enabled: bool = False
    retry_interval_s: float = Field(default=15.0, gt=0.0)

    jitter_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    """Symmetric jitter applied to ``retry_interval_s``, as a fraction of it.

    Every client blocked at the cap would otherwise retry in lockstep — burst,
    all rejected, wait, burst — which biases the very throughput the mode
    measures and is harsher on the provider than steady polling. The jitter is
    ``interval x (1 +/- jitter_fraction)``, so the *mean* interval is still
    exactly ``retry_interval_s`` and the ``1 / retry_interval_s`` poll-rate
    arithmetic survives in expectation, which is what the estimator consumes.
    ``0.0`` restores the exact fixed interval."""

    per_call_budget_s: float = Field(default=3600.0, gt=0.0)
    """Wall-clock budget for one *agent* call's 429 retries.

    A floor, not a ceiling: ``stop`` is evaluated on an attempt's outcome, so a
    call overshoots by :attr:`call_overshoot_s`.
    :func:`validate_rate_limit_probe_budget` folds that overshoot into the
    invariant rather than assuming the slack absorbs it."""

    simulator_per_call_budget_s: float = Field(default=600.0, gt=0.0)
    """Per-call 429 budget for the user-simulator client.

    Shorter than the agent's on purpose. The simulator shares the agent's
    provider quota, so it has to absorb 429s or a simulator 429 kills the trial
    the agent-side probe was keeping alive — but the simulator's throughput is
    not what the probe measures, so paying agent-sized wall time for it only
    eats the trial's lease headroom. It is part of the budget invariant, and
    enters it multiplied by :data:`USER_REPLY_MAX_ATTEMPTS`, because one turn
    issues one agent call and up to that many simulator calls (see
    :func:`validate_rate_limit_probe_budget`)."""

    bucket_width_s: int = Field(default=DEFAULT_PROBE_BUCKET_WIDTH_S, gt=0)
    """Width of one goodput-measurement window, in whole seconds.

    The probe records throughput into fixed-width windows anchored on the Unix
    epoch, not on run start, so windows produced by simultaneous run legs on
    different machines line up and can be summed window by window — see
    :meth:`~tolokaforge.core.run_display_events.RateLimitProbeStats.bucket_start`.
    Whole seconds keep every boundary an exact integer epoch, so the serialised
    timestamps match across legs with no float drift.

    Cumulative totals are not a substitute: measured goodput decays at a
    *constant* offered concurrency while the rejection rate climbs, and a single
    average hides both (``docs/OUTPUT_FORMAT.md`` § Field observations)."""

    max_buckets: int = Field(default=DEFAULT_PROBE_MAX_BUCKETS, gt=0)
    """Cap on how many ``(role, model, window)`` rows one trial may open, so
    memory stays bounded.

    Rows, not windows: a two-role trial consumes two rows per window. At the 30 s
    default width, 4096 rows is ~34 h for a single ``(role, model)`` series and
    ~17 h for the two-role default — either way far past any episode budget the
    invariant permits. Once the cap is reached a recording still lands in the flat
    and per-``(role, model)`` totals but cannot open a new row, and
    ``Metrics.probe_dropped_buckets`` counts the refused rows so the truncation is
    never silent. The cap is global rather than per series, so a high-volume role
    can consume the whole budget."""

    def for_simulator(self) -> "RateLimitProbeConfig":
        """This mode with the *simulator's* per-call budget in force.

        The client only reads ``per_call_budget_s``, so the simulator's shorter
        budget is applied by handing its client a block whose per-call budget
        *is* the simulator budget. Idempotent — re-deriving from the result
        yields the same block.

        ``bucket_width_s`` / ``max_buckets`` are carried for block fidelity and
        are inert on this copy: the accumulator is built once per trial from the
        *agent* block (``conductor._build_probe_stats``) precisely so both roles
        share one window grid. Dropping them here would make the copy lossy and
        break the idempotence above."""
        return RateLimitProbeConfig(
            enabled=self.enabled,
            retry_interval_s=self.retry_interval_s,
            jitter_fraction=self.jitter_fraction,
            per_call_budget_s=self.simulator_per_call_budget_s,
            simulator_per_call_budget_s=self.simulator_per_call_budget_s,
            bucket_width_s=self.bucket_width_s,
            max_buckets=self.max_buckets,
        )

    @property
    def turn_budget_s(self) -> float:
        """Worst-case 429 wall time one *uninterrupted turn* can spend.

        A turn issues the agent's ``generate`` and then the user simulator's
        ``reply`` (``ToolCallingLoop._run_turn`` -> ``_advance_user_turn``),
        and the episode timeout is only evaluated *between* turns, so every
        budget below can be spent back to back with nothing able to interrupt
        them.

        The simulator term is multiplied by :data:`USER_REPLY_MAX_ATTEMPTS`:
        the user-reply guard regenerates a reply a detector flags, each
        regeneration is a fresh retry controller carrying the whole simulator
        budget, and a 429 does not consume a guard attempt — it propagates out
        of the generation the guard called — so the worst cases compound
        instead of excluding each other."""
        return self.per_call_budget_s + USER_REPLY_MAX_ATTEMPTS * self.simulator_per_call_budget_s

    @property
    def call_overshoot_s(self) -> float:
        """How far past ``per_call_budget_s`` one call can run, seconds.

        ``stop`` is evaluated on an attempt's *outcome*, so a call whose elapsed
        time is a hair under its budget still gets one more wait and one more
        attempt. The wait is ``retry_interval_s`` at its jitter maximum
        (``1 + jitter_fraction``; the jitter is symmetric, so the *upper* edge is
        what bounds the worst case) and the attempt costs up to
        :data:`RATE_LIMIT_PROBE_ATTEMPT_CEILING_S`.

        Both jitter knobs are read here on purpose: ``retry_interval_s`` has no
        upper field bound, so an invariant that ignores it can be defeated by
        that knob alone while every other budget stays at its documented
        default."""
        jitter_max_wait_s = self.retry_interval_s * (1.0 + self.jitter_fraction)
        return jitter_max_wait_s + RATE_LIMIT_PROBE_ATTEMPT_CEILING_S

    @property
    def turn_overshoot_s(self) -> float:
        """The per-turn overshoot: :attr:`call_overshoot_s` for every call.

        One turn issues the agent's call plus up to
        :data:`USER_REPLY_MAX_ATTEMPTS` simulator calls, none of which can be
        interrupted, so every one of those overshoots lands inside the same
        turn."""
        return (1 + USER_REPLY_MAX_ATTEMPTS) * self.call_overshoot_s

    @property
    def turn_wall_ceiling_s(self) -> float:
        """Worst-case wall time one uninterrupted turn spends on 429 handling.

        ``turn_budget_s + turn_overshoot_s`` — the quantity
        :func:`validate_rate_limit_probe_budget` holds strictly below the
        effective episode budget."""
        return self.turn_budget_s + self.turn_overshoot_s


def validate_rate_limit_probe_budget(
    probe: RateLimitProbeConfig | None,
    episode_timeout_s: float,
    *,
    source: str,
) -> None:
    """Raise when a probe's per-turn 429 handling cannot fit inside the episode budget.

    A call already blocked in 429 backoff is not interrupted mid-flight — the
    episode timeout is only evaluated between turns — and one turn issues the
    agent's call plus up to :data:`USER_REPLY_MAX_ATTEMPTS` user-simulator
    calls, because the user-reply guard regenerates a reply a detector flags
    rather than editing it. The episode check can pass with elapsed time a hair
    under ``episode_timeout_s``, so the worst-case trial wall time is
    ``episode_timeout_s`` plus one whole turn of 429 handling::

        turn_wall_ceiling_s = turn_budget_s        # agent + guard-many simulator budgets
                            + turn_overshoot_s    # one overshoot per call

    A call's overshoot is one jitter-maximum retry interval plus one attempt's
    own ceiling (:data:`RATE_LIMIT_PROBE_ATTEMPT_CEILING_S`), because ``stop`` is
    evaluated on an attempt's *outcome* rather than pre-empting it.

    Holding ``turn_wall_ceiling_s`` strictly below ``episode_timeout_s`` bounds
    the probe-attributable wall time at ``2 x episode_timeout_s``, which is
    exactly the queue-lease horizon (``max(300, episode_s * 2)``). Every knob
    that can stretch a turn's 429 handling is read: each role's per-call budget,
    ``retry_interval_s`` and ``jitter_fraction``, with the guard's attempt count
    as a fixed multiplier the config cannot move. ``retry_interval_s`` has no
    upper field bound, and an invariant that ignores it is defeatable by that one
    knob while every other budget sits at its documented default.

    **What this bounds and what it does not.** It bounds what the *probe* adds.
    It does not bound tool execution, grading, or a runaway upstream stream: the
    loop has no per-turn timeout, and an attempt's ``timeout`` is a per-read
    httpx timeout unless a preset sets ``api_call_wall_timeout_s`` (see
    :data:`RATE_LIMIT_PROBE_ATTEMPT_CEILING_S`). Those components are identical
    on a probe-off run, so the guarantee is "enabling the mode cannot be the
    thing that pushes a trial past its lease", not "a trial can never outlive
    its lease".

    ``episode_timeout_s`` must be the *effective* budget — the value after the
    task-pack ``min()`` clamp — not the configured run-level value, or a pack
    declaring ``trial_seconds`` would silently shrink the ceiling this
    invariant is checked against. ``source`` names the config site for the
    error message.
    """
    if probe is None or not probe.enabled:
        return
    if episode_timeout_s <= RATE_LIMIT_PROBE_MIN_EPISODE_S:
        raise ValueError(
            f"{source}: rate_limit_probe.enabled requires an episode budget "
            f"above {RATE_LIMIT_PROBE_MIN_EPISODE_S}s (hours, not minutes); "
            f"effective episode budget is {episode_timeout_s}s. Raise "
            "orchestrator.timeouts.episode_s."
        )
    if probe.turn_wall_ceiling_s >= episode_timeout_s:
        raise ValueError(
            f"{source}: rate_limit_probe worst-case per-turn 429 wall time "
            f"({probe.turn_wall_ceiling_s}s = per_call_budget_s "
            f"{probe.per_call_budget_s}s + {USER_REPLY_MAX_ATTEMPTS} x "
            f"simulator_per_call_budget_s {probe.simulator_per_call_budget_s}s "
            f"+ {probe.turn_overshoot_s}s of overshoot for those "
            f"{1 + USER_REPLY_MAX_ATTEMPTS} calls, at retry_interval_s "
            f"{probe.retry_interval_s}s and jitter_fraction "
            f"{probe.jitter_fraction}) must be strictly below the effective "
            f"episode budget ({episode_timeout_s}s). One turn issues the agent's "
            f"call and up to {USER_REPLY_MAX_ATTEMPTS} user-simulator calls — the "
            "reply guard regenerates a reply a detector flags instead of editing it "
            "— back to back, the episode timeout is only checked between turns, "
            "and stop is evaluated on an attempt's outcome — so a larger budget "
            "lets the trial outlive its queue lease and be re-run by another "
            "worker. Lower per_call_budget_s / simulator_per_call_budget_s / "
            "retry_interval_s, or raise orchestrator.timeouts.episode_s."
        )


class StuckHeuristics(BaseModel):
    """Stuck detection configuration"""

    model_config = {"extra": "ignore"}

    enabled: bool = True
    max_repeated_tool_calls: int = 10
    max_idle_turns: int = 12


class TypeSenseConfig(BaseModel):
    """TypeSense server configuration for knowledge base search.

    Supports three modes:
    - local: Orchestrator manages a local Docker container (auto start/stop)
    - remote: Connect to an external TypeSense server
    - disabled: no server is started

    ``enabled: false`` and ``mode: disabled`` are equally final: the orchestrator
    hands the adapter connection details only when the block is enabled AND the
    mode is not ``disabled``, so under either spelling no task's ``search.host``
    is set and no trial reaches the TypeSense plane. ``remote`` still emits —
    nothing is started for it, but the address is a real one.
    """

    model_config = {"extra": "ignore"}

    enabled: bool = True  # Whether TypeSense is enabled
    mode: Literal["local", "remote", "disabled"] = "local"  # Server mode
    host: str = "127.0.0.1"  # TypeSense server host
    port: int | Literal["auto"] = "auto"  # Port ("auto" finds available port)
    api_key: str | None = None  # API key (auto-generated if None for local mode)
    data_dir: str = ".cache/typesense"  # Data directory for local mode
    image: str = "typesense/typesense:26.0"  # Docker image for local mode
    container_name: str = "tolokaforge-typesense"  # Container name for local mode
    timeout: float = 30.0  # Connection timeout
    cleanup_on_exit: bool = True  # Remove container on exit (local mode)


LEGACY_DOCKER_RUNTIME_ALIAS = "docker"
"""Legacy ``orchestrator.runtime`` value accepted as an alias for the
``shared`` runtime backend. Coerced before any registry lookup — the registry
has no ``docker`` name."""

DOCKER_RUNTIME_ALIAS_TARGET = "shared"
"""Registered runtime-backend name :data:`LEGACY_DOCKER_RUNTIME_ALIAS` maps to."""


class OrchestratorConfig(BaseModel):
    """Orchestrator configuration"""

    model_config = {"extra": "ignore"}

    workers: int = 8
    repeats: int = 5
    # Diagnostic only. Off by default so trial_index=N benefits from
    # warm state (caches, indexes) seeded by trial_index<N. Turn on to
    # decorrelate trial_index from "coldness" when measuring per-index
    # metric asymmetries.
    shuffle_trials: bool = False
    max_budget_usd: float | None = Field(
        default=None, ge=0.0
    )  # Optional hard stop for cumulative run spend
    max_requests_per_second: float | None = Field(
        default=None, gt=0.0
    )  # Optional global request throttle across workers
    max_attempt_retries: int = Field(
        default=0, ge=0
    )  # Number of retry attempts for transient infrastructure failures
    queue_backend: Literal["sqlite", "postgres"] = "sqlite"
    queue_postgres_dsn: str | None = None
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    """Run-level cap on per-trial timeouts. Effective values applied
    by the runtime = ``min(TaskConfig.timeouts, this)`` — the
    task-scoped value is authoritative, this is an optional
    operator-side clamp. Unset means the task-scoped value governs.
    Field-name migration to ``TimeoutDefaults`` (``trial_seconds`` /
    ``tool_call_seconds``) lands with the cleanup milestone."""

    rate_limit_probe: RateLimitProbeConfig = Field(default_factory=RateLimitProbeConfig)
    """Rate-limit probe mode. Disabled by default; see
    :class:`RateLimitProbeConfig`. A probe run measures provider-served
    throughput and must not produce a leaderboard number."""

    max_turns: int = 50
    """Run-level cap on per-trial ``max_turns`` — an always-on operator
    clamp. Effective budget at runtime is ``min(TaskConfig.max_turns, this)``:
    a task authoring a higher value is clamped down to this cap. To let a
    task's value stand uncapped, set this above the task's declared value.
    A future release will flip this to an opt-in cap (default ``None``);
    tracked as a post-M9 follow-up."""

    auto_start_services: bool = True  # Auto-start Docker services via EngineStack

    strict_task_load: bool = False
    """Opt in to fail-loud task loading. When ``False`` (the default), an
    adapter exception raised from :meth:`BaseAdapter.get_task` during
    :meth:`Orchestrator.load_tasks` is logged at error level and the task is
    skipped — the run proceeds with the remaining tasks. When ``True`` the
    exception propagates with the task id in the message, so the run refuses
    to start rather than silently omitting a task.

    ``--dry-run`` is strict regardless of this flag: it has its own loader
    (:func:`tolokaforge.core.dry_run.load_tasks_for_dry_run`) with no
    exception handling — surfacing config errors is the whole point of that
    entry point."""

    continue_prompt: str = "Please proceed to the next step."
    """Deprecated. Not consumed by any runtime code today; the
    canonical home is ``TaskDefaults.continue_prompt``. Kept for
    backward compatibility of run configs that declare it; a
    ``DeprecationWarning`` fires when the field is explicitly set to
    a non-default value."""

    stuck_heuristics: StuckHeuristics = Field(default_factory=StuckHeuristics)
    """Deprecated. The conductor now reads stuck-heuristics from the
    task-scoped ``TaskConfig.stuck_heuristics`` (populated via the M2
    loader's per-task merge chain from
    ``project.task_defaults.stuck_heuristics``). Kept on this model
    for backward compatibility; a ``DeprecationWarning`` fires when
    the field is explicitly set."""

    runtime: str | None = None
    """Deprecated operator override for backend selection.

    Backend selection is task-driven — the orchestrator picks
    :class:`PerTrialRuntimeBackend` when any task's manifest requires
    per-trial materialisation, otherwise :class:`SharedStackRuntimeBackend`.
    Setting this field bypasses that signal and emits a
    ``DeprecationWarning``. Retired in a future release.

    Any name registered in the ``tolokaforge.runtime_backends`` entry-point
    group is accepted (built-in ``shared`` / ``per_trial`` / ``in_memory``, or
    a plug-in's name); the name is resolved against the registry at run start,
    which raises an actionable error listing the known names on a typo.

    Legacy value ``docker`` is accepted as an alias for ``shared`` with
    the same deprecation warning; drop both from configs going forward.
    """

    @field_validator("runtime", mode="before")
    @classmethod
    def _accept_legacy_docker_alias(cls, value: Any) -> Any:
        """Accept ``docker`` as an alias for ``shared`` and emit the
        deprecation warning for any explicit setting."""
        if value is None:
            return value
        if value == LEGACY_DOCKER_RUNTIME_ALIAS:
            warnings.warn(
                "OrchestratorConfig.runtime = 'docker' is a deprecated alias "
                "for 'shared'; update your run config.",
                DeprecationWarning,
                stacklevel=2,
            )
            value = DOCKER_RUNTIME_ALIAS_TARGET
        warnings.warn(
            "OrchestratorConfig.runtime is deprecated; backend selection is "
            "now task-driven (any task requiring per-trial isolation forces "
            "PerTrialRuntimeBackend, otherwise SharedStackRuntimeBackend). "
            "Drop `orchestrator.runtime` from the run config. Retired in a "
            "future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return value

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_task_scope_fields(cls, values: Any) -> Any:
        """Emit ``DeprecationWarning`` when a caller sets
        ``stuck_heuristics`` or ``continue_prompt`` on the run-side
        orchestrator config. Both fields have canonical homes on
        ``TaskDefaults`` (``TaskDefaults.stuck_heuristics``,
        ``TaskDefaults.continue_prompt``); the orchestrator copies are
        retained for backward compatibility and retired with the
        cleanup milestone.
        """
        if not isinstance(values, dict):
            return values
        for field_name in ("stuck_heuristics", "continue_prompt"):
            if field_name in values:
                warnings.warn(
                    f"OrchestratorConfig.{field_name} is deprecated; move "
                    f"it under task_defaults.{field_name} on the enclosing "
                    "project. The orchestrator copy is retained for "
                    "backward compatibility only.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        return values

    @model_validator(mode="after")
    def _check_rate_limit_probe_budget(self) -> Self:
        """Reject a probe config whose budgets cannot fit at load time.

        This checks the *configured* run-level episode budget so an
        unrunnable YAML fails before any provisioning. The conductor
        re-checks against the per-task *effective* budget, which is the
        authoritative value once the task-pack clamp is applied.
        """
        validate_rate_limit_probe_budget(
            self.rate_limit_probe,
            self.timeouts.episode_s,
            source="orchestrator",
        )
        return self

    typesense: TypeSenseConfig | None = None  # TypeSense server configuration

    def effective_typesense(self) -> TypeSenseConfig | None:
        """Return the run's TypeSense plane, or ``None`` when it has none.

        ``mode: disabled`` is as final as ``enabled: false``: no server is
        started for either, so a plane exists only when the block is enabled
        AND the mode is not ``disabled``. Every consumer that has to answer
        "does this run have a TypeSense plane" — the connection details handed
        to the adapter, the address injected into the runner container, the
        dry-run preview of both — reads this one answer, so they cannot drift
        apart.
        """
        if self.typesense is None or not self.typesense.enabled:
            return None
        if self.typesense.mode == "disabled":
            return None
        return self.typesense


class HarnessAdapterConfig(BaseModel):
    """Configuration for external harness adapters (e.g., Tau-bench)"""

    model_config = {"extra": "ignore"}

    type: str = "native"  # "native", "tau", etc.
    params: dict[str, Any] = Field(default_factory=dict)


class GradingFindingSeverity(str, Enum):
    """How sure the authoring gate is that a finding is the author's mistake.

    Most severe first: enforcing down to one class enforces every class above it.
    ``unchecked`` is deliberately not a member — it is a channel rather than a
    severity, and no caller may make it fatal.
    """

    ERROR = "error"
    """The schema proves the check cannot grade what its author wrote."""

    ADVISORY = "advisory"
    """A schema that permits what it does not declare, so a probable typo."""


class GradingValidationConfig(BaseModel):
    """How strictly the pre-run gate reads the selected packs' grading blocks.

    A sub-object rather than a bare key so a third severity class costs a member
    of :class:`GradingFindingSeverity` and nothing else.
    """

    model_config = {"extra": "forbid"}

    fail_on: GradingFindingSeverity = GradingFindingSeverity.ADVISORY
    """The least severe finding class that fails the run."""


class EvaluationConfig(BaseModel):
    """Evaluation configuration.

    ``projects`` lists project roots this run pulls tasks from. When
    omitted the loader defaults to the enclosing project (the project
    directory containing the run config file). Legacy configs may use
    ``task_packs`` — it is accepted here as an alias for ``projects``
    and coerced with a ``DeprecationWarning``.
    """

    model_config = {"extra": "ignore"}

    tasks_glob: str = "**/task.yaml"
    projects: list[str] = Field(default_factory=list)
    task_packs: list[str] = Field(default_factory=list)
    output_dir: str
    cache_images: bool = True
    harness_adapter: HarnessAdapterConfig | None = None
    grading_validation: GradingValidationConfig = Field(default_factory=GradingValidationConfig)
    """Severity policy for the pre-run grading gate.

    ``extra="ignore"`` on this model means a misspelled *block* name —
    ``grading_validaton:`` — is dropped without a word and the defaults stand.
    The sub-object's own ``extra="forbid"`` catches a misspelled field inside a
    correctly-named block. Documented in ``docs/CONFIG.md``.
    """

    @model_validator(mode="before")
    @classmethod
    def _coerce_task_packs_alias(cls, values: Any) -> Any:
        return coerce_task_packs_alias(values)


class EngineConfig(BaseModel):
    """Engine-wide configuration that lives outside per-trial/per-model surface.

    Holds operator-level knobs that change *which engine extensions* a run
    picks up at startup — distinct from ``OrchestratorConfig`` (per-run
    execution semantics) and ``ModelConfig`` (per-model overrides).
    """

    model_config = {"extra": "ignore"}

    presets_file: str | None = Field(
        default=None,
        description=(
            "Path to an additional model-presets YAML overlay. Merged onto "
            "the bundled tolokaforge_models/data/model_presets.yaml at startup "
            "so operators can register or shadow presets without an engine "
            "release. The --presets-file CLI flag takes precedence over this "
            "field. See docs/CONFIG.md and ADR 0002."
        ),
    )


class LocalDockerComputeConfig(BaseModel):
    """Configuration for the ``local-docker`` compute provider."""

    model_config = {"extra": "ignore"}


class ComputeConfig(BaseModel):
    """Compute substrate + parallelism + budget selection for a run.

    ``provider`` selects the runtime substrate; the sub-block matching
    the provider (e.g. ``local_docker``) carries provider-specific
    settings. When another provider is registered it shows up as a new
    ``Literal`` value on ``provider`` plus its own sub-block.
    """

    model_config = {"extra": "ignore"}

    provider: Literal["local-docker"] = "local-docker"
    workers: int | None = Field(default=None, ge=1)
    max_budget_usd: float | None = Field(default=None, ge=0.0)
    max_requests_per_second: float | None = Field(default=None, gt=0.0)
    max_attempt_retries: int = Field(default=0, ge=0)
    log_tail: int = Field(default=500, ge=1)
    capture_logs_on_success: bool = False
    local_docker: LocalDockerComputeConfig | None = None

    capabilities: list[Any] = Field(default_factory=list)
    """Backend-capability declarations. Each entry is either a bare
    ``"name"`` string or a ``{"name": {params}}`` mapping with a single
    key. Field is typed ``list[Any]`` so :meth:`_validate_capability_entries`
    can emit context-rich errors (Pydantic's built-in union resolution
    reports the failure against every union arm, which reads badly for
    authors). Registry lookup and admission gate land with the isolation
    redesign; this field reserves the shape so packs can start
    declaring capabilities against the eventual registry vocabulary."""

    @field_validator("capabilities")
    @classmethod
    def _validate_capability_entries(cls, value: list[Any]) -> list[Any]:
        for idx, entry in enumerate(value):
            if isinstance(entry, str):
                if not entry:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: bare-string entry "
                        "must be a non-empty capability name."
                    )
                continue
            if isinstance(entry, dict):
                if len(entry) != 1:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: dict entry must have "
                        f"exactly one key (the capability name); got {sorted(entry)!r}."
                    )
                ((name, params),) = entry.items()
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: capability name must "
                        f"be a non-empty string; got {name!r}."
                    )
                if not isinstance(params, dict):
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: params for capability "
                        f"{name!r} must be a mapping; got {type(params).__name__}."
                    )
                continue
            raise ValueError(
                f"ComputeConfig.capabilities[{idx}]: entry must be a string or a "
                f"single-key dict; got {type(entry).__name__}."
            )
        return value


class LocalStorageConfig(BaseModel):
    """Local-filesystem storage backend for artifacts or logs.

    Extras rejected so a mis-tagged input (e.g. ``bucket`` on a
    ``type=local`` block) fails loud instead of dropping the stray
    field. The discriminator on ``StorageBackend`` selects this variant
    by ``type``; extras=forbid makes the selection safe.
    """

    model_config = {"extra": "forbid"}

    type: Literal["local"] = "local"
    path: str


class S3StorageConfig(BaseModel):
    """S3 storage backend for artifacts or logs.

    Extras rejected — same rationale as :class:`LocalStorageConfig`.
    """

    model_config = {"extra": "forbid"}

    type: Literal["s3"] = "s3"
    bucket: str
    prefix: str | None = None


# Discriminated union over the ``type`` tag so mixed-tag inputs fail
# loud instead of silently dropping the fields of the losing variant.
StorageBackend = Annotated[LocalStorageConfig | S3StorageConfig, Field(discriminator="type")]


class QueueStorageConfig(BaseModel):
    """Queue backend for orchestrator state.

    ``backend='postgres'`` requires ``postgres_dsn`` — enforced by
    ``_require_postgres_dsn`` so a partial declaration fails at load
    instead of falling back silently.
    """

    model_config = {"extra": "ignore"}

    backend: Literal["sqlite", "postgres"] = "sqlite"
    postgres_dsn: str | None = None

    @model_validator(mode="after")
    def _require_postgres_dsn(self) -> Self:
        if self.backend == "postgres" and not self.postgres_dsn:
            raise ValueError("QueueStorageConfig.backend='postgres' requires postgres_dsn.")
        return self


class StorageConfig(BaseModel):
    """Where a run's artifacts, logs, and queue state live."""

    model_config = {"extra": "ignore"}

    artifacts: StorageBackend | None = None
    logs: StorageBackend | None = None
    queue: QueueStorageConfig | None = None


class TracingConfig(BaseModel):
    """Tracing exporter selection.

    A non-default ``exporter`` requires an ``endpoint``; ``none`` (the
    default) does not.
    """

    model_config = {"extra": "ignore"}

    exporter: Literal["none", "otlp"] = "none"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter != "none" and not self.endpoint:
            raise ValueError(f"TracingConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class MetricsConfig(BaseModel):
    """Metrics exporter selection.

    A non-default ``exporter`` requires an ``endpoint``; ``none`` (the
    default) does not.
    """

    model_config = {"extra": "ignore"}

    exporter: Literal["none", "prometheus"] = "none"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter != "none" and not self.endpoint:
            raise ValueError(f"MetricsConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class LoggingConfig(BaseModel):
    """Logging exporter selection.

    ``exporter='otlp'`` requires an ``endpoint``; ``stdout`` (the
    default) does not.
    """

    model_config = {"extra": "ignore"}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    exporter: Literal["stdout", "otlp"] = "stdout"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter == "otlp" and not self.endpoint:
            raise ValueError(f"LoggingConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class ObservabilityConfig(BaseModel):
    """Tracing, metrics, and logging exporters for a run."""

    model_config = {"extra": "ignore"}

    tracing: TracingConfig | None = None
    metrics: MetricsConfig | None = None
    logging: LoggingConfig | None = None
    pricing_overlay_path: Path | str | None = None
    """Optional JSON or YAML overlay merged onto the shipped pricing
    table before the orchestrator is constructed. Same schema as
    ``tolokaforge_models/data/pricing.json``. Applied globally for the
    run's lifetime; used when a model the shipped table does not price
    (or prices incorrectly) is in use."""


_DUAL_HOME_COMPUTE_ALIASES: tuple[tuple[str, str], ...] = (
    ("workers", "workers"),
    ("max_budget_usd", "max_budget_usd"),
    ("max_requests_per_second", "max_requests_per_second"),
    ("max_attempt_retries", "max_attempt_retries"),
)
"""``orchestrator.<legacy>`` → ``compute.<canonical>`` field pairs.
Same names on both sides; kept explicit so a future rename hits one
list."""

_DUAL_HOME_STORAGE_QUEUE_ALIASES: tuple[tuple[str, str], ...] = (
    ("queue_backend", "backend"),
    ("queue_postgres_dsn", "postgres_dsn"),
)
"""``orchestrator.<legacy>`` → ``storage.queue.<canonical>`` field
pairs. Legacy names carry the ``queue_`` prefix; canonical names
don't (the ``queue`` sub-block is the namespace)."""


class RunConfig(BaseModel):
    """Complete run configuration"""

    model_config = {"extra": "ignore"}

    models: dict[str, ModelConfig]
    orchestrator: OrchestratorConfig
    evaluation: EvaluationConfig
    engine: EngineConfig | None = None
    compute: ComputeConfig | None = None
    storage: StorageConfig | None = None
    observability: ObservabilityConfig | None = None
    docker: DockerConfig | None = None

    @property
    def effective_workers(self) -> int:
        """Effective worker count for this run.

        Canonical home is ``compute.workers``. When the user declared
        it (directly or via the ``orchestrator.workers`` legacy alias,
        which the parse-time lift moved to ``compute.workers``), the
        canonical value wins. Otherwise falls back to the
        ``OrchestratorConfig.workers`` default so runs that never
        touched either field still work.
        """
        if self.compute is not None and self.compute.workers is not None:
            return self.compute.workers
        return self.orchestrator.workers

    @property
    def effective_max_budget_usd(self) -> float | None:
        """Effective per-run budget cap in USD. ``compute.max_budget_usd``
        is canonical; falls back to ``orchestrator.max_budget_usd``."""
        if self.compute is not None and self.compute.max_budget_usd is not None:
            return self.compute.max_budget_usd
        return self.orchestrator.max_budget_usd

    @property
    def effective_max_requests_per_second(self) -> float | None:
        """Effective global request throttle. ``compute.max_requests_per_second``
        is canonical; falls back to
        ``orchestrator.max_requests_per_second``."""
        if self.compute is not None and self.compute.max_requests_per_second is not None:
            return self.compute.max_requests_per_second
        return self.orchestrator.max_requests_per_second

    @property
    def effective_max_attempt_retries(self) -> int:
        """Effective retry attempts for transient infra failures.
        ``compute.max_attempt_retries`` is canonical; falls back to
        ``orchestrator.max_attempt_retries``.

        Asymmetric with the other ``effective_*`` accessors: the field
        is a plain ``int`` with default ``0`` on both sides — there is
        no ``None`` sentinel to distinguish "unset" from "explicit 0".
        Whenever ``compute`` exists, its value is authoritative; the
        parse-time lift ensures both sides agree by the time either is
        constructed. Object-form callers (``RunConfig(compute=...)``)
        who need the fallback must leave ``compute`` unset entirely."""
        if self.compute is not None:
            return self.compute.max_attempt_retries
        return self.orchestrator.max_attempt_retries

    @property
    def effective_queue_backend(self) -> str:
        """Effective queue-storage backend. ``storage.queue.backend`` is
        canonical; falls back to ``orchestrator.queue_backend``."""
        if self.storage is not None and self.storage.queue is not None:
            return self.storage.queue.backend
        return self.orchestrator.queue_backend

    @property
    def effective_queue_postgres_dsn(self) -> str | None:
        """Effective postgres DSN when the queue backend is ``postgres``.
        ``storage.queue.postgres_dsn`` is canonical; falls back to
        ``orchestrator.queue_postgres_dsn``."""
        if self.storage is not None and self.storage.queue is not None:
            return self.storage.queue.postgres_dsn
        return self.orchestrator.queue_postgres_dsn

    @model_validator(mode="before")
    @classmethod
    def _lift_orchestrator_dual_home_aliases(cls, values: Any) -> Any:
        """Lift legacy ``orchestrator.*`` fields to their canonical
        ``compute.*`` / ``storage.queue.*`` homes at parse time.

        The six aliases (workers, max_budget_usd, max_requests_per_second,
        max_attempt_retries, queue_backend, queue_postgres_dsn) once
        lived on ``OrchestratorConfig`` alone; the Project layer moved
        them to ``ComputeConfig`` / ``StorageConfig``. This validator
        preserves the legacy shape as a read-time alias so unmigrated
        run configs still load, emits per-key ``DeprecationWarning``,
        and drops the legacy key from ``orchestrator`` so downstream
        reads route through the canonical field.

        Collision policy — if both sides carry values:
        - Equal values: warn once naming the collision, drop legacy.
        - Differing values: fail loud naming both keys and both values;
          the author must pick one.

        Scope: only dict-form inputs are lifted. Object-form callers
        that pass an already-constructed ``OrchestratorConfig``
        instance (e.g. tests using ``RunConfig(orchestrator=
        OrchestratorConfig(workers=4))``) bypass the lift entirely —
        the effective-config accessors' fallback branch surfaces the
        orchestrator value in that case, but no deprecation warning
        fires. Production YAML load always passes dicts, so the lift
        runs on every real load.
        """
        if not isinstance(values, dict):
            return values
        orch_input = values.get("orchestrator")
        if not isinstance(orch_input, dict):
            return values

        # Copy input containers before mutating so callers who kept a
        # reference to the raw dict (e.g. ``config_validator`` reads
        # ``raw["orchestrator"]`` after calling ``RunConfig(**raw)``)
        # still see their original layout.
        values = dict(values)
        orch = dict(orch_input)
        values["orchestrator"] = orch

        compute_input = values.get("compute")
        compute = dict(compute_input) if isinstance(compute_input, dict) else {}
        for legacy_key, canonical_key in _DUAL_HOME_COMPUTE_ALIASES:
            _lift_alias(orch, legacy_key, compute, canonical_key, "compute")
        if compute:
            values["compute"] = compute

        storage_input = values.get("storage")
        storage = dict(storage_input) if isinstance(storage_input, dict) else {}
        queue_input = storage.get("queue")
        queue = dict(queue_input) if isinstance(queue_input, dict) else {}
        for legacy_key, canonical_key in _DUAL_HOME_STORAGE_QUEUE_ALIASES:
            _lift_alias(orch, legacy_key, queue, canonical_key, "storage.queue")
        if queue:
            storage["queue"] = queue
            values["storage"] = storage

        return values


def _lift_alias(
    legacy_container: dict[str, Any],
    legacy_key: str,
    canonical_container: dict[str, Any],
    canonical_key: str,
    canonical_container_label: str,
) -> None:
    """Lift a single legacy key into a canonical container in place.

    Removes the legacy key from *legacy_container* (so downstream reads
    can't accidentally see both). Emits ``DeprecationWarning`` when the
    legacy key was set; raises ``ValueError`` on a collision with a
    different canonical value.
    """
    if legacy_key not in legacy_container:
        return
    legacy_value = legacy_container[legacy_key]
    canonical_value = canonical_container.get(canonical_key)
    if canonical_value is not None and canonical_value != legacy_value:
        raise ValueError(
            f"orchestrator.{legacy_key}={legacy_value!r} conflicts with "
            f"{canonical_container_label}.{canonical_key}={canonical_value!r}; "
            f"drop the legacy `orchestrator.{legacy_key}` and keep the "
            f"canonical `{canonical_container_label}.{canonical_key}`."
        )
    if canonical_value is None:
        canonical_container[canonical_key] = legacy_value
    warnings.warn(
        f"orchestrator.{legacy_key} is deprecated; use "
        f"{canonical_container_label}.{canonical_key} instead. Legacy "
        "field will be removed in a future release.",
        DeprecationWarning,
        stacklevel=4,
    )
    del legacy_container[legacy_key]


class RunDefaults(BaseModel):
    """Base run-level configuration inherited by every ``run_configs/*.yaml``.

    Applied at loader time; per-invocation run-config files deep-merge on
    top. Every field is optional — a project without ``run_defaults`` acts
    as if every run config were a standalone declaration.
    """

    model_config = {"extra": "ignore"}

    compute: ComputeConfig | None = None
    storage: StorageConfig | None = None
    observability: ObservabilityConfig | None = None
    orchestrator: OrchestratorConfig | None = None
    models: dict[str, ModelConfig] = Field(default_factory=dict)
