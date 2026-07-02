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
    RunConfig,
    TaskConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import TrialArtifactWriter
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, TrialResult, TrialSpec
from tolokaforge.core.trial_grader import TrialGrader

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger

__all__ = [
    "Conductor",
    "ConductorCallLog",
    "ConductorContext",
    "ConductorFactory",
    "InMemoryConductor",
    "InProcessConductor",
]


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


# ---------------------------------------------------------------------------
# Module-level helpers (called by InProcessConductor's body)
# ---------------------------------------------------------------------------


def _build_resolved_block(model_config: ModelConfig) -> dict[str, Any]:
    """Return the ``resolved:`` block for a :class:`ModelConfig`.

    Shape: ``{"effective_preset": ..., "schema_sanitizer": ..., ...}``.
    See :func:`tolokaforge.core.llm.presets.resolve_policy_names` for the
    six policy slots included in the fingerprint. Analytics tools diff this
    across runs to detect preset / capability drift.
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
    """

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        """Execute one trial end-to-end."""
        ...


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


@dataclass
class ConductorCallLog:
    """Records what an :class:`InMemoryConductor` was asked to do.

    Tests assert on this directly instead of mocking the conductor's
    method. Each entry captures the trial-identifying inputs.
    """

    runs: list[dict[str, Any]] = field(default_factory=list)


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

    Captures the orchestrator's per-run dependencies (adapter, artifact
    writer, config, logger, verbose / strict flags, agent client,
    docker runtime, output directory, request limiter) at construction
    time. :meth:`run` drives one trial end-to-end: environment setup,
    runner registration, agent loop, grading, artifact write.
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
        self._capture_final_state(setup, trajectory)
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
        tool_schemas = [
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

        self.logger.info(
            "Docker runtime: Registered trial",
            trial_id=trial_id,
            tool_count=register_result.get("num_agent_tools", len(tool_schemas)),
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

        # User tool executor is not used in Docker mode (Runner handles tools).
        user_tool_executor = None
        user_tool_schemas: list[dict[str, Any]] = []

        user_llm_config = user_config if task.user_simulator.mode == "llm" else None
        user_simulator = UserSimulator(
            mode=task.user_simulator.mode,
            llm_config=user_llm_config,
            persona=task.user_simulator.persona,
            backstory=task.user_simulator.backstory,
            scripted_flow=task.user_simulator.scripted_flow,
            tool_schemas=user_tool_schemas if user_tool_executor else None,
        )

        stuck_detector = None
        if self.config.orchestrator.stuck_heuristics.enabled:
            stuck_detector = StuckDetector(
                max_repeated_tool_calls=self.config.orchestrator.stuck_heuristics.max_repeated_tool_calls,
                max_idle_turns=self.config.orchestrator.stuck_heuristics.max_idle_turns,
            )

        system_prompt = self._build_system_prompt(task, setup.tool_schemas, setup.task_dir)

        # Respect per-task max_turns when provided; fall back to orchestrator default.
        max_turns = (
            task.max_turns if task.max_turns is not None else self.config.orchestrator.max_turns
        )

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

        runner = TrialRunner(
            task_id=task.task_id,
            trial_index=setup.trial_idx,
            agent_client=self.agent_client,
            user_simulator=user_simulator,
            tool_executor=setup.tool_executor,
            tool_schemas=setup.tool_schemas,
            max_turns=max_turns,
            turn_timeout_s=self.config.orchestrator.timeouts.turn_s,
            episode_timeout_s=self.config.orchestrator.timeouts.episode_s,
            stuck_detector=stuck_detector,
            user_tool_executor=user_tool_executor,
            request_limiter=self.request_limiter,
            verbose=self.verbose,
            strict=self.strict,
        )

        # Use initial_user_message if provided (e.g., tool-use style tasks).
        # Otherwise use task.description which the user simulator interprets (e.g., TAU tasks).
        initial_message = task.initial_user_message if task.initial_user_message else ""
        trajectory = runner.run(system_prompt, initial_message)

        return trajectory, runner, system_prompt

    def _capture_final_state(
        self,
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
        / ``STUCK_DETECTED`` and the runner-RPC path for a normal
        completion — lives inside the grader. This phase is the trigger
        (per CLOUD_RUNTIME §6.3): assemble the agent's post-policy
        system prompt, delegate.
        """
        agent_system_prompt = runner.effective_system_prompt or system_prompt
        trajectory.grade = self.trial_grader.grade(spec, trajectory, agent_system_prompt)
        self.logger.info(
            "Trial graded",
            task_id=task_config.task_id,
            trial_index=setup.trial_idx,
            score=trajectory.grade.score,
            binary_pass=trajectory.grade.binary_pass,
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
        # ``tools_schemas.yaml`` — the exact list handed to the agent's
        # :class:`LLMClient`, passed through the capabilities' schema
        # sanitizer so the file reproduces what the provider saw.
        agent_config = self.agent_client.config
        sanitized = self.agent_client.capabilities.schema_sanitizer.sanitize(setup.tool_schemas)
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
        """Build system prompt for task

        Priority:
        1. task.policies['agent_system_prompt'] (inline string)
        2. task.system_prompt == "__adapter__" -> use adapter.get_system_prompt()
        3. task.system_prompt (file path)
        4. main_policy.md pattern (legacy)
        5. Minimal default

        Note: Tool schemas should NOT be in system prompt - they're sent via function calling API
        """

        # 1. Check for inline agent_system_prompt in policies
        if "agent_system_prompt" in task.policies:
            return task.policies["agent_system_prompt"]

        # 2. Check for adapter-based system prompt
        if task.system_prompt == "__adapter__" and self.adapter:
            adapter_prompt = self.adapter.get_system_prompt(task.task_id)
            if adapter_prompt:
                AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy.
"""

                return f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{adapter_prompt}
</policy>"""

        # 3. Check for system_prompt file path
        if task.system_prompt and task.system_prompt != "__adapter__":
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                return system_prompt_path.read_text()

        # 3. Check for main_policy.md + additional system prompt file structure (legacy)
        main_policy_path = task_dir.parent / "main_policy.md"  # One level up from task dir
        if not main_policy_path.exists():
            main_policy_path = task_dir / "main_policy.md"  # Try task dir itself

        if main_policy_path.exists() and task.system_prompt:
            # Load main policy
            with open(main_policy_path) as f:
                main_policy = f.read()

            # Load additional policy file
            additional_policy_path = task_dir.parent / task.system_prompt
            if not additional_policy_path.exists():
                additional_policy_path = task_dir / task.system_prompt

            if additional_policy_path.exists():
                with open(additional_policy_path) as f:
                    additional_policy = f.read()

                # Concatenate policies with XML tags
                domain_policy = (
                    "<main_policy>\n"
                    + main_policy
                    + "\n</main_policy>\n"
                    + "<tech_support_policy>\n"
                    + additional_policy
                    + "\n</tech_support_policy>"
                )
            else:
                # Fallback: just use main policy if additional policy file not found
                domain_policy = main_policy

            AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy."""

            # Tools are passed separately to LLM API, NOT in system prompt
            prompt = f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{domain_policy}
</policy>"""
            return prompt

        # Single-file system prompt
        elif task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                with open(system_prompt_path) as f:
                    domain_policy = f.read()

                AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy."""

                prompt = f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{domain_policy}
</policy>"""
                return prompt

        # 4. Minimal default fallback
        # Tool schemas are sent separately via function calling API, NOT in system prompt
        # Enrich the default prompt with task-specific context when available.
        parts = ["You are a helpful assistant."]

        # Add task guidance from policies
        guidance = task.policies.get("guidance", []) if task.policies else []
        if guidance:
            parts.append("\nGuidance:")
            for g in guidance:
                parts.append(f"- {g}")

        # Add browser URL if configured so the agent knows where to navigate
        browser_config = task.tools.agent.get("browser", {}) if task.tools else {}
        if isinstance(browser_config, dict):
            browser_url = browser_config.get("initial_url")
            if browser_url:
                parts.append(f"\nThe web portal is available at: {browser_url}")
                parts.append(
                    "Navigate to this URL to access the portal content. "
                    "Do not guess other URLs or ports."
                )

        return "\n".join(parts)
