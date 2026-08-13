"""`Conductor` Protocol — the per-trial executor seam.

The orchestrator schedules trials and aggregates results; a
`Conductor` runs each individual trial end-to-end (environment setup,
runner registration, agent loop, grading, artifact writing). The
orchestrator depends on this Protocol, not a concrete class.

* :class:`Conductor` — Protocol the orchestrator depends on.
* :class:`InProcessConductor` — the production implementation that
  runs the trial in the orchestrator process. Receives the per-run
  dependencies (adapter, writer, config, …) at construction time.
* :class:`InMemoryConductor` — non-trial-executing implementation used
  as a test fixture. Records every ``run()`` invocation and returns a
  configurable :class:`TrialResult`; useful for asserting orchestrator
  scheduling / retry behaviour without spinning up Docker or an LLM.

The ``TrialRunner`` class in ``tolokaforge.core.runner`` is a separate
concept — it drives the agent ↔ user simulator loop *inside* a trial.
``InProcessConductor.run`` instantiates one per trial as part of its
body.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.adapters import BaseAdapter
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.env_identity import describe_environment_identity
from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.llm import LLMClient, UserSimulator, build_capabilities
from tolokaforge.core.llm.presets import (
    resolve_effective_preset,
    resolve_policy_names,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    RateLimitProbeConfig,
    RunConfig,
    TaskConfig,
    Trajectory,
    TrialStatus,
    validate_rate_limit_probe_budget,
)
from tolokaforge.core.output.artifacts import TrialArtifactWriter
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    RateLimitProbeStats,
    RunDisplayEvents,
    _NullRunDisplayEvents,
)
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.core.system_prompt import build_system_prompt
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, TrialResult, TrialSpec
from tolokaforge.core.trial_grader import GradingFailedError, TrialGrader

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger

__all__ = [
    "DEFAULT_MAX_TURNS",
    "Conductor",
    "ConductorCallLog",
    "ConductorContext",
    "ConductorFactory",
    "InMemoryConductor",
    "InProcessConductor",
    "resolve_max_turns",
]

#: Engine default per-trial turn budget, applied when neither the run-level
#: cap (``OrchestratorConfig.max_turns``) nor the task declares a value.
DEFAULT_MAX_TURNS = 50


def resolve_max_turns(task_max_turns: int | None, run_cap: int | None) -> int:
    """Coalesce the task-declared budget and the optional run-level cap into a
    concrete per-trial turn budget.

    The run cap is an optional operator-side clamp; the task value is
    authoritative for the task's own semantics. When both are set the effective
    budget is the tighter of the two. When neither is set the engine default
    (:data:`DEFAULT_MAX_TURNS`) applies.
    """
    if task_max_turns is None and run_cap is None:
        return DEFAULT_MAX_TURNS
    if task_max_turns is None:
        return run_cap
    if run_cap is None:
        return task_max_turns
    return min(task_max_turns, run_cap)


# ---------------------------------------------------------------------------
# Factory contract — the typed argument for a ``Conductor`` factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConductorContext:
    """Per-run dependencies the orchestrator hands to a Conductor factory.

    Packs the arguments :class:`InProcessConductor` takes at construction
    into a single typed value so factory signatures stay stable as new
    orchestrator-side dependencies land. The factory contract is
    ``Callable[[ConductorContext], Conductor]``.
    """

    adapter: BaseAdapter
    artifact_writer: TrialArtifactWriter
    config: RunConfig
    logger: StructuredLogger
    verbose: bool
    strict: bool
    agent_client: LLMClient
    runtime_backend: RuntimeBackend
    trial_grader: TrialGrader
    output_dir: Path
    request_limiter: GlobalRateLimiter | None
    events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)


# ---------------------------------------------------------------------------
# Module-level helpers (called by InProcessConductor's body)
# ---------------------------------------------------------------------------


def _build_resolved_block(model_config: ModelConfig) -> dict[str, Any]:
    """Return the ``resolved:`` block for a :class:`ModelConfig`.

    Shape: ``{"effective_preset": ..., "schema_sanitizer": ..., ...}``.
    See :func:`tolokaforge.core.llm.presets.resolve_policy_names` for the
    named policy slots included in the fingerprint. Analytics tools diff
    this across runs to detect preset / capability drift.
    """
    capabilities = build_capabilities(
        model_config.name,
        model_config.provider,
        overrides=model_config.capabilities,
    )
    return {
        "effective_preset": resolve_effective_preset(model_config.name, model_config.provider),
        **resolve_policy_names(capabilities),
    }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Conductor(Protocol):
    """Per-trial executor. Reads a typed :class:`TrialSpec` + orchestrator
    -side :class:`TaskConfig`, writes a :class:`TrialResult`.

    The orchestrator constructs a Conductor once per ``run()`` invocation
    (after the adapter, runtime backend, and per-run dependencies are
    resolved at the orchestrator level) and calls ``conductor.run(spec,
    task_config)`` for every leased trial.

    * ``spec`` — the wire-format :class:`TrialSpec` (ADR-0003) carrying
      trial identity, model configs, env endpoints, and per-trial knobs.
    * ``task_config`` — the orchestrator-side rich :class:`TaskConfig`
      (initial state, tool configs, user-simulator mode, …). The wire
      format ``spec.task`` is the runner-side projection; the conductor
      needs both surfaces because the per-trial execution path uses
      orchestrator-side detail the wire format doesn't carry.

    **Optional capability:** ``supports_rate_limit_probe: bool``. Rate-limit
    probe mode is armed by the orchestrator (it owns the agent client) but the
    simulator-side probe, the per-task effective-budget re-check and the
    per-trial telemetry accumulator are the *conductor's* to wire — see
    :data:`SUPPORTS_RATE_LIMIT_PROBE_ATTR`. A conductor that wires all three
    declares the attribute ``True``; anything else (including a conductor
    written before the mode existed) is treated as unsupported and
    :meth:`Orchestrator._build_conductor` refuses to start such a run rather
    than emitting artifacts that read all-default while the run really did
    absorb 429s. Deliberately *not* a declared Protocol member: the Protocol is
    ``@runtime_checkable`` and adding a data member would break ``isinstance``
    for every implementation that predates it.
    """

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        """Execute one trial end-to-end."""
        ...


SUPPORTS_RATE_LIMIT_PROBE_ATTR = "supports_rate_limit_probe"
"""Name of the optional :class:`Conductor` capability flag for probe mode.

