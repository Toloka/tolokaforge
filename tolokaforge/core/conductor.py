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

import json
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
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeStatus,
    JudgeUsage,
    Metrics,
    ModelConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import TrialArtifactWriter
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, TrialResult, TrialSpec

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger

__all__ = [
    "Conductor",
    "ConductorCallLog",
    "InMemoryConductor",
    "InProcessConductor",
]


# ---------------------------------------------------------------------------
# Module-level helpers (called by InProcessConductor's body)
# ---------------------------------------------------------------------------


def _build_resolved_block(model_config: ModelConfig) -> dict[str, Any]:
    """Return the Stage 7 ``resolved:`` block for a :class:`ModelConfig`.

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


# ---------------------------------------------------------------------------
# InMemoryConductor — test fixture, records calls + returns synthetic result
# ---------------------------------------------------------------------------


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
    writer, config, logger, verbose / strict flags) at construction
    time. The orchestrator constructs one of these per ``run()`` /
    ``run_worker()`` invocation, after the adapter is loaded.

    The body of :meth:`run` is the trial-execution code that used to
    live as ``Orchestrator._run_trial``. Three helpers travel with it:
    :meth:`_build_system_prompt`, :meth:`_build_judge_messages_json`,
    :meth:`_serialize_model_config`.
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
        agent_client: LLMClient | None,
        docker_runtime: RuntimeBackend | None,
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
        self.docker_runtime = docker_runtime
        self.output_dir = output_dir
        self.request_limiter = request_limiter

    @staticmethod
    def _build_judge_messages_json(
        task: TaskConfig,  # noqa: ARG004 — kept for signature symmetry / future gating
        trajectory: Trajectory,
        agent_system_prompt: str | None,
    ) -> str | None:
        """Serialise the transcript for the runner-side grading (judge + transcript rules).

        The agent's system prompt is sent as a leading ``system`` message so the
        rubric judge can inject it as the agent's policy. The runner decides
        whether to actually run the judge (based on its own grading config) and
        narrows the input surface from there — so this always serialises the
        transcript when there is one, and returns ``None`` for an empty trace.
        """
        if not trajectory.messages and not agent_system_prompt:
            return None

        messages: list[dict[str, Any]] = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        for msg in trajectory.messages:
            entry: dict[str, Any] = {
                "role": msg.role.value,
                "content": msg.content or "",
            }
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)
        return json.dumps(messages)

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
        """
        # Compatibility shims — re-bind the legacy parameter names from
        # ``spec`` / ``task_config`` / ``self`` so the verbatim
        # ``_run_trial`` body below keeps reading the names it was written
        # against. Eliminating these is the body-refactor follow-up.
        task = task_config
        trial_idx = int(spec.trial_id.rsplit(":", 1)[1])
        agent_client = self.agent_client
        user_config = spec.user_model_config
        output_dir = self.output_dir
        docker_runtime = self.docker_runtime
        request_limiter = self.request_limiter
        worker_id = spec.worker_id
        judge_config = spec.judge_model_config

        # Get task directory from adapter (supports both native and tau)
        task_dir = self.adapter.get_task_dir(task.task_id)

        if agent_client is None:
            raise ValueError("Agent client must be provided for trial execution")

        # Per-trial DB namespace for parallel isolation
        db_ns = f"{task.task_id}_{trial_idx}"

        # Initialize environment state
        env_state = EnvironmentState(task_dir, task.initial_state)
        env_state.hydrate()

        # Initialize json-db service with initial state (namespaced for parallel isolation)
        # Skip when Docker runtime is active — the Runner's InMemoryDatabase is the
        # source of truth, and the standalone json-db service is not started.
        if env_state.db_state and not docker_runtime:
            try:
                import httpx

                json_db_reset_urls = [
                    f"http://json-db:8000/ns/{db_ns}",
                    f"http://localhost:8000/ns/{db_ns}",
                ]

                initialized = False
                for reset_url in json_db_reset_urls:
                    try:
                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(f"{reset_url}/reset", json=env_state.db_state)
                        if response.status_code == 200:
                            self.logger.debug(
                                "Initialized json-db service",
                                url=reset_url,
                                namespace=db_ns,
                                tables=len(env_state.db_state),
                            )
                            initialized = True
                            break
                    except Exception:
                        continue

                if not initialized:
                    self.logger.warning("Failed to initialize json-db service")
            except Exception as e:
                self.logger.warning("Could not initialize json-db service", error=str(e))

        # Initialize RAG index if corpus is configured
        if env_state.rag_corpus_dir:
            try:
                import httpx

                rag_service_urls = ["http://rag-service:8001", "http://localhost:8001"]

                corpus_path = str(env_state.rag_corpus_dir)
                container_corpus_path = None
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    repo_tolokaforge = repo_root / "tolokaforge"
                    if env_state.rag_corpus_dir.is_relative_to(repo_tolokaforge):
                        rel_path = env_state.rag_corpus_dir.relative_to(repo_tolokaforge)
                        container_corpus_path = str(Path("/app/tolokaforge") / rel_path)
                except Exception:
                    container_corpus_path = None
                indexed = False
                for rag_service_url in rag_service_urls:
                    try:
                        request_path = corpus_path
                        if "localhost" in rag_service_url and container_corpus_path:
                            request_path = container_corpus_path

                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(
                                f"{rag_service_url}/index",
                                json={"corpus_path": request_path},
                            )
                        if response.status_code == 200:
                            self.logger.debug(
                                "Indexed RAG corpus", path=corpus_path, url=rag_service_url
                            )
                            indexed = True
                            break
                    except Exception:
                        continue

                if not indexed:
                    self.logger.warning("Failed to index RAG corpus", path=corpus_path)
            except Exception as e:
                self.logger.warning("Could not index RAG corpus", error=str(e))

        # Execute initialization_actions to set correct starting state
        if task.initial_state.initialization_actions:
            init_actions = [
                action.model_dump() for action in task.initial_state.initialization_actions
            ]
            self.logger.debug("Executing initialization actions", count=len(init_actions))

            # Import MCP server to execute actions before trial starts
            mcp_server_ref = task.tools.agent.get("mcp_server")
            if mcp_server_ref:
                mcp_server_path = task_dir / mcp_server_ref
                if mcp_server_path.exists():
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(
                        "mcp_server_init", mcp_server_path
                    )
                    if spec and spec.loader:
                        mcp_module_init = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mcp_module_init)

                        # Sync current env state to MCP server
                        if hasattr(mcp_module_init, "set_data"):
                            mcp_module_init.set_data(env_state.get_db())

                        # Execute each initialization action
                        for action in init_actions:
                            env_type = action.get("env_type")  # "user" or "assistant"
                            func_name = action.get("func_name")
                            arguments = action.get("arguments", {})

                            self.logger.debug(
                                "Executing initialization action",
                                env_type=env_type,
                                func_name=func_name,
                                arguments=arguments,
                            )

                            try:
                                # Invoke helper/tool via MCP server
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

                        # Retrieve updated state after initialization
                        if hasattr(mcp_module_init, "get_data"):
                            updated_state = mcp_module_init.get_data()
                            if updated_state:
                                env_state.db_state = updated_state
                                env_state._normalize_db_state()
                                self.logger.debug("Retrieved updated state after initialization")

        # Create trial directory early for video recording
        trial_dir = output_dir / "trials" / task.task_id / str(trial_idx)
        trial_dir.mkdir(parents=True, exist_ok=True)

        # Load adapter environment (needed for state sync with Tau tasks)
        adapter_env = self.adapter.create_environment(task.task_id)

        # Sync adapter environment data to env_state for Tau tasks
        # This ensures adapter data appears in env.yaml and is available for grading
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

        # Docker runtime - use executor adapter for tool execution
        from tolokaforge.tools.registry import sanitize_schema_properties

        trial_id = f"{task.task_id}:{trial_idx}"

        tool_executor = DockerRunnerAdapter(
            runner_client=docker_runtime.executor_client, trial_id=trial_id
        )

        # The spec is the single source of truth for the timeout; the proto
        # field is filled from it so the two cannot diverge silently. The
        # adapter-registry guard runs orchestrator-side before the spec is
        # constructed, so the runner reads ``spec.task`` directly without
        # re-resolving the adapter here.
        register_result = tool_executor.register_trial(
            trial_spec_json=spec.model_dump_json(),
            default_tool_timeout_s=spec.default_tool_timeout_s or DEFAULT_TOOL_TIMEOUT_S,
        )
        if not register_result["success"]:
            error = register_result.get("error", "Unknown error")
            raise RuntimeError(
                f"Failed to register trial with executor for trial {trial_id}: {error}"
            )

        # Use tool schemas from register_trial result (converted to OpenAI format)
        # Sanitize property names to match LLM API requirements (^[a-zA-Z0-9_.-]+$)
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

        # User tool executor is not used in Docker mode (Runner handles tools)
        user_tool_executor = None
        user_tool_schemas: list[dict[str, Any]] = []

        # Use backstory from task configuration
        backstory = task.user_simulator.backstory

        # Create user simulator
        user_llm_config = user_config if task.user_simulator.mode == "llm" else None
        user_simulator = UserSimulator(
            mode=task.user_simulator.mode,
            llm_config=user_llm_config,
            persona=task.user_simulator.persona,
            backstory=backstory,
            scripted_flow=task.user_simulator.scripted_flow,
            tool_schemas=user_tool_schemas if user_tool_executor else None,
        )

        # Create stuck detector with configured heuristics
        stuck_detector = None
        if self.config.orchestrator.stuck_heuristics.enabled:
            stuck_detector = StuckDetector(
                max_repeated_tool_calls=self.config.orchestrator.stuck_heuristics.max_repeated_tool_calls,
                max_idle_turns=self.config.orchestrator.stuck_heuristics.max_idle_turns,
            )

        # Build system prompt
        system_prompt = self._build_system_prompt(task, tool_schemas, task_dir)

        # Respect per-task max_turns when provided. Fall back to orchestrator default.
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

        # Create runner with verbose and strict flags
        runner = TrialRunner(
            task_id=task.task_id,
            trial_index=trial_idx,
            agent_client=agent_client,
            user_simulator=user_simulator,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
            max_turns=max_turns,
            turn_timeout_s=self.config.orchestrator.timeouts.turn_s,
            episode_timeout_s=self.config.orchestrator.timeouts.episode_s,
            stuck_detector=stuck_detector,
            user_tool_executor=user_tool_executor,
            request_limiter=request_limiter,
            verbose=self.verbose,
            strict=self.strict,
        )

        # Run trial
        # Use initial_user_message if provided (e.g., tool-use style tasks)
        # Otherwise use task.description which will be interpreted by user simulator (e.g., TAU tasks)
        initial_message = task.initial_user_message if task.initial_user_message else ""
        trajectory = runner.run(system_prompt, initial_message)

        # Sync JSON DB state for native tasks (if no MCP server is used)
        # Skip when Docker runtime is active — state comes from Runner DB service.
        if (
            task.initial_state.json_db
            and not task.tools.agent.get("mcp_server")
            and not docker_runtime
        ):
            try:
                import httpx

                json_db_sync_urls = [
                    f"http://json-db:8000/ns/{db_ns}",
                    f"http://localhost:8000/ns/{db_ns}",
                ]

                synced = False
                for sync_url in json_db_sync_urls:
                    try:
                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(f"{sync_url}/query", json={"jsonpath": "$"})
                        if response.status_code == 200:
                            results = response.json().get("results", [])
                            if results:
                                env_state.db_state = results[0]
                                env_state._normalize_db_state()
                                self.logger.debug(
                                    "Synced json-db state for grading",
                                    url=sync_url,
                                    namespace=db_ns,
                                )
                                synced = True
                                break
                    except Exception:
                        continue

                if not synced:
                    self.logger.warning("Failed to sync json-db state")
            except Exception as e:
                self.logger.warning("Could not sync json-db state", error=str(e))

        # Retrieve final state from Runner DB service (source of truth in Docker mode)
        # The adapter_env.data is a snapshot from create_environment() and does NOT
        # reflect tool-execution changes made through the Runner.
        # For native MCP-server tasks the Runner's GetState RPC now syncs the
        # subprocess state to db-service before reading, so the condition no
        # longer needs to exclude NativeAdapter.
        if docker_runtime:
            try:
                state_result = docker_runtime.executor_client.get_state(trial_id)
                if state_result.get("success") and state_result.get("state_json"):
                    import json as _json

                    runner_state = _json.loads(state_result["state_json"])
                    if isinstance(runner_state, dict) and runner_state:
                        env_state.db_state = runner_state
                        env_state._normalize_db_state()
                        self.logger.debug(
                            "Synced final state from Runner DB service",
                            tables_count=len(runner_state),
                            tables_sample=list(runner_state.keys())[:5],
                        )
                    else:
                        self.logger.debug("Runner DB state empty, falling back to adapter env data")
                        if adapter_env.data:
                            env_state.db_state = adapter_env.data
                            env_state._normalize_db_state()
                else:
                    self.logger.debug(
                        "Failed to fetch Runner DB state, falling back to adapter env data",
                        error=state_result.get("error"),
                    )
                    if adapter_env.data:
                        env_state.db_state = adapter_env.data
                        env_state._normalize_db_state()
            except Exception as e:
                self.logger.warning(
                    "Could not fetch state from Runner, using adapter env data",
                    error=str(e),
                )
                if adapter_env.data:
                    env_state.db_state = adapter_env.data
                    env_state._normalize_db_state()
        elif adapter_env.data and not isinstance(self.adapter, NativeAdapter):
            # Non-Docker mode fallback — adapter is source of truth
            env_state.db_state = adapter_env.data
            env_state._normalize_db_state()
            self.logger.debug(
                "Synced final adapter env data to env_state",
                tables_count=(len(adapter_env.data) if isinstance(adapter_env.data, dict) else 0),
                tables_sample=(
                    list(adapter_env.data.keys())[:5]
                    if isinstance(adapter_env.data, dict)
                    else "non-dict"
                ),
            )

        # Capture final environment state
        final_state = env_state.get_final_state()
        # Pass agent_visible_dir so the agentic judge can read files from disk
        final_state["agent_visible_dir"] = str(env_state.agent_visible_dir)
        trajectory.final_env_state = final_state

        # Check if trial completed successfully - ERROR/TIMEOUT trials should auto-fail
        # This prevents false positives when 429 or other errors occur before any work is done
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task.task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )
        elif trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            # Stuck agents always fail — even if hash matches
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task.task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )
        else:
            # Grade trajectory via Runner's GradeTrial RPC
            # It computes golden hash via DB service
            trial_id = f"{task.task_id}:{trial_idx}"
            # Serialize the transcript + the agent's policy (its system prompt)
            # whenever there are messages; the runner owns the decision of whether
            # to actually run the rubric judge (based on its grading config).
            llm_messages_json = self._build_judge_messages_json(
                task, trajectory, runner.effective_system_prompt or system_prompt
            )
            grade_result = docker_runtime.executor_client.grade_trial(
                trial_id=trial_id, llm_messages_json=llm_messages_json
            )
            if grade_result["success"] and grade_result["grade"]:
                g = grade_result["grade"]

                # Parse state_diff from gRPC JSON for post-mortem diagnostics
                state_diff_parsed: dict[str, Any] | None = None
                if g.get("state_diff_json"):
                    try:
                        state_diff_parsed = json.loads(g["state_diff_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                criterion_results = None
                raw_criterion_results = g.get("criterion_results")
                if raw_criterion_results:
                    criterion_results = [CriterionResult(**cr) for cr in raw_criterion_results]

                # Judge accounting + audit transcript (None when no judge ran).
                judge_usage: JudgeUsage | None = None
                judge_transcript: list[dict[str, Any]] | None = None
                raw_report = g.get("judge_report")
                if raw_report:
                    judge_usage = JudgeUsage(
                        calls=raw_report.get("calls", 0),
                        prompt_tokens=raw_report.get("prompt_tokens", 0),
                        completion_tokens=raw_report.get("completion_tokens", 0),
                        reasoning_tokens=raw_report.get("reasoning_tokens", 0),
                        cost_usd=raw_report.get("cost_usd", 0.0),
                        tool_calls=raw_report.get("tool_calls", 0),
                    )
                    raw_transcript = raw_report.get("transcript_json")
                    if raw_transcript:
                        try:
                            parsed = json.loads(raw_transcript)
                            if isinstance(parsed, list):
                                judge_transcript = parsed
                        except (json.JSONDecodeError, TypeError):
                            pass

                grade = Grade(
                    binary_pass=g["binary_pass"],
                    score=g["score"],
                    components=GradeComponents(
                        state_checks=g["components"].get("state_checks", -1.0),
                        transcript_rules=g["components"].get("transcript_rules", -1.0),
                        llm_judge=g["components"].get("llm_judge", -1.0),
                        custom_checks=g["components"].get("custom_checks", -1.0),
                    ),
                    reasons=g.get("reasons", ""),
                    state_diff=state_diff_parsed,
                    criterion_results=criterion_results,
                    judge_status=JudgeStatus.from_proto(g.get("judge_status", 0)),
                    judge_usage=judge_usage,
                    judge_transcript=judge_transcript,
                )
                self.logger.info(
                    "Grading via Runner RPC",
                    task_id=task.task_id,
                    trial_index=trial_idx,
                    score=grade.score,
                    binary_pass=grade.binary_pass,
                )
            else:
                # Grading RPC failed - fail the trial
                error_msg = grade_result.get("error", "Unknown grading error")
                self.logger.error(
                    "Grading RPC failed",
                    task_id=task.task_id,
                    trial_index=trial_idx,
                    error=error_msg,
                )
                grade = Grade(
                    binary_pass=False,
                    score=0.0,
                    components=GradeComponents(state_checks=0.0),
                    reasons=f"Grading RPC failed: {error_msg}",
                )
        trajectory.grade = grade

        self.logger.info(
            "Trial graded",
            task_id=task.task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )

        # Note: Browser cleanup is handled automatically by Playwright when the process ends.
        # The video recording is finalized when the browser context closes.
        # We don't need explicit cleanup here - it was causing event loop issues.
        # The video file is already being written to the videos directory.

        # Save trial outputs through the shared :class:`FileArtifactWriter`.
        # ``trial_dir`` was already created earlier for video recording.
        writer = self._artifact_writer

        # Get grading config for output (from adapter)
        grading_config = self.adapter.get_grading_config(task.task_id)

        # Persist the post-policy tool list inside the trial bundle as
        # ``tools_schemas.yaml``. ``tool_schemas`` is the list handed to the
        # agent's :class:`LLMClient`; pushing it through the matched
        # ``schema_sanitizer`` reproduces exactly what the provider saw.
        # Self-contained per-trial: no dedup, no cross-trial state.
        agent_config = agent_client.config
        sanitized = agent_client.capabilities.schema_sanitizer.sanitize(tool_schemas)
        writer.write_tools_schemas(trial_dir, sanitized)

        # Persist the agent's effective (post-policy) system prompt and
        # the user simulator's system prompt as ``prompts.yaml`` — kept
        # separate from ``trajectory.yaml`` so the message trace stays
        # easy to scan. Both come off the runner as read-only properties
        # populated during ``run()``.
        writer.write_prompts(
            trial_dir,
            agent_prompt=runner.effective_system_prompt,
            user_prompt=runner.user_system_prompt,
        )

        # Prepare task config for output (includes model config snapshot for
        # reproducibility + Stage 7 resolved preset fingerprint).
        task_config_dict = {
            "task_id": task.task_id,
            "trial_index": trial_idx,
            "category": task.category,
            "description": task.description,
            "grading_config": grading_config.model_dump(mode="json") if grading_config else {},
            "tools": task.tools.model_dump(mode="json"),
            "policies": task.policies,
            "model_config": self._serialize_model_config(
                agent_config=agent_config, user_config=user_config, judge_config=judge_config
            ),
        }

        # Write all split output files via the composed writer. The per-trial
        # :class:`OutputWriter` is cached inside FileArtifactWriter keyed on
        # ``trial_dir`` so repeated writes don't re-create the directory.
        writer.write_trial_bundle(
            trial_dir, trajectory, task_config_dict, final_state, runner.logger
        )

        self.logger.info(
            "Trial output saved",
            task_id=task.task_id,
            trial_index=trial_idx,
            output_dir=str(trial_dir),
        )

        return TrialResult.from_trajectory(
            trial_id=trial_id, trajectory=trajectory, worker_id=worker_id
        )

    def _serialize_model_config(
        self,
        agent_config: ModelConfig | None = None,
        user_config: ModelConfig | None = None,
        judge_config: ModelConfig | None = None,
    ) -> dict[str, Any]:
        """Serialize model config for trial output.

        Stage 7 (P6): each role's block is extended with a ``resolved:``
        sub-block carrying the preset fingerprint (effective preset name plus
        the six registered-policy names from
        :func:`tolokaforge.core.llm.presets.resolve_policy_names`). Analytics
        tools diff this across runs to detect config drift without having to
        re-match the preset YAML.

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

        # Stage 7 — resolved fingerprint per role.
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