Read with a ``False`` default, so a conductor that never heard of the mode fails
closed instead of silently producing a run whose ``metrics.yaml`` proves nothing.
See :class:`Conductor` and :meth:`InProcessConductor.supports_rate_limit_probe`.
"""


def conductor_supports_rate_limit_probe(conductor: object) -> bool:
    """Whether *conductor* wires every part of rate-limit probe mode.

    Fails closed: an implementation that does not declare
    :data:`SUPPORTS_RATE_LIMIT_PROBE_ATTR` is unsupported. Arming the mode on
    such a conductor produces an agent client that really does absorb 429s for up
    to ``per_call_budget_s`` per call while the artifacts carry every
    ``rate_limit_*`` / ``probe_*`` field at its default — a run that is
    indistinguishable from a normal one in its own output.
    """
    return getattr(conductor, SUPPORTS_RATE_LIMIT_PROBE_ATTR, False) is True


def require_rate_limit_probe_support(
    conductor: object,
    probe: RateLimitProbeConfig,
    *,
    source: str,
) -> None:
    """Raise unless *conductor* can support an enabled rate-limit probe *probe*.

    No-op while the mode is off. Called from every site that resolves a conductor
    while holding an already-armed agent client — ``Orchestrator._build_conductor``
    and :func:`tolokaforge.core.run_trial.run_trial` — because arming and wiring
    live at different layers: the orchestrator owns the agent client, the
    conductor owns the simulator probe, the per-task effective-budget re-check
    and the per-trial telemetry accumulator.

    Fail-fast rather than degrade: without those three the mode still arms, so
    the run genuinely absorbs 429s and its latency figures are genuinely inflated
    while ``metrics.yaml`` carries every counter at its default. Nothing
    downstream could tell that run apart from a normal one, which is exactly the
    artifacts-prove-nothing failure the mode's design is meant to exclude.

    ``source`` names the call site for the message.
    """
    if not probe.enabled or conductor_supports_rate_limit_probe(conductor):
        return
    raise ValueError(
        f"{source}: rate_limit_probe.enabled requires a conductor that supports "
        f"the mode, but {type(conductor).__name__} does not declare "
        f"{SUPPORTS_RATE_LIMIT_PROBE_ATTR}=True. Only such a conductor wires the "
        "user-simulator probe, the per-task episode-budget re-check and the "
        "per-trial probe telemetry, so this run would absorb 429s while writing "
        "all-default rate_limit_* / probe_* metrics. Run the probe on the "
        f"in_process conductor, or set {SUPPORTS_RATE_LIMIT_PROBE_ATTR} = True on "
        "your conductor once it wires all three."
    )


ConductorFactory = Callable[[ConductorContext], Conductor]
"""Type of the ``Orchestrator.deps.conductor_factory`` seam.

A callable that receives a :class:`ConductorContext` and returns any
implementation of the :class:`Conductor` Protocol.
"""


# ---------------------------------------------------------------------------
# InMemoryConductor — test fixture, records calls + returns synthetic result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TrialSetup:
    """Per-trial context assembled during setup, threaded through the phase
    methods of :meth:`InProcessConductor.run`.

    Populated by :meth:`InProcessConductor._setup_trial`; consumed by the
    downstream phases (agent loop, final-state capture, grading, artifact
    write). Frozen because the run body only reads these values — mutations
    to trial state happen via ``env_state`` and ``trajectory``.
    """

    trial_id: str
    trial_idx: int
    task_dir: Path
    trial_dir: Path
    env_state: EnvironmentState
    adapter_env: Any
    tool_schemas: list[dict[str, Any]]
    tool_executor: DockerRunnerAdapter
    user_tool_schemas: list[dict[str, Any]]
    user_tool_executor: DockerRunnerAdapter | None


@dataclass
class ConductorCallLog:
    """Records what an :class:`InMemoryConductor` was asked to do.

    Tests assert on this directly instead of mocking the conductor's
    method. Each entry captures the trial-identifying inputs.
    """

    runs: list[dict[str, Any]] = field(default_factory=list)


def _build_probe_stats(probe: RateLimitProbeConfig) -> RateLimitProbeStats | None:
    """The trial's probe accumulator, or ``None`` when the mode is off.

    ``None`` is what keeps a normal run's ``Metrics`` at its defaults: the
    runner treats the accumulator's presence as the mode flag, and every
    recording site is gated on it. The bucketing knobs come from the same config
    block that armed the mode, so a run leg's window grid is declared in one
    place — which matters because simultaneous legs must share it to be summable
    (see :meth:`RateLimitProbeStats.bucket_start`).
    """
    if not probe.enabled:
        return None
    return RateLimitProbeStats(
        bucket_width_s=probe.bucket_width_s,
        max_buckets=probe.max_buckets,
    )


def _default_success_trajectory(task_id: str, trial_idx: int) -> Trajectory:
    """Build a minimal completed-with-pass trajectory for in-memory tests."""
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_idx,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        metrics=Metrics(),
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(),
            reasons="synthetic-success",
        ),
    )


class InMemoryConductor:
    """Non-trial-executing :class:`Conductor` implementation.

    Records every ``run()`` invocation on :attr:`call_log` and returns a
    :class:`TrialResult` produced by ``trajectory_factory``. The default
    factory returns a synthetic-success trajectory; tests can supply a
    custom factory to drive failure / timeout / stuck scenarios.

    Useful for orchestrator-level tests that need to assert scheduling
    or retry behaviour without spinning up Docker or an LLM.
    """

    def __init__(
        self,
        trajectory_factory: Callable[[str, int], Trajectory] | None = None,
    ) -> None:
        self.call_log = ConductorCallLog()
        self._factory = trajectory_factory or _default_success_trajectory

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        # ``spec.trial_id`` is canonical (``"{task_id}:{trial_idx}"``); derive
        # ``trial_idx`` from it so the call log entry shape matches what tests
        # established under the pre-reshape signature.
        trial_idx = int(spec.trial_id.rsplit(":", 1)[1])
        self.call_log.runs.append(
            {
                "trial_id": spec.trial_id,
                "task_id": task_config.task_id,
                "trial_idx": trial_idx,
                "attempt_id": spec.attempt_id,
                "worker_id": spec.worker_id,
            }
        )
        trajectory = self._factory(task_config.task_id, trial_idx)
        return TrialResult.from_trajectory(
            trial_id=spec.trial_id, trajectory=trajectory, worker_id=spec.worker_id
        )


# ---------------------------------------------------------------------------
# InProcessConductor — production implementation
# ---------------------------------------------------------------------------


class InProcessConductor:
    """Production :class:`Conductor`. Runs each trial in the orchestrator
    process.

    The constructor kwargs are a 1:1 match for :class:`ConductorContext`
    fields — the orchestrator unpacks the context via ``**vars(ctx)`` on
    the default-factory path, so field-name / kwarg-name parity between
    the two is a load-bearing contract.

    Captures the orchestrator's per-run dependencies (adapter, artifact
    writer, config, logger, verbose / strict flags, agent client,
    docker runtime, output directory, request limiter) at construction
    time. :meth:`run` drives one trial end-to-end: environment setup,
    runner registration, agent loop, grading, artifact write.
    """

    supports_rate_limit_probe = True
    """This conductor wires every part of rate-limit probe mode.

    All three: the simulator-side probe (``for_simulator()`` onto the
    ``UserSimulator``), the per-task effective-budget re-check after the
    ``min(task trial_seconds, run episode_s)`` clamp, and the per-trial
    :class:`RateLimitProbeStats` accumulator. See :class:`Conductor` for why the
    flag is an opt-in read by name rather than a Protocol member.
    """

    def __init__(
        self,
        *,
        adapter: BaseAdapter,
        artifact_writer: TrialArtifactWriter,
        config: RunConfig,
        logger: StructuredLogger,
        verbose: bool = False,
        strict: bool = False,
        agent_client: LLMClient,
        runtime_backend: RuntimeBackend,
        trial_grader: TrialGrader,
        output_dir: Path,
        request_limiter: GlobalRateLimiter | None = None,
        events: RunDisplayEvents = _NULL_EVENTS,
    ) -> None:
        self.adapter = adapter
        self._artifact_writer = artifact_writer
        self.config = config
        self.logger = logger
        self.verbose = verbose
        self.strict = strict
        self.agent_client = agent_client
        self.runtime_backend = runtime_backend
        self.trial_grader = trial_grader
        self.output_dir = output_dir
        self.request_limiter = request_limiter
        self.events = events

    def run(
        self,
        spec: TrialSpec,
        task_config: TaskConfig,
    ) -> TrialResult:
        """Run a single trial with environment state and grading.

        ``spec`` carries trial identity, model configs, env endpoints,
        retry / worker metadata. ``task_config`` is the orchestrator-side
        rich task type (initial state, tool configs, user-simulator mode);
        the runner-side projection lives on ``spec.task``.

        The body delegates to five phase methods executed in order —
        :meth:`_setup_trial`, :meth:`_run_agent_loop`,
        :meth:`_capture_final_state`, :meth:`_grade`,
        :meth:`_write_artifacts`. Each phase owns one responsibility;
        this method is the thin coordinator.
        """
        setup = self._setup_trial(spec, task_config)
        trajectory, runner, system_prompt = self._run_agent_loop(spec, task_config, setup)
        self._capture_final_state(spec, setup, trajectory)
        self._grade(spec, task_config, setup, trajectory, runner, system_prompt)
        self._write_artifacts(spec, task_config, setup, trajectory, runner)
        return TrialResult.from_trajectory(
            trial_id=setup.trial_id, trajectory=trajectory, worker_id=spec.worker_id
        )

    def _setup_trial(
        self,
        spec: TrialSpec,
        task_config: TaskConfig,
    ) -> _TrialSetup:
        """Prepare the trial's environment state and register it with the runner.

        Runs before the agent loop. Builds the task directory, hydrates
        the :class:`EnvironmentState`, executes any declared
        ``initialization_actions`` via the task's MCP server, creates
        the trial output directory, resolves the adapter's environment
        snapshot, and issues the ``register_trial`` RPC — returning
        everything downstream phases need.
        """
        task = task_config
        trial_idx = int(spec.trial_id.rsplit(":", 1)[1])
        trial_id = f"{task.task_id}:{trial_idx}"

        task_dir = self.adapter.get_task_dir(task.task_id)

        env_state = EnvironmentState(task_dir, task.initial_state)
        env_state.hydrate()

        if task.initial_state.initialization_actions:
            init_actions = [
                action.model_dump() for action in task.initial_state.initialization_actions
            ]
            self.logger.debug("Executing initialization actions", count=len(init_actions))

            mcp_server_ref = task.tools.agent.get("mcp_server")
            if mcp_server_ref:
                mcp_server_path = task_dir / mcp_server_ref
                if mcp_server_path.exists():
                    import importlib.util

                    module_spec = importlib.util.spec_from_file_location(
                        "mcp_server_init", mcp_server_path
                    )
                    if module_spec and module_spec.loader:
                        mcp_module_init = importlib.util.module_from_spec(module_spec)
                        module_spec.loader.exec_module(mcp_module_init)

                        if hasattr(mcp_module_init, "set_data"):
                            mcp_module_init.set_data(env_state.get_db())

                        for action in init_actions:
                            env_type = action.get("env_type")
                            func_name = action.get("func_name")
                            arguments = action.get("arguments", {})

                            self.logger.debug(
                                "Executing initialization action",
                                env_type=env_type,
                                func_name=func_name,
                                arguments=arguments,
                            )

                            try:
                                if hasattr(mcp_module_init, "invoke_environment_action"):
                                    result = mcp_module_init.invoke_environment_action(
                                        env_type, func_name, **arguments
                                    )
                                else:
                                    result = mcp_module_init.invoke_tool(func_name, **arguments)
                                self.logger.debug("Init action completed", result=str(result)[:100])
                            except Exception as e:
                                self.logger.warning(
                                    "Init action failed", func_name=func_name, error=str(e)
                                )

                        if hasattr(mcp_module_init, "get_data"):
                            updated_state = mcp_module_init.get_data()
                            if updated_state:
                                env_state.db_state = updated_state
                                env_state._normalize_db_state()
                                self.logger.debug("Retrieved updated state after initialization")

        # Create trial directory early for video recording
        trial_dir = self.output_dir / "trials" / task.task_id / str(trial_idx)
        trial_dir.mkdir(parents=True, exist_ok=True)

        adapter_env = self.adapter.create_environment(task.task_id)

        # Sync adapter environment data to env_state for Tau tasks — the adapter
        # data appears in env.yaml and is available for grading.
        if adapter_env.data and not isinstance(self.adapter, NativeAdapter):
            env_state.db_state = adapter_env.data
            env_state._normalize_db_state()
            self.logger.debug(
                "Synced adapter env data to env_state",
                tables_count=(len(adapter_env.data) if isinstance(adapter_env.data, dict) else 0),
                tables_sample=(
                    list(adapter_env.data.keys())[:5]
                    if isinstance(adapter_env.data, dict)
                    else "non-dict"
                ),
            )

        from tolokaforge.tools.registry import sanitize_schema_properties

        tool_executor = DockerRunnerAdapter(runtime=self.runtime_backend, trial_id=trial_id)

        # The spec is the single source of truth for the timeout; the proto
        # field is filled from it so the two cannot diverge silently. The
        # adapter-registry guard runs orchestrator-side before the spec is
        # constructed, so the runner reads ``spec.task`` directly without
        # re-resolving the adapter here. Registration goes straight to the
        # runtime backend (ADR-0013); the tool executor is only used for the
        # runner's ``execute()`` path.
        register_result = self.runtime_backend.register_trial(
            trial_id=trial_id,
            # ``environment_manifest`` describes HOW the orchestrator
            # materialises the trial's substrate; the runner runs INSIDE
            # that substrate and has no need for the manifest. Excluded
            # from the wire so the runner-side ``TrialSpec`` validator
            # doesn't try to re-validate a ``compose_file`` path that
            # was resolved on the orchestrator's local filesystem.
            trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
            default_tool_timeout_s=spec.default_tool_timeout_s or DEFAULT_TOOL_TIMEOUT_S,
        )
        if not register_result["success"]:
            error = register_result.get("error", "Unknown error")
            raise RuntimeError(
                f"Failed to register trial with executor for trial {trial_id}: {error}"
            )

        # Tool schemas from register_trial (converted to OpenAI format).
        # Sanitise property names to match LLM API requirements (^[a-zA-Z0-9_.-]+$).
        declared_schemas = [
            {
                "type": "function",
                "function": {
                    "name": ts["name"],
                    "description": ts["description"],
                    "parameters": sanitize_schema_properties(ts["parameters"]),
                },
            }
            for ts in register_result["tool_schemas"]
        ]

        # ``tool_schemas`` is one list carrying both actors' surfaces, agent
        # slice first, partitioned at ``num_agent_tools`` (RegisterTrialResponse,
        # ``runner.proto``). Offering the whole list to the agent would advertise
        # tools the runner refuses to execute under ``executor="agent"``.
        num_agent_tools = register_result["num_agent_tools"]
        tool_schemas = declared_schemas[:num_agent_tools]
        user_tool_schemas = declared_schemas[num_agent_tools:]

        user_tool_executor = (
            DockerRunnerAdapter(runtime=self.runtime_backend, trial_id=trial_id, executor="user")
            if user_tool_schemas
            else None
        )

        self.logger.info(
            "Docker runtime: Registered trial",
            trial_id=trial_id,
            tool_count=num_agent_tools,
            user_tool_count=len(user_tool_schemas),
        )

        return _TrialSetup(
            trial_id=trial_id,
            trial_idx=trial_idx,
            task_dir=task_dir,
            trial_dir=trial_dir,
            env_state=env_state,
            adapter_env=adapter_env,
            tool_schemas=tool_schemas,
            tool_executor=tool_executor,
            user_tool_schemas=user_tool_schemas,
            user_tool_executor=user_tool_executor,
        )

    def _run_agent_loop(
        self,
        spec: TrialSpec,
        task_config: TaskConfig,
        setup: _TrialSetup,
    ) -> tuple[Trajectory, TrialRunner, str]:
        """Build the user simulator, stuck detector, system prompt, and
        :class:`TrialRunner`, then execute the agent ↔ user-simulator loop.

        Returns the produced :class:`Trajectory`, the runner instance (used
        downstream for prompt/logger access during artifact write), and the
        system prompt string (used by :meth:`_grade` when the runner has
        not yet populated its ``effective_system_prompt``).
        """
        task = task_config
        user_config = spec.user_model_config

        # ``interaction_mode='agent_only'`` runs the agent as a monologue —
        # the turn loop never dispatches a user actor, so constructing a
        # simulator here would sink the LLM budget its scripted / persona /
        # backstory carry with it into a component the runner never wakes.
        # The runner accepts ``user_simulator=None`` under this mode; the
        # ``AgentOnlyTurnPolicy`` factory ignores ``TurnPolicyContext.user_simulator``.
        rate_limit_probe = self.config.orchestrator.rate_limit_probe
        user_simulator: UserSimulator | None
        if task.interaction_mode == "conversational":
            sim = task.resolve_user_simulator()
            user_llm_config = user_config if sim.mode == "llm" else None
            # The simulator hits the same provider quota as the agent, so a probe
            # run has to cover it too — otherwise a simulator 429 kills the trial
            # the agent-side probe was keeping alive. It gets the shorter
            # simulator-scoped per-call budget: its throughput is not what the probe
            # measures, and both budgets are spent inside one uninterruptible turn.
            user_simulator = UserSimulator(
                mode=sim.mode,
                llm_config=user_llm_config,
                persona=sim.persona,
                backstory=sim.backstory,
                scripted_flow=sim.scripted_flow,
                tool_schemas=setup.user_tool_schemas or None,
                rate_limit_probe=rate_limit_probe.for_simulator(),
            )
        else:
            user_simulator = None

        # Task-scope stuck_heuristics is canonical (populated by the M2
        # loader's per-task merge from ``project.task_defaults``); the
        # run-side ``OrchestratorConfig.stuck_heuristics`` is deprecated
        # and only used as a fallback when the task declared nothing.
        stuck_cfg = (
            task.stuck_heuristics
            if task.stuck_heuristics is not None
            else self.config.orchestrator.stuck_heuristics
        )
        stuck_detector = None
        if stuck_cfg.enabled:
            stuck_detector = StuckDetector(
                max_repeated_tool_calls=stuck_cfg.max_repeated_tool_calls,
                max_idle_turns=stuck_cfg.max_idle_turns,
            )

        system_prompt = self._build_system_prompt(task, setup.tool_schemas, setup.task_dir)

        max_turns = resolve_max_turns(task.max_turns, self.config.orchestrator.max_turns)

        # Scale turn budget for complex multi-app mobile tasks only when task max_turns
        # is not explicitly pinned.
        if task.max_turns is None:
            mobile_cfg = task.tools.agent.get("mobile", {})
            mobile_apps = mobile_cfg.get("apps", {}) if isinstance(mobile_cfg, dict) else {}
            if isinstance(mobile_apps, dict):
                app_count = len(mobile_apps)
                if app_count >= 5:
                    max_turns = max(max_turns, 90)
                elif app_count == 4:
                    max_turns = max(max_turns, 75)

        # Effective per-trial timeouts = min(task-scope, run-level cap).
        # Task-side field names (``trial_seconds`` / ``tool_call_seconds``)
        # map to the run-side legacy names (``episode_s`` / ``turn_s``);
        # the name reconciliation is a follow-up in the cleanup
        # milestone.
        run_turn_s = self.config.orchestrator.timeouts.turn_s
        run_episode_s = self.config.orchestrator.timeouts.episode_s
        if task.timeouts is not None:
            turn_timeout_s = min(task.timeouts.tool_call_seconds, run_turn_s)
            episode_timeout_s = min(task.timeouts.trial_seconds, run_episode_s)
        else:
            turn_timeout_s = run_turn_s
            episode_timeout_s = run_episode_s

        # Checked against the post-clamp value: a pack declaring trial_seconds
        # shrinks the ceiling the probe's per-call budget has to fit inside.
        validate_rate_limit_probe_budget(
            rate_limit_probe,
            episode_timeout_s,
            source=f"task {task.task_id}",
        )

        runner = TrialRunner(
            task_id=task.task_id,
            trial_index=setup.trial_idx,
            agent_client=self.agent_client,
            user_simulator=user_simulator,
            tool_executor=setup.tool_executor,
            tool_schemas=setup.tool_schemas,
            max_turns=max_turns,
            turn_timeout_s=turn_timeout_s,
            episode_timeout_s=episode_timeout_s,
            stuck_detector=stuck_detector,
            user_tool_executor=setup.user_tool_executor,
            request_limiter=self.request_limiter,
            verbose=self.verbose,
            strict=self.strict,
            events=self.events,
            probe_stats=_build_probe_stats(rate_limit_probe),
            interaction_mode=task.interaction_mode,
        )

        # Use initial_user_message if provided (e.g., tool-use style tasks).
        # Otherwise use task.description which the user simulator interprets (e.g., TAU tasks).
        initial_message = task.initial_user_message if task.initial_user_message else ""
        trajectory = runner.run(system_prompt, initial_message)

        return trajectory, runner, system_prompt

    def _capture_final_state(
        self,
        spec: TrialSpec,
        setup: _TrialSetup,
        trajectory: Trajectory,
    ) -> None:
        """Sync the trial's final environment state from the Runner DB
        service and stash it on ``trajectory.final_env_state``.

        The Runner's ``GetState`` RPC syncs the subprocess state to
        db-service before reading, so this read covers every adapter.
        ``adapter_env.data`` (from :meth:`BaseAdapter.create_environment`)
        is a snapshot from before the trial ran; it is used as a fallback
        only when the Runner-side read fails.

        When the trial's task carries an ``environment_manifest`` (a
        Project-layer / multi-container substrate), the resolved
        environment identity is recorded under the ``environment`` key so a
        post-mortem can read which services, images, DSNs, and mounts backed
        the trial. Manifest-less trials keep the JSON-DB-only shape.
        """
        runner_state: dict[str, Any] | None = None
        try:
            state_result = self.runtime_backend.get_state(setup.trial_id)
            if state_result.get("success") and state_result.get("state_json"):
                import json as _json

                decoded = _json.loads(state_result["state_json"])
                if isinstance(decoded, dict) and decoded:
                    runner_state = decoded
                else:
                    self.logger.debug("Runner DB state empty, falling back to adapter env data")
            else:
                self.logger.debug(
                    "Failed to fetch Runner DB state, falling back to adapter env data",
                    error=state_result.get("error"),
                )
        except Exception as e:
            self.logger.warning(
                "Could not fetch state from Runner, using adapter env data",
                error=str(e),
            )

        if runner_state is not None:
            setup.env_state.db_state = runner_state
            setup.env_state._normalize_db_state()
            self.logger.debug(
                "Synced final state from Runner DB service",
                tables_count=len(runner_state),
                tables_sample=list(runner_state.keys())[:5],
            )
        elif setup.adapter_env.data:
            setup.env_state.db_state = setup.adapter_env.data
            setup.env_state._normalize_db_state()

        final_state = setup.env_state.get_final_state()
        # Pass agent_visible_dir so the agentic judge can read files from disk.
        final_state["agent_visible_dir"] = str(setup.env_state.agent_visible_dir)

        manifest = spec.task.environment_manifest
        if manifest is not None:
            final_state["environment"] = describe_environment_identity(manifest).model_dump(
                mode="json"
            )

        trajectory.final_env_state = final_state

    def _grade(
        self,
        spec: TrialSpec,
        task_config: TaskConfig,
        setup: _TrialSetup,
        trajectory: Trajectory,
        runner: TrialRunner,
        system_prompt: str,
    ) -> None:
        """Compute the trial's :class:`Grade` via the injected
        :class:`TrialGrader` and assign it to ``trajectory.grade``.

        Grading strategy — including auto-fail on ``ERROR`` / ``TIMEOUT``
        / ``STUCK_DETECTED``, the ``None`` verdict for a trial the
        infrastructure aborted, and the runner-RPC path for a normal
        completion — lives inside the grader. This phase is the trigger
        (per CLOUD_RUNTIME §6.3): assemble the agent's post-policy
        system prompt, delegate.

        An ungraded trial scores nothing: no ``judgment_scored`` event
        fires, because there is no judgment to report.

        A :class:`~tolokaforge.core.trial_grader.GradingFailedError` is caught
        and recorded on ``trajectory.grading_error``, leaving ``grade`` unset.
        The trial then completes its normal path — ``termination_reason`` and
        ``status`` still say how the trial itself ended, the bundle is written,
        and the run counts the attempt with the cause recoverable from disk.
        """
        agent_system_prompt = runner.effective_system_prompt or system_prompt
        try:
            grade = self.trial_grader.grade(spec, trajectory, agent_system_prompt)
        except GradingFailedError as e:
            trajectory.grading_error = str(e)
            self.logger.error(
                "Trial could not be graded",
                task_id=task_config.task_id,
                trial_index=setup.trial_idx,
                error=str(e),
            )
            return
        trajectory.grade = grade
        if grade is None:
            self.logger.info(
                "Trial not graded",
                task_id=task_config.task_id,
                trial_index=setup.trial_idx,
                termination_reason=(
                    trajectory.termination_reason.value if trajectory.termination_reason else None
                ),
            )
            return
        self.events.judgment_scored(
            trial_id=setup.trial_id,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        self.logger.info(
            "Trial graded",
            task_id=task_config.task_id,
            trial_index=setup.trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )

    def _write_artifacts(
        self,
        spec: TrialSpec,
        task_config: TaskConfig,
        setup: _TrialSetup,
        trajectory: Trajectory,
        runner: TrialRunner,
    ) -> None:
        """Persist the trial bundle (trajectory, prompts, tools schemas,
        task config snapshot) via the shared
        :class:`TrialArtifactWriter`.

        The trial directory was created during :meth:`_setup_trial` for
        video recording. This phase writes the remaining artifacts
        through the composed writer; the per-trial writer instance is
        cached inside the ``FileArtifactWriter`` keyed on ``trial_dir``.
        """
        task = task_config
        writer = self._artifact_writer
        grading_config = self.adapter.get_grading_config(task.task_id)

        # Persist the post-policy tool list inside the trial bundle as
        # ``tools_schemas.yaml`` — the trial's declared tool surface, agent
        # slice then user slice, passed through the capabilities' schema
        # sanitizer so the agent's half reproduces what the provider saw. Both
        # slices are recorded because the replay authoring gate rebuilds the
        # trial's whole ``ToolInventory`` from this one file
        # (``grading.trace_replay.tool_inventory_from_bundle``): an agent-only
        # record would leave a matcher naming a user tool unblessable. Both slices
        # go through the *agent's* provider sanitizer, so the user slice is recorded
        # in the agent provider's dialect rather than in whatever the simulator's own
        # provider was handed — the file records one trial's declared surface, not two
        # providers' wire payloads.
        agent_config = self.agent_client.config
        sanitized = self.agent_client.capabilities.schema_sanitizer.sanitize(
            setup.tool_schemas + setup.user_tool_schemas
        )
        writer.write_tools_schemas(setup.trial_dir, sanitized)

        # Persist the agent's effective (post-policy) system prompt and
        # the user simulator's system prompt as ``prompts.yaml`` — kept
        # separate from ``trajectory.yaml`` so the message trace stays
        # easy to scan.
        writer.write_prompts(
            setup.trial_dir,
            agent_prompt=runner.effective_system_prompt,
            user_prompt=runner.user_system_prompt,
        )

        task_config_dict = {
            "task_id": task.task_id,
            "trial_index": setup.trial_idx,
            "category": task.category,
            "description": task.description,
            "grading_config": grading_config.model_dump(mode="json") if grading_config else {},
            "tools": task.tools.model_dump(mode="json"),
            "policies": task.policies,
            "model_config": self._serialize_model_config(
                agent_config=agent_config,
                user_config=spec.user_model_config,
                judge_config=spec.judge_model_config,
            ),
        }

        writer.write_trial_bundle(
            setup.trial_dir,
            trajectory,
            task_config_dict,
            trajectory.final_env_state,
            runner.logger,
        )

        self.logger.info(
            "Trial output saved",
            task_id=task.task_id,
            trial_index=setup.trial_idx,
            output_dir=str(setup.trial_dir),
        )

    def _serialize_model_config(
        self,
        agent_config: ModelConfig | None = None,
        user_config: ModelConfig | None = None,
        judge_config: ModelConfig | None = None,
    ) -> dict[str, Any]:
        """Serialize model config for trial output.

        Each role's block carries a ``resolved:`` sub-block with the preset
        fingerprint (effective preset name plus the six registered-policy
        names from :func:`tolokaforge.core.llm.presets.resolve_policy_names`).
        Analytics tools diff this across runs to detect config drift without
        having to re-match the preset YAML.

        The ``judge`` role (the run-level read-only rubric judge) is recorded the
        same way as ``agent`` / ``user`` so every grade bundle records which judge
        produced it — the judge model is a mutable per-run knob. ``judge`` is
        ``null`` when the run configures no judge.

        When callers (e.g. tests) omit *agent_config* / *user_config* /
        *judge_config*, the method falls back to the values declared on
        ``self.config.models``. The ``resolved:`` block is computed against the
        same identity.
        """
        result: dict[str, Any] = {}

        resolved_agent_config = agent_config
        resolved_user_config = user_config
        resolved_judge_config = judge_config

        models = self.config.models
        if isinstance(models, dict):
            agent = models.get("agent", {})
            user = models.get("user")
            judge = models.get("judge")
            result["agent"] = agent if isinstance(agent, dict) else agent.model_dump(mode="json")
            result["user"] = (
                (user if isinstance(user, dict) else user.model_dump(mode="json")) if user else None
            )
            result["judge"] = (
                (judge if isinstance(judge, dict) else judge.model_dump(mode="json"))
                if judge
                else None
            )
            if resolved_agent_config is None and not isinstance(agent, dict):
                resolved_agent_config = agent
            if resolved_user_config is None and user is not None and not isinstance(user, dict):
                resolved_user_config = user
            if resolved_judge_config is None and judge is not None and not isinstance(judge, dict):
                resolved_judge_config = judge
        else:
            result["agent"] = models.agent.model_dump(mode="json")
            result["user"] = models.user.model_dump(mode="json") if models.user else None
            judge = getattr(models, "judge", None)
            result["judge"] = judge.model_dump(mode="json") if judge else None
            if resolved_agent_config is None:
                resolved_agent_config = models.agent
            if resolved_user_config is None and models.user is not None:
                resolved_user_config = models.user
            if resolved_judge_config is None and judge is not None:
                resolved_judge_config = judge

        # Resolved fingerprint per role.
        if resolved_agent_config is not None:
            result["agent"]["resolved"] = _build_resolved_block(resolved_agent_config)
        if resolved_user_config is not None and result.get("user"):
            result["user"]["resolved"] = _build_resolved_block(resolved_user_config)
        if resolved_judge_config is not None and result.get("judge"):
            result["judge"]["resolved"] = _build_resolved_block(resolved_judge_config)

        return result

    def _build_system_prompt(
        self, task: TaskConfig, tool_schemas: list[dict[str, Any]], task_dir: Path
    ) -> str:
        return build_system_prompt(task=task, task_dir=task_dir, adapter=self.adapter)


def in_process_conductor_factory(ctx: ConductorContext) -> InProcessConductor:
    """Build an :class:`InProcessConductor` from a conductor context.

    :class:`InProcessConductor`'s keyword-only constructor mirrors
    :class:`ConductorContext`'s fields 1:1, so the context unpacks directly.
    """
    return InProcessConductor(**vars(ctx))


def in_memory_conductor_factory(ctx: ConductorContext) -> InMemoryConductor:
    """Build an :class:`InMemoryConductor` from a conductor context.

    The recording fixture executes no trial, so it reads none of the
    per-run dependencies the context carries.
    """
    return InMemoryConductor()
