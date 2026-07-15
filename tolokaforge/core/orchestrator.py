"""Orchestrator for managing runs and workers"""

import logging
import os
import random
import socket
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tolokaforge.adapters import BaseAdapter, ensure_registered_adapter, get_adapter
from tolokaforge.core.conductor import (
    Conductor,
    ConductorContext,
    ConductorFactory,
    InProcessConductor,
)
from tolokaforge.core.engine_run_state import (
    read_persisted_run_id,
    write_engine_run_state,
)
from tolokaforge.core.failure_attribution import (
    attribute_failure,
    is_failed_trajectory,
    summarize_failure_attributions,
)
from tolokaforge.core.llm import LLMClient
from tolokaforge.core.llm.presets import (
    get_overlay_path,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.metrics import (
    calculate_aggregate_metrics,
    calculate_latency_percentiles,
    calculate_task_metrics,
)
from tolokaforge.core.models import (
    ModelConfig,
    ProjectConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
    TypeSenseConfig,
)
from tolokaforge.core.output.aggregates import FileAggregateWriter, RunAggregateWriter
from tolokaforge.core.output.artifacts import FileArtifactWriter, TrialArtifactWriter
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.resume import RunStateManager
from tolokaforge.core.run_display_events import RunDisplayEvents, _NullRunDisplayEvents
from tolokaforge.core.run_queue import AttemptLease, create_run_queue
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial import (
    DEFAULT_TOOL_TIMEOUT_S,
    EnvEndpoints,
    EnvironmentManifest,
    TrialSpec,
)
from tolokaforge.core.trial_executor import TrialExecutor
from tolokaforge.runner.models import AdapterType, TaskDescription

# Tools that need Playwright + Chromium baked into the runner image. The
# orchestrator scans the task list before starting the docker stack and
# enables the ``INSTALL_PLAYWRIGHT`` build arg when any task uses one of
# them. ``MobileTool`` subclasses ``BrowserTool`` so it has the same
# Playwright dependency — keeping the list inline (vs. inferring from the
# class hierarchy) avoids importing the tool modules at orchestrator
# import time.
_PLAYWRIGHT_TOOL_NAMES: frozenset[str] = frozenset({"browser", "mobile"})

# Tools / initial-state declarations that require ``full_stack`` (mock-web
# at port 8080 and rag-service at 8001) on top of the core db-service +
# runner. ``browser`` and ``mobile`` reach mock-web for app/site URLs;
# ``search_kb`` reaches rag-service. Tasks may also declare
# ``initial_state.mock_web`` / ``initial_state.rag`` directly without
# enabling those tools — both shapes flip the switch.
#
# Routing matrix:
# +------------------------------------+--------------+
# | Signal in task config              | Stack        |
# +------------------------------------+--------------+
# | tools.agent.enabled ∋ browser      | full_stack   |
# | tools.agent.enabled ∋ mobile       | full_stack   |
# | tools.agent.enabled ∋ search_kb    | full_stack   |
# | initial_state.mock_web is truthy   | full_stack   |
# | initial_state.rag is truthy        | full_stack   |
# | otherwise                          | core_stack   |
# +------------------------------------+--------------+
_FULL_STACK_TOOL_NAMES: frozenset[str] = frozenset({"browser", "mobile", "search_kb"})


def _tasks_need_playwright(tasks: list[Any]) -> bool:
    """Return True iff any task enables a Playwright-dependent tool.

    Used by :class:`Orchestrator` to decide whether to pass
    ``enable_playwright=True`` to :func:`core_stack`. Pure function so
    unit tests can construct ``TaskConfig`` instances directly without
    standing up the docker stack.
    """
    for task in tasks:
        enabled = task.tools.agent.get("enabled", []) if task.tools else []
        if _PLAYWRIGHT_TOOL_NAMES.intersection(enabled):
            return True
    return False


def _tasks_need_full_stack(tasks: list[Any]) -> bool:
    """Return True iff any task needs ``full_stack`` (mock-web / rag).

    See ``_FULL_STACK_TOOL_NAMES`` for the routing matrix. Detection works
    on both ``ToolsConfig`` / ``InitialStateConfig`` Pydantic models and
    plain dicts (raw YAML), to keep the unit tests simple.
    """
    for task in tasks:
        enabled = task.tools.agent.get("enabled", []) if task.tools else []
        if _FULL_STACK_TOOL_NAMES.intersection(enabled):
            return True
        initial_state = task.initial_state if task.initial_state is not None else None
        if initial_state is None:
            continue
        mock_web = (
            initial_state.mock_web
            if hasattr(initial_state, "mock_web")
            else initial_state.get("mock_web") if isinstance(initial_state, dict) else None
        )
        rag = (
            initial_state.rag
            if hasattr(initial_state, "rag")
            else initial_state.get("rag") if isinstance(initial_state, dict) else None
        )
        if mock_web or rag:
            return True
    return False


def _run_needs_full_stack(tasks: list[Any], stack_requirements: Any) -> bool:
    """Return True iff the run needs ``full_stack``.

    Combines the task-level signals (:func:`_tasks_need_full_stack`) with the
    adapter's declarative stack needs
    (``DockerStackRequirements.needs_rag_service``). Adapters whose search
    signal is not visible in task tool names or ``initial_state`` (e.g. a
    domain-shipped ``docindex/`` knowledge base surfaced as
    ``TaskDescription.search.enabled``) declare the rag-service need on their
    :meth:`BaseAdapter.docker_stack_requirements`. The Runner hard-fails
    ``RegisterTrial`` for search-enabled tasks when no RAG client is
    configured (``runner/service.py``), so the provisioning decision must
    follow the same signal the Runner enforces. Duck-typed like its sibling
    so unit tests can pass lightweight stand-ins.
    """
    if stack_requirements is not None and getattr(stack_requirements, "needs_rag_service", False):
        return True
    return _tasks_need_full_stack(tasks)


def resolve_run_directory(base_output_dir: str | Path) -> tuple[str, Path]:
    """Return ``(run_id, output_dir)`` for a run rooted at ``base_output_dir``.

    ``base_output_dir`` is treated as a base name — the returned
    ``output_dir`` is a sibling under ``Path(base_output_dir).parent``
    named ``<basename>_<YYYYMMDD_HHMMSS>``. The timestamp is sourced from
    :func:`datetime.now` at call time, so successive invocations produce
    distinct paths (within one-second resolution). The returned
    ``output_dir`` is NOT ``.resolve()``d — banner rendering and disk I/O
    apply their own resolution.

    Raises :class:`ValueError` with a message naming ``evaluation.output_dir``
    when the basename is empty (``.``, ``/``, ``""``).
    """
    base_name = Path(base_output_dir).name
    if not base_name:
        raise ValueError(
            f"run requires evaluation.output_dir with a non-empty basename; got {base_output_dir!r}"
        )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{base_name}_{timestamp}"
    output_dir = Path(base_output_dir).parent / run_id
    return run_id, output_dir


_DEFAULT_DB_SERVICE_URL = "http://tolokaforge-db-service:8000"
"""Runner-perspective DB service URL the docker stack injects into the runner
container at start (`tolokaforge/docker/stacks/core.py`). The orchestrator
mirrors the value on ``TrialSpec.env_endpoints`` so a future out-of-process
runner reads its service URLs from the spec instead of its own env."""


def _normalise_runner_url(runner_address: str) -> str:
    """Prepend ``http://`` to a bare ``host:port`` runner address, leaving
    fully-qualified URLs untouched."""
    if runner_address.startswith(("http://", "https://")):
        return runner_address
    return f"http://{runner_address}"


def _build_env_endpoints(runner_address: str) -> EnvEndpoints:
    """Resolve the per-trial service URLs for inclusion in :class:`TrialSpec`.

    Field semantics:

    * ``runner_url`` — derived from the orchestrator's known runner
      address (the value passed to :class:`SharedStackRuntimeBackend`). Always set.
    * ``db_url`` — populated in built-in-stack mode from
      ``DB_SERVICE_URL`` in the environment (or the default the docker
      stack injects, ``_DEFAULT_DB_SERVICE_URL``). Env_manifest mode
      resolves it best-effort from the task-declared compose stack; a
      missing ``db-service`` leaves it ``None`` — see
      :class:`EnvEndpoints`.
    * ``rag_url`` — optional. Reads ``RAG_SERVICE_URL`` from the
      environment if set, otherwise stays ``None``. ``rag-service``
      ships in ``full_stack`` only, so a ``core_stack`` run with no
      override resolves to ``None`` — carrying a hardcoded RAG URL
      would point at a service that isn't running.
    """
    return EnvEndpoints(
        db_url=os.environ.get("DB_SERVICE_URL", _DEFAULT_DB_SERVICE_URL),
        rag_url=os.environ.get("RAG_SERVICE_URL"),
        runner_url=_normalise_runner_url(runner_address),
    )


@dataclass(frozen=True)
class OrchestratorDeps:
    """Pluggable seams the :class:`Orchestrator` delegates to.

    Packs the four independent injection points (per-trial artifact
    writer, run-level aggregate writer, execution runtime, per-trial
    executor factory) into a single frozen record so the constructor
    doesn't grow a kwarg per seam. Default construction preserves the
    legacy behaviour: fresh :class:`FileArtifactWriter` /
    :class:`FileAggregateWriter` defaults, no runtime backend override
    (``run()`` builds :class:`DockerRuntime`), no factory override
    (``_build_conductor`` builds :class:`InProcessConductor`).
    """

    artifact_writer: TrialArtifactWriter = field(default_factory=FileArtifactWriter)
    run_aggregate_writer: RunAggregateWriter = field(default_factory=FileAggregateWriter)
    runtime_backend: RuntimeBackend | None = None
    conductor_factory: ConductorFactory | None = None
    events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)


class Orchestrator:
    """Orchestrates benchmark runs across tasks and trials"""

    def __init__(
        self,
        config: RunConfig,
        resume: bool = False,
        verbose: bool = False,
        strict: bool = False,
        deps: OrchestratorDeps | None = None,
        project: ProjectConfig | None = None,
    ):
        self.config = config
        self.resume = resume
        self.verbose = verbose
        self.strict = strict
        # Enclosing project (loaded from ``project.yaml``). Adapter
        # instantiation reads ``project.task_defaults`` from here so every
        # task inherits the project-level defaults declared alongside
        # the pack. ``None`` for callers without a project (e.g. old-
        # shape packs where the CLI synthesises a default upstream).
        self.project = project
        self.tasks: list[TaskConfig] = []
        self.results: list[Trajectory] = []
        self.state_manager: RunStateManager | None = None
        self.adapter: BaseAdapter | None = None
        # Shared per-trial writer — every per-trial write goes through it
        # so the orchestrator stays decoupled from filesystem details and
        # alternative writers (in-memory tests, remote stores) can plug in.
        # Run-level analogue: the four post-run aggregate JSONs go through
        # the aggregate writer instead of inline ``json.dump`` calls.
        # Execution surface: the orchestrator depends on the
        # :class:`RuntimeBackend` Protocol, not a concrete class. When
        # ``None``, ``run()`` / ``run_worker()`` construct a default
        # :class:`SharedStackRuntimeBackend` from the resolved runner address — the
        # legacy behaviour. Tests / alternate backends inject via ``deps``.
        # Per-trial executor: the orchestrator delegates each trial to a
        # :class:`Conductor`. ``_build_conductor`` invokes the injected
        # factory (typed against :class:`ConductorContext`) when one is
        # supplied; otherwise it constructs :class:`InProcessConductor`.
        resolved_deps = deps if deps is not None else OrchestratorDeps()
        self._artifact_writer: TrialArtifactWriter = resolved_deps.artifact_writer
        self._run_aggregate_writer: RunAggregateWriter = resolved_deps.run_aggregate_writer
        self._injected_runtime_backend: RuntimeBackend | None = resolved_deps.runtime_backend
        self._conductor_factory: ConductorFactory | None = resolved_deps.conductor_factory
        self._events: RunDisplayEvents = resolved_deps.events
        # Per-run cache of resolved ``TaskDescription`` objects keyed by
        # task_id. ``adapter.to_task_description()`` reads the system
        # prompt, tool schemas, fixtures, and base64-bundles the task_dir
        # — repeating that K times for ``repeats=K`` trials of the same
        # task is wasted work. Populated lazily by ``_build_trial_spec``.
        self._task_desc_cache: dict[str, TaskDescription] = {}

        # Initialize logger
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = get_logger("orchestrator", level=log_level, strict=strict)

        # Docker submodules use `logging.getLogger(__name__)`; the root
        # handler installed by `configure_root_logging` renders them via
        # propagation. Set the namespace threshold so INFO-level progress
        # messages emit even when root sits at WARNING.
        logging.getLogger("tolokaforge.docker").setLevel(log_level)

    def _create_adapter(self) -> BaseAdapter:
        """Create adapter based on configuration"""
        adapter_config = self.config.evaluation.harness_adapter

        if adapter_config:
            adapter_type = adapter_config.type
            params = adapter_config.params.copy()
        else:
            adapter_type = AdapterType.NATIVE
            params = {}

        # Add tasks_glob to params for both native and other adapters
        params["tasks_glob"] = self.config.evaluation.tasks_glob
        # ``evaluation.projects`` is the canonical field; the deprecated
        # ``evaluation.task_packs`` alias is coerced by
        # ``EvaluationConfig`` so ``projects`` always carries the
        # effective list here.
        task_packs = list(self.config.evaluation.projects)

        # In Docker flows, TASK_PACKS_DIRS can override config paths to container-visible mounts.
        env_task_packs = os.environ.get("TASK_PACKS_DIRS", "").strip()
        if env_task_packs:
            task_packs = [part.strip() for part in env_task_packs.split(",") if part.strip()]
        params["task_packs"] = task_packs

        # Pass TypeSense config to adapter if configured
        typesense_config = self.config.orchestrator.typesense
        if typesense_config and typesense_config.enabled:
            params["typesense"] = typesense_config.model_dump()

        # Layer project.task_defaults under every task the adapter loads.
        # Adapters consume this via ``load_task_yaml(project_task_defaults=...)``.
        # ``exclude_defaults`` drops fields the project author didn't set
        # so we don't repeat schema defaults inside every task dict.
        if self.project is not None:
            defaults = self.project.task_defaults.model_dump(exclude_defaults=True)
            if defaults:
                params["project_task_defaults"] = defaults
            # Forward the project's default_environment patch so the adapter
            # can bind it to each task's own environment patch via
            # ``project_loader.resolve`` and hand the resulting
            # ``EnvironmentManifest`` to ``TaskDescription``.
            if self.project.default_environment is not None:
                params["project_default_environment"] = self.project.default_environment

        self.logger.info("Creating adapter", type=adapter_type, params=params)
        return get_adapter(adapter_type, params)

    @staticmethod
    def _collect_existing_cost(output_dir: Path) -> float:
        """Aggregate already-recorded trial cost from output artifacts."""
        total_cost = 0.0
        trials_root = output_dir / "trials"
        if not trials_root.exists():
            return total_cost

        import yaml

        for metrics_path in trials_root.glob("*/*/metrics.yaml"):
            try:
                with open(metrics_path) as f:
                    metrics = yaml.safe_load(f) or {}
                total_cost += float(metrics.get("cost_usd", 0.0) or 0.0)
            except Exception:
                continue
        return total_cost

    @staticmethod
    def _is_retryable_trajectory(trajectory: Trajectory) -> bool:
        """Classify retryable infrastructure failures.

        Substrate provisioning failures (``TerminationReason.PROVISION_ERROR``)
        short-circuit to non-retryable — ``failure_attribution`` classifies
        them as ``deterministic=True``, and retrying a deterministic
        config fault (bad compose file, missing manifest) burns cycles
        without changing the outcome. When we later gain a way to
        distinguish transient substrate faults (image pull timeout, docker
        daemon flake) from deterministic config faults, this branch will
        gate on that finer signal; today, fail-fast preserves diagnostic
        clarity and matches AGENTS.md rule 1.
        """
        if trajectory.termination_reason == TerminationReason.PROVISION_ERROR:
            return False
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            return True
        if trajectory.termination_reason in (
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_ERROR,
            TerminationReason.TIMEOUT,
            TerminationReason.ERROR,
        ):
            return True
        return False

    def _build_conductor(
        self,
        *,
        agent_client: LLMClient,
        runtime_backend: RuntimeBackend,
        output_dir: Path,
        request_limiter: GlobalRateLimiter | None,
    ) -> Conductor:
        """Construct the per-trial executor for this run.

        Invokes the injected ``conductor_factory`` when one was supplied
        on ``__init__``; otherwise defaults to a fresh :class:`InProcessConductor`
        wired against the orchestrator's per-run dependencies.

        Called once per ``run()`` / ``run_worker()`` invocation, after
        the adapter is loaded, the artifact writer is initialised, and the
        per-run wiring (LLM client, runtime backend, output directory,
        rate limiter) is resolved. Raises ``RuntimeError`` if ``self.adapter``
        is unset — surfaces a clear failure rather than propagating ``None``
        into the Conductor's body (where it would crash deep in ``run()``).
        """
        if self.adapter is None:
            raise RuntimeError(
                "Conductor cannot be built before the adapter is loaded. "
                "Ensure load_tasks() has run successfully."
            )
        from tolokaforge.core.trial_grader import RunnerRPCTrialGrader

        trial_grader = RunnerRPCTrialGrader(runtime_backend=runtime_backend, logger=self.logger)

        ctx = ConductorContext(
            adapter=self.adapter,
            artifact_writer=self._artifact_writer,
            config=self.config,
            logger=self.logger,
            verbose=self.verbose,
            strict=self.strict,
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            trial_grader=trial_grader,
            output_dir=output_dir,
            request_limiter=request_limiter,
            events=self._events,
        )
        if self._conductor_factory is not None:
            return self._conductor_factory(ctx)
        # ``ConductorContext`` fields are a 1:1 match for
        # ``InProcessConductor.__init__`` kwargs. Shallow-unpack via
        # ``vars`` (``dataclasses.asdict`` would deep-copy) so a new
        # field on the context surfaces as a loud unexpected-kwarg
        # error here instead of a silent omission.
        return InProcessConductor(**vars(ctx))

    def _build_trial_spec(
        self,
        *,
        task: TaskConfig,
        trial_idx: int,
        attempt_id: int,
        worker_id: str,
        run_id: str,
        agent_client: LLMClient,
        user_config: ModelConfig,
        judge_config: ModelConfig | None,
        env_endpoints: EnvEndpoints,
    ) -> TrialSpec:
        """Build the per-trial :class:`TrialSpec` the Conductor consumes.

        Resolves the wire-format ``TaskDescription`` through the adapter and
        validates that its declared backend is registered before the spec is
        constructed, so failures surface here rather than mid-execution.
        ``run_id`` is supplied by the caller (computed once at the top of
        ``run()`` / read from the engine run-state file in ``run_worker()``)
        so trial identity is independent of where artifacts are written.
        """
        if self.adapter is None:
            raise RuntimeError("Trial spec cannot be built before the adapter is loaded.")
        task_desc = self._task_desc_cache.get(task.task_id)
        if task_desc is None:
            task_desc = self.adapter.to_task_description(task.task_id)
            ensure_registered_adapter(task_desc.adapter_type)
            self._task_desc_cache[task.task_id] = task_desc
        return TrialSpec(
            trial_id=f"{task.task_id}:{trial_idx}",
            run_id=run_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            task=task_desc,
            agent_model_config=agent_client.config,
            user_model_config=user_config,
            judge_model_config=judge_config,
            max_turns=task.max_turns,
            default_tool_timeout_s=DEFAULT_TOOL_TIMEOUT_S,
            env_endpoints=env_endpoints,
        )

    def _canonicalise_resumed_run_id(self, run_state: Any, canonical_run_id: str) -> None:
        """Heal a resumed :class:`RunState` whose ``run_id`` disagrees with
        the directory it lives in.

        The directory basename is the disk fact and the canonical identifier
        every other surface (engine_run_state.json, TrialSpec.run_id) is
        derived from. A legacy state file written before the unification may
        carry a timestamp-only ``run_id``; on resume we rewrite the state
        file in place so the two surfaces agree from now on. No-op when
        they already match.
        """
        if self.state_manager is None:
            raise RuntimeError("state_manager must be initialised before canonicalising run_id")
        if run_state.run_id == canonical_run_id:
            return
        self.logger.warning(
            "RunState.run_id differs from output_dir.name; canonicalising to output_dir.name",
            loaded=run_state.run_id,
            canonical=canonical_run_id,
        )
        run_state.run_id = canonical_run_id
        self.state_manager.save_state(run_state)

    def _cleanup_runner_state_for_retry(
        self,
        runtime_backend: RuntimeBackend,
        task_id: str,
        trial_idx: int,
    ) -> None:
        """Forget the prior attempt's runner-side trial registration before retry.

        The Runner service tracks each trial in ``self.trials[trial_id]`` plus
        the DB Service trial row. ``RegisterTrial`` rejects duplicates with
        ``Trial 'X' already exists``, so re-attempting a transiently-failed
        trial would otherwise burn every retry on the registration error.

        Idempotent: a stale or already-absent trial is logged and ignored so a
        failing cleanup never blocks the retry attempt itself (the
        re-registration will surface a clearer error if state is unrecoverable).
        """
        trial_id = f"{task_id}:{trial_idx}"
        try:
            result = runtime_backend.cleanup_trial(trial_id)
        except Exception as e:
            self.logger.warning(
                "Cleanup before retry raised; continuing with re-registration",
                task_id=task_id,
                trial_index=trial_idx,
                error=str(e),
            )
            return
        if not result.get("success"):
            self.logger.warning(
                "Cleanup before retry returned non-success; continuing with re-registration",
                task_id=task_id,
                trial_index=trial_idx,
                error=result.get("error"),
            )

    def _extract_run_env_manifest(self) -> EnvironmentManifest | None:
        """Return the shared ``environment_manifest`` declared by the run's
        tasks, or ``None`` if none of them declare one.

        The run's tasks must be consistent: either every task in the run
        declares the same ``environment_manifest.compose_file`` or none do.
        Mixed runs (some tasks with, some without; or different compose
        files across tasks) fail loud — a single ``SharedStackRuntimeBackend``
        can only materialise one substrate per run, and a mixed declaration
        signals an ambiguous operator intent.
        """
        manifests_by_compose: dict[str, EnvironmentManifest] = {}
        tasks_by_manifest_status: dict[bool, list[str]] = {True: [], False: []}
        for task in self.tasks:
            manifest = task.environment_manifest
            tasks_by_manifest_status[manifest is not None].append(task.task_id)
            if manifest is not None:
                manifests_by_compose[str(manifest.compose_file)] = manifest
        if not manifests_by_compose:
            return None
        if tasks_by_manifest_status[False]:
            raise RuntimeError(
                "Run has a mix of tasks with and without environment_manifest — "
                "SharedStackRuntimeBackend can only materialise one substrate per run. "
                f"Tasks with manifest: {tasks_by_manifest_status[True]}. "
                f"Tasks without: {tasks_by_manifest_status[False]}. "
                "Split into separate runs or declare a consistent manifest for all tasks."
            )
        if len(manifests_by_compose) > 1:
            raise RuntimeError(
                "Run has tasks declaring different environment_manifest.compose_file "
                f"values: {sorted(manifests_by_compose)}. A single "
                "SharedStackRuntimeBackend materialises one substrate for the whole run. "
                "Split into separate runs or converge on one compose file."
            )
        return next(iter(manifests_by_compose.values()))

    def _construct_runtime_backend(
        self,
        runner_address: str,
        env_manifest: EnvironmentManifest | None = None,
        run_id: str = "run",
    ) -> RuntimeBackend:
        """Construct the runtime backend from ``config.orchestrator.runtime``.

        ``shared`` (default) → :class:`SharedStackRuntimeBackend`. When
        ``env_manifest`` is passed the backend materialises the
        task-declared compose stack once per run at ``connect`` time.
        When absent, the backend connects to the built-in shared engine
        at ``runner_address`` with endpoints resolved via
        :func:`_build_env_endpoints`.

        ``per_trial`` → :class:`PerTrialRuntimeBackend` (env_manifest is
        consumed per-trial there; ignored here).

        Called when no backend is injected via
        ``Orchestrator.__init__(runtime_backend=...)``.
        """
        runtime_choice = self.config.orchestrator.runtime
        if runtime_choice == "per_trial":
            from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend

            self.logger.info(
                "runtime.backend.selected", backend="PerTrialRuntimeBackend", source="config"
            )
            return PerTrialRuntimeBackend()
        from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

        self.logger.info(
            "runtime.backend.selected",
            backend="SharedStackRuntimeBackend",
            source="config" if runtime_choice == "shared" else "default",
            env_manifest_present=env_manifest is not None,
        )
        if env_manifest is not None:
            return SharedStackRuntimeBackend(
                env_manifest=env_manifest,
                run_id=run_id,
            )
        return SharedStackRuntimeBackend(
            runner_address=runner_address,
            endpoints=_build_env_endpoints(runner_address),
        )

    _LOCAL_ALIAS_TAG: str = "local"
    """Stable secondary tag applied to freshly-built engine images after
    ``EngineStack.start_all()``. Decoupled from ``tolokaforge.__version__``
    so task compose files referencing ``:local`` don't have to rotate on
    every release. When a public registry lands, task composes will
    reference the published tag directly; ``:local`` stays as the
    local-dev alias."""

    _PER_TRIAL_ALIASED_SERVICES: tuple[tuple[str, str], ...] = (
        ("runner", "tolokaforge-runner"),
        ("db-service", "tolokaforge-db-service"),
    )
    """(service_name, alias_repository) pairs the ``:local``-alias hook
    aliases. Task compose files that declare a per-trial substrate
    reference these images by ``<repo>:local``; the shared-stack build
    is the source of truth for each image's content, and the alias step
    surfaces it under a stable name."""

    def _ensure_engine_image_local_aliases(self, service_stack: Any) -> None:
        """Apply ``:local`` aliases on the freshly-built engine images so
        task compose files can reference stable names that outlive
        content-hash rebuilds and release-version bumps.

        The shared-stack build tags each engine image with a content-hash
        suffix that changes on every source edit — unreachable from a
        task-pack compose file. After the stack starts, this hook applies
        ``:local`` as a secondary tag on the same underlying images (no
        rebuild, no data copy). Per-trial task compose files reference
        ``tolokaforge-runner:local`` + ``tolokaforge-db-service:local``,
        which are legal pinned tags (not one of the floating-tag names
        the :class:`EnvironmentManifest` validator rejects: ``latest`` /
        ``main`` / ``master`` / ``edge`` / ``stable`` / ``dev`` /
        ``develop`` / ``nightly`` / ``head``).

        Each alias step is best-effort against :class:`ImageError` — the
        expected daemon-rejection path is narrowed to that type, so a
        genuine coding bug (``AttributeError`` / ``TypeError``) surfaces
        loudly instead of masquerading as a WARNING. Only per-trial task
        compose files referencing the ``:local`` tags would then fail —
        a user-visible error at that point, not at run-start.
        """
        from tolokaforge.docker.image import ImageError

        for service_name, alias_repository in self._PER_TRIAL_ALIASED_SERVICES:
            image = service_stack.get_image(service_name)
            if image is None:
                self.logger.debug(
                    "engine image not built by service stack; skipping alias-tag hook",
                    service_name=service_name,
                )
                continue
            try:
                image.add_alias_tag(alias_repository, self._LOCAL_ALIAS_TAG)
            except ImageError as e:
                self.logger.warning(
                    "Failed to apply engine-image alias tag; "
                    "task compose files referencing the :local tag will fail",
                    service_name=service_name,
                    alias_repository=alias_repository,
                    alias_tag=self._LOCAL_ALIAS_TAG,
                    error=str(e),
                )
                continue

    def _build_trial_executor(
        self, runtime_backend: RuntimeBackend, conductor: Conductor
    ) -> TrialExecutor:
        """Compose the per-run :class:`TrialExecutor` (ADR-0015).

        The executor owns the per-trial substrate lifecycle bracket
        (``provision`` / ``await_ready`` / ``endpoints`` / ``teardown``)
        around ``conductor.run``. The orchestrator submits
        ``trial_executor.execute`` to the worker pool in place of
        ``conductor.run``; the bracket runs on the worker thread so
        provisioning parallelism equals worker count.
        """
        from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

        return ProvisioningTrialExecutor(
            runtime_backend=runtime_backend,
            conductor=conductor,
            logger=self.logger,
        )

    def _verify_isolation_compatibility(self, runtime_backend: RuntimeBackend) -> None:
        """Refuse to start the run if any task declares per-trial isolation
        but the selected runtime backend cannot provide it.

        Called after backend selection and before any trial runs.
        Silent cross-trial state contamination is the failure mode this
        guard prevents — a task that declares
        ``environment_manifest.isolation: per_trial`` would produce wrong
        verdicts when run against a shared stack.

        Reads :attr:`RuntimeBackend.isolation_mode` rather than inspecting
        the concrete class, so a future backend on a different substrate
        (Kubernetes, Modal, ...) plugs into this check by setting the
        attribute correctly.

        Raises :class:`RuntimeError` naming the offending tasks and the
        concrete fix.
        """
        from tolokaforge.core.runtime import IsolationMode
        from tolokaforge.runner.models import TaskIsolation

        if runtime_backend.isolation_mode is IsolationMode.PER_TRIAL_STACK:
            # Any per-trial backend satisfies every isolation requirement.
            return

        if self.adapter is None:
            raise RuntimeError(
                "Isolation-compatibility check requires the adapter to be loaded first."
            )

        violations: list[str] = []
        for task in self.tasks:
            task_desc = self._task_desc_cache.get(task.task_id)
            if task_desc is None:
                task_desc = self.adapter.to_task_description(task.task_id)
                self._task_desc_cache[task.task_id] = task_desc
            manifest = task_desc.environment_manifest
            if manifest is None:
                continue
            if manifest.isolation is TaskIsolation.PER_TRIAL:
                violations.append(task.task_id)

        if violations:
            raise RuntimeError(
                f"Runtime backend {type(runtime_backend).__name__} shares state "
                f"across every trial in the run, but {len(violations)} task(s) "
                f"declare `environment_manifest.isolation: per_trial`: "
                f"{sorted(violations)!r}. These tasks would silently produce "
                "wrong verdicts on a shared-stack backend.\n"
                "  Fix: select a per-trial runtime backend in the run config "
                "(e.g. PerTrialRuntimeBackend), or set `isolation: shared_ok` "
                "on the task(s) that genuinely tolerate shared state across "
                "trials."
            )

    def _build_pending_trials(
        self,
        tasks: list[TaskConfig],
        repeats: int,
        skip_completed: Callable[[str, int], bool] | None = None,
    ) -> list[tuple[str, int]]:
        """Build pending (task_id, trial_index) pairs in enqueue order.

        Order is (task, trial_index) lexicographic. With
        ``orchestrator.shuffle_trials`` set, the order is randomized —
        diagnostic only, does not eliminate state leakage between trials.
        """
        pending_trials: list[tuple[str, int]] = []
        for task in tasks:
            for trial_idx in range(repeats):
                if skip_completed and skip_completed(task.task_id, trial_idx):
                    continue
                pending_trials.append((task.task_id, trial_idx))

        if self.config.orchestrator.shuffle_trials:
            random.shuffle(pending_trials)
        return pending_trials

    def _ensure_typesense_started(self) -> None:
        """Start TypeSense server if configured for local mode.

        This must be called before adapter creation to ensure the adapter
        gets resolved port/api_key values.
        """
        typesense_config = self.config.orchestrator.typesense
        if typesense_config and typesense_config.enabled and typesense_config.mode == "local":
            # Check if already resolved (port is int, not "auto")
            if typesense_config.port == "auto" or typesense_config.api_key is None:
                try:
                    from tolokaforge.core.search.typesense_server import create_typesense_server

                    self.logger.info(
                        "Starting local TypeSense server", config=typesense_config.model_dump()
                    )
                    # Create server with individual params from config
                    self._typesense_server = create_typesense_server(
                        port=typesense_config.port,
                        api_key=typesense_config.api_key,
                        data_dir=typesense_config.data_dir,
                        image=typesense_config.image,
                        container_name=typesense_config.container_name,
                        timeout=typesense_config.timeout,
                        cleanup_on_exit=typesense_config.cleanup_on_exit,
                    )
                    if self._typesense_server:
                        self._typesense_server.start()
                        # Update config object with resolved port/api_key for adapter use
                        resolved_config = typesense_config.model_dump()
                        resolved_config["port"] = self._typesense_server.port
                        resolved_config["api_key"] = self._typesense_server.api_key
                        self.config.orchestrator.typesense = TypeSenseConfig(**resolved_config)
                        self.logger.info(
                            "TypeSense server started",
                            host=self._typesense_server.host,
                            port=self._typesense_server.port,
                        )
                    else:
                        raise RuntimeError(
                            "TypeSense server could not be created (Docker not available?). "
                            "TypeSense is configured as enabled; aborting to avoid silent failures."
                        )
                except ImportError as e:
                    raise RuntimeError(
                        f"TypeSense is configured but the server module is not available: {e}"
                    ) from e
                except Exception as e:
                    raise RuntimeError(f"Failed to start TypeSense server: {e}") from e

    def _connect_typesense_to_runner_network(self, service_stack: Any) -> None:
        """Connect TypeSense container to the core stack's Docker network.

        After core_stack starts, the Runner is on 'runner-net'. TypeSense is on
        its own network. We connect TypeSense to runner-net so the Runner can
        reach it via Docker DNS (container name / alias).

        The container port is ALWAYS 8108 inside Docker networks — only the
        host-mapped port differs, and that is irrelevant for inter-container
        communication.
        """
        try:
            import docker as docker_lib

            client = docker_lib.from_env()

            # Get TypeSense container from its stack
            ts_stack = self._typesense_server._stack
            ts_container_obj = ts_stack._containers.get("typesense") if ts_stack else None

            if ts_container_obj is None:
                self.logger.warning("TypeSense container not found for network bridging")
                return

            ts_container_id = ts_container_obj.container_id
            ts_container = client.containers.get(ts_container_id)

            # Get the runner-net network from the core stack
            runner_net = service_stack._networks.get("runner-net")
            if runner_net is None:
                self.logger.warning("Runner network not found for TypeSense bridging")
                return

            docker_network = client.networks.get(runner_net.network_id)

            # Connect TypeSense to runner-net with an alias so it is reachable
            # as "typesense:8108" inside the network.
            docker_network.connect(ts_container, aliases=["typesense"])
            self.logger.info(
                "Connected TypeSense to runner network",
                network=runner_net.name,
                container=ts_container.name,
            )

            # Update TypeSense config to use Docker DNS name for Runner access.
            # Inside Docker networks, containers use the container port (8108)
            # directly — not the host-mapped port.
            typesense_config = self.config.orchestrator.typesense
            if typesense_config:
                resolved_config = typesense_config.model_dump()
                resolved_config["host"] = "typesense"
                resolved_config["port"] = 8108
                self.config.orchestrator.typesense = TypeSenseConfig(**resolved_config)
                self.logger.info(
                    "Updated TypeSense config for Docker networking",
                    host="typesense",
                    port=8108,
                )

                # Propagate Docker-internal connection details to the adapter
                # so that to_task_description() puts Docker-reachable values
                # (typesense:8108) into SearchConfig rather than host-side ones.
                if self.adapter and hasattr(self.adapter, "params"):
                    self.adapter.params["typesense"] = resolved_config
                    self.logger.debug(
                        "Propagated TypeSense Docker config to adapter",
                        host="typesense",
                        port=8108,
                    )

        except Exception as e:
            self.logger.warning("Failed to connect TypeSense to runner network", error=str(e))

    def load_tasks(self) -> None:
        """Load tasks using configured adapter"""
        # Ensure TypeSense is started BEFORE adapter creation
        # This allows the adapter to get resolved port/api_key
        if not hasattr(self, "_typesense_server"):
            self._typesense_server = None
        self._ensure_typesense_started()

        # Create adapter if not already created
        if self.adapter is None:
            self.adapter = self._create_adapter()

        # Get task IDs from adapter
        task_ids = self.adapter.get_task_ids()

        # Load each task
        for task_id in task_ids:
            try:
                task = self.adapter.get_task(task_id)
                self.tasks.append(task)
            except Exception as e:
                self.logger.error("Failed to load task", task_id=task_id, error=str(e))

        self.logger.info("Tasks loaded", count=len(self.tasks), adapter=type(self.adapter).__name__)

    def _resolve_judge_config(self) -> ModelConfig | None:
        """Resolve the run-level judge model and fail loud on the missing-judge case.

        The judge model lives at the run level (``models.judge``), symmetric with
        the agent and user models. There is NO default and NO fallback to the
        agent model (self-grading bias). If any selected task declares an
        ``llm_judge`` grading component but ``models.judge`` is absent, the run is
        rejected here — before any trial executes — naming the offending tasks
        (AGENTS.md rule 1).

        Assumes every adapter populates ``to_task_description().grading.llm_judge``
        for rubric tasks; only ``NativeAdapter`` implements rubric grading today,
        so non-native adapters simply surface no offending tasks here.
        """
        judge_config = self.config.models.get("judge")
        if judge_config is not None:
            return judge_config

        if self.adapter is None:
            raise RuntimeError(
                "Judge config cannot be resolved before the adapter is loaded. "
                "Ensure load_tasks() has run successfully."
            )
        offending = [
            task.task_id
            for task in self.tasks
            if self.adapter.to_task_description(task.task_id).grading.llm_judge is not None
        ]
        if offending:
            raise ValueError(
                "These selected tasks use an llm_judge grading component but the run "
                "config has no judge model: "
                f"{', '.join(sorted(offending))}. Add a judge model to the run config "
                "under models.judge (provider/name), e.g. "
                "`models: {judge: {provider: openrouter, name: anthropic/claude-sonnet-4.6}}`."
            )
        return None

    def run(self) -> Path:
        """Execute all tasks with configured trials.

        Returns the resolved absolute path of the timestamped run directory
        created by this invocation. The path is the same directory the
        orchestrator wrote every trial artifact, report, and run-state file
        into; callers publish or open it after the run completes.
        """
        # The canonical ``run_id`` is computed here once and threaded
        # through the run state, the engine run-state file (so workers
        # read the same value), and every ``TrialSpec`` via
        # ``_build_trial_spec``. ``run()`` treats
        # ``config.evaluation.output_dir`` as a base name and appends a
        # timestamp (so successive runs land in sibling directories);
        # ``prepare_run`` treats its ``output_dir`` arg as the fully-
        # qualified run directory verbatim. Symmetric fail-fast: an empty
        # basename (``.``, ``/``) is rejected before any disk writes.
        base_output_dir = self.config.evaluation.output_dir
        base_name = Path(base_output_dir).name
        if not base_name:
            raise ValueError(
                f"run requires evaluation.output_dir with a non-empty basename; "
                f"got {base_output_dir!r}"
            )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"{base_name}_{timestamp}"
        output_dir = Path(base_output_dir).parent / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure TypeSense is started and tasks are loaded
        if not self.tasks:
            self.load_tasks()

        # Initialize resume state manager
        self.state_manager = RunStateManager(output_dir)

        # Check for existing run state
        run_state = None
        if self.resume:
            run_state = self.state_manager.load_state()
            if run_state:
                self._canonicalise_resumed_run_id(run_state, run_id)
                resume_info = self.state_manager.get_resume_info()
                if resume_info:
                    self.logger.info(
                        "Resuming run",
                        run_id=run_id,
                        completed=resume_info["completed_trials"],
                        total=resume_info["total_trials"],
                        pending=resume_info["pending_trials"],
                        failed=resume_info["failed_trials"],
                    )
            else:
                self.logger.info("No resumable run found, starting fresh")
                self.resume = False

        # Initialize new run state if not resuming
        if not run_state:
            task_ids = [task.task_id for task in self.tasks]
            run_state = self.state_manager.initialize_run(
                run_id=run_id,
                config_path=str(self.config.evaluation.output_dir),
                task_ids=task_ids,
                repeats=self.config.orchestrator.repeats,
            )
            self.logger.info(
                "Starting new run",
                run_id=run_id,
                tasks=len(task_ids),
                repeats=self.config.orchestrator.repeats,
                total_trials=run_state.total_trials,
            )
        write_engine_run_state(output_dir, run_id=run_id, presets_file=get_overlay_path())

        # Create agent and user clients
        agent_config = self.config.models.get("agent")
        user_config = self.config.models.get("user")

        if not agent_config:
            self.logger.error("Agent model configuration required")
            raise ValueError("Agent model configuration required")

        # Apply default user model if not configured
        if user_config is None:
            user_config = ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4.6",
                temperature=0.2,
            )
            self.logger.info(
                "Using default user model",
                user_model="openrouter/anthropic/claude-sonnet-4.6",
            )

        # Resolve the run-level judge model and reject the run up front if any
        # selected task needs a judge but none is configured (fail loud).
        judge_config = self._resolve_judge_config()

        # Log model configuration for all roles
        self.logger.info(
            "Model configuration",
            agent_model=f"{agent_config.provider}/{agent_config.name}",
            user_model=f"{user_config.provider}/{user_config.name}",
            judge_model=(f"{judge_config.provider}/{judge_config.name}" if judge_config else None),
        )

        # Instantiate agent client in orchestrator process
        agent_client = LLMClient(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.effective_max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.effective_max_requests_per_second)
            self.logger.info(
                "Global request limiter enabled",
                max_requests_per_second=self.config.effective_max_requests_per_second,
            )

        # Task-declared shared-stack manifest: if the run's tasks declare an
        # environment_manifest, extract the shared manifest here — mixed / divergent
        # declarations fail loud before we touch docker.
        run_env_manifest = self._extract_run_env_manifest()

        # Auto-start services via EngineStack if configured
        service_stack = None
        if self.config.orchestrator.auto_start_services:
            try:
                from tolokaforge.docker.stacks import core_stack, full_stack

                self.logger.info("Auto-starting Docker services via EngineStack")
                stack_requirements = (
                    self.adapter.docker_stack_requirements() if self.adapter is not None else None
                )
                core_stack_kwargs = (
                    stack_requirements.to_core_stack_kwargs() if stack_requirements else {}
                )
                # Ensure host-side paths for any extra bind mounts exist so
                # Docker doesn't create them as root-owned at start-up.
                for host_path, _ in core_stack_kwargs.get("extra_runner_binds", []):
                    Path(host_path).mkdir(parents=True, exist_ok=True)
                # Detect required Docker features from task configs.
                # Both browser and mobile (a BrowserTool subclass) need
                # Playwright/Chromium baked in; skipping the install for
                # the common case keeps image builds fast.
                if _tasks_need_playwright(self.tasks):
                    self.logger.info(
                        "Playwright-dependent tool detected in tasks — enabling Playwright"
                    )
                    core_stack_kwargs["enable_playwright"] = True
                # Pick full_stack (db-service + runner + rag-service +
                # mock-web) when any task talks to mock-web or rag (see
                # ``_FULL_STACK_TOOL_NAMES`` for the routing matrix) OR when
                # the adapter declares a rag-service need
                # (``DockerStackRequirements.needs_rag_service``).
                # ``full_stack`` accepts every kwarg ``core_stack`` does,
                # so the playwright/binds plumbing above still applies.
                if _run_needs_full_stack(self.tasks, stack_requirements):
                    self.logger.info(
                        "Full-stack-dependent run detected (task browser/mobile/search_kb, "
                        "initial_state.mock_web/rag, or adapter-declared rag-service need) "
                        "- using full_stack (db-service + runner + rag-service + mock-web)",
                        adapter_needs_rag_service=bool(
                            stack_requirements is not None
                            and getattr(stack_requirements, "needs_rag_service", False)
                        ),
                    )
                    stack_factory = full_stack
                else:
                    self.logger.info("Creating service stack (db-service + runner)")
                    stack_factory = core_stack
                service_stack = stack_factory(**core_stack_kwargs)
                per_trial_mode = self.config.orchestrator.runtime == "per_trial"
                # In per_trial mode OR shared+env_manifest mode the built-in engine
                # containers go unused — the task-declared compose stack owns the
                # runner + db-service. The engine images still need to be BUILT so
                # task compose files can reference the `:local` aliases; skipping
                # the container-start is the shared-with-per_trial optimisation.
                task_stack_mode = per_trial_mode or run_env_manifest is not None
                if task_stack_mode:
                    self.logger.info(
                        "Preparing Docker engine images (task-declared-stack mode: "
                        "images built + aliased, built-in containers not started)..."
                    )
                    service_stack.build_and_prepare()
                    self._ensure_engine_image_local_aliases(service_stack)
                    runner_address = None
                    self.logger.info("EngineStack prepared (images ready, no containers started)")
                else:
                    self.logger.info(
                        "Building Docker images and starting containers "
                        "(this may take a few minutes on first run)..."
                    )
                    service_stack.start_all(wait=True)
                    self._ensure_engine_image_local_aliases(service_stack)
                    # Use localhost address — the orchestrator runs on the host,
                    # not inside Docker, so Docker container names don't resolve.
                    runner_url = service_stack.get_service_url("runner", 50051)
                    # get_service_url returns "http://localhost:{port}" — strip scheme for gRPC
                    runner_address = runner_url.replace("http://", "")
                    self.logger.info("EngineStack started", runner_address=runner_address)

                # Connect TypeSense to core stack network so Runner can reach it
                if hasattr(self, "_typesense_server") and self._typesense_server:
                    if task_stack_mode:
                        raise RuntimeError(
                            "TypeSense KB is enabled but the run uses a task-declared "
                            "compose stack (either --runtime per_trial or a run whose tasks "
                            "declare environment_manifest under --runtime shared). The "
                            "TypeSense bridge connects TypeSense to the shared 'runner-net' "
                            "and rewrites the TypeSense config to 'typesense:8108' Docker "
                            "DNS — task-declared runners live on task-side networks and "
                            "cannot reach that host. Either drop the TypeSense KB block or "
                            "drop the task-declared environment_manifest."
                        )
                    self._connect_typesense_to_runner_network(service_stack)
            except Exception as e:
                self.logger.error("Failed to auto-start services", error=str(e))
                raise
        else:
            runner_address = None

        # Resolve the runtime backend: injected for tests / alternate
        # backends, default :class:`SharedStackRuntimeBackend` otherwise.
        if runner_address is None:
            runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")

        runtime_backend: RuntimeBackend
        if self._injected_runtime_backend is not None:
            runtime_backend = self._injected_runtime_backend
        else:
            runtime_backend = self._construct_runtime_backend(
                runner_address,
                env_manifest=run_env_manifest,
                run_id=run_id,
            )
        runtime_backend.connect()
        self.logger.info("Runtime backend connected")
        self._verify_isolation_compatibility(runtime_backend)

        env_endpoints = _build_env_endpoints(runner_address)
        if self.config.orchestrator.runtime == "per_trial" or run_env_manifest is not None:
            # Per-trial backend resolves fresh endpoints per trial via
            # ``endpoints(handle)``. Shared+env_manifest resolves them once
            # at connect time from the materialised stack. In both cases the
            # default values built here would be phantom values that never
            # reach a trial spec — logging them here would misdirect
            # log-based diagnosis.
            self.logger.info(
                "Trial-scoped service endpoints resolved by backend from task-declared stack"
            )
        else:
            self.logger.info(
                "Resolved trial-scoped service endpoints",
                db_url=env_endpoints.db_url,
                rag_url=env_endpoints.rag_url,
                runner_url=env_endpoints.runner_url,
            )

        conductor = self._build_conductor(
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            output_dir=output_dir,
            request_limiter=request_limiter,
        )
        trial_executor = self._build_trial_executor(runtime_backend, conductor)

        executor_healthy = runtime_backend.health_check()
        self.logger.info("Docker runtime health check", executor_healthy=executor_healthy)

        # Build pending task/trial pairs and initialize durable queue.
        task_by_id = {task.task_id: task for task in self.tasks}
        pending_trials = self._build_pending_trials(
            self.tasks,
            self.config.orchestrator.repeats,
            skip_completed=lambda task_id, trial_idx: self.resume
            and self.state_manager.is_completed(task_id, trial_idx),
        )

        run_queue = create_run_queue(
            self.config.effective_queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.effective_max_attempt_retries,
            postgres_dsn=self.config.effective_queue_postgres_dsn,
        )
        run_queue.enqueue_many(pending_trials)
        recovered = run_queue.recover_inflight(
            max_lease_age_s=max(300, self.config.orchestrator.timeouts.episode_s * 2)
        )
        if recovered > 0:
            self.logger.warning("Recovered stale in-flight attempts", recovered=recovered)

        budget_limit = self.config.effective_max_budget_usd
        total_cost_usd = self._collect_existing_cost(output_dir)
        budget_exhausted = False
        total_trials_scheduled = len(pending_trials)
        if total_cost_usd > 0:
            self.logger.info("Loaded existing run spend", total_cost_usd=round(total_cost_usd, 6))
        if budget_limit is not None and total_cost_usd >= budget_limit:
            budget_exhausted = True
            self.logger.warning(
                "Budget already exhausted at run start; no trials will be scheduled",
                budget_limit_usd=budget_limit,
                total_cost_usd=round(total_cost_usd, 6),
            )

        lease_seconds = max(300, self.config.orchestrator.timeouts.episode_s * 2)
        lease_owner = f"orchestrator:{os.getpid()}"

        self._events.run_started(
            total_trials=run_state.total_trials,
            initial_completed=run_state.completed_trials,
        )

        # Run tasks with parallel workers using the durable queue.
        with ThreadPoolExecutor(max_workers=self.config.effective_workers) as executor:
            active_futures: dict[Any, AttemptLease] = {}

            def submit_one() -> bool:
                if budget_exhausted:
                    return False
                lease = run_queue.lease_next(worker_id=lease_owner, lease_seconds=lease_seconds)
                if lease is None:
                    return False
                task = task_by_id.get(lease.task_id)
                if task is None:
                    # Should never happen; fail-fast and continue scheduling.
                    run_queue.mark_failed(
                        lease.id, f"Task not found in loaded set: {lease.task_id}", retryable=False
                    )
                    run_state.mark_failed(
                        lease.task_id, lease.trial_index, f"Task not found: {lease.task_id}"
                    )
                    self.state_manager.save_state(run_state)
                    return True

                # Mark as running
                run_queue.mark_running(lease.id, lease_owner)
                run_state.mark_running(lease.task_id, lease.trial_index)
                self.state_manager.save_state(run_state)

                self._events.trial_started(
                    trial_id=f"{lease.task_id}:{lease.trial_index}",
                    task_id=lease.task_id,
                    trial_index=lease.trial_index,
                )

                try:
                    spec = self._build_trial_spec(
                        task=task,
                        trial_idx=lease.trial_index,
                        attempt_id=lease.retry_count,
                        worker_id=lease_owner,
                        run_id=run_id,
                        agent_client=agent_client,
                        user_config=user_config,
                        judge_config=judge_config,
                        env_endpoints=env_endpoints,
                    )
                except Exception as e:
                    self.logger.error(
                        "Trial spec build failed",
                        task_id=lease.task_id,
                        trial_index=lease.trial_index,
                        error=str(e),
                    )
                    run_queue.mark_failed(lease.id, f"Spec build failed: {e}", retryable=False)
                    run_state.mark_failed(lease.task_id, lease.trial_index, str(e))
                    self.state_manager.save_state(run_state)
                    self._events.trial_failed(
                        trial_id=f"{lease.task_id}:{lease.trial_index}",
                        error=str(e),
                        retryable=False,
                    )
                    return True
                future = executor.submit(trial_executor.execute, spec, task)
                active_futures[future] = lease
                return True

            while len(active_futures) < self.config.effective_workers and submit_one():
                pass

            while active_futures:
                done, _ = wait(active_futures.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    lease = active_futures.pop(future)
                    task_id = lease.task_id
                    trial_idx = lease.trial_index
                    try:
                        trial_result = future.result()
                        trajectory = trial_result.trajectory
                        self.results.append(trajectory)
                        trial_cost = trajectory.metrics.cost_usd or 0.0
                        total_cost_usd += trial_cost

                        # Retry transient infra failures based on queue retry policy.
                        if self._is_retryable_trajectory(trajectory):
                            reason = (
                                trajectory.termination_reason.value
                                if trajectory.termination_reason
                                else trajectory.status.value
                            )
                            should_retry = run_queue.mark_failed(
                                lease.id,
                                f"Retryable failure: {reason}",
                                retryable=True,
                            )
                            if should_retry:
                                self._cleanup_runner_state_for_retry(
                                    runtime_backend, task_id, trial_idx
                                )
                                self.logger.warning(
                                    "Retrying trial after transient failure",
                                    task_id=task_id,
                                    trial_index=trial_idx,
                                    retry_count_next=lease.retry_count + 1,
                                    status=trajectory.status.value,
                                    termination_reason=reason,
                                )
                            else:
                                run_state.mark_failed(
                                    task_id,
                                    trial_idx,
                                    f"Retry limit reached after transient failure: {reason}",
                                )
                                self.state_manager.save_state(run_state)
                                self._events.trial_failed(
                                    trial_id=f"{task_id}:{trial_idx}",
                                    error=f"Retry limit reached after transient failure: {reason}",
                                    retryable=True,
                                )
                            self.logger.info(
                                "Trial failed (transient)",
                                task_id=task_id,
                                trial_index=trial_idx,
                                trial_cost_usd=trial_cost,
                                total_cost_usd=round(total_cost_usd, 6),
                            )
                        else:
                            run_queue.mark_completed(lease.id, cost_usd=trial_cost)
                            # Update run state
                            if trajectory.grade:
                                run_state.mark_completed(
                                    task_id,
                                    trial_idx,
                                    trajectory.grade.binary_pass,
                                    trajectory.grade.score,
                                )
                            else:
                                run_state.mark_completed(task_id, trial_idx, False, 0.0)
                            self.state_manager.save_state(run_state)

                            self._events.trial_completed(
                                trial_id=f"{task_id}:{trial_idx}",
                                binary_pass=(
                                    trajectory.grade.binary_pass if trajectory.grade else False
                                ),
                                score=trajectory.grade.score if trajectory.grade else None,
                            )

                            self.logger.info(
                                "Trial completed",
                                task_id=trajectory.task_id,
                                trial_index=trajectory.trial_index,
                                status=trajectory.status.value,
                                score=trajectory.grade.score if trajectory.grade else None,
                                trial_cost_usd=trial_cost,
                                total_cost_usd=round(total_cost_usd, 6),
                            )
                    except Exception as e:
                        should_retry = run_queue.mark_failed(lease.id, str(e), retryable=True)
                        self.logger.error(
                            "Trial execution exception",
                            task_id=task_id,
                            trial_index=trial_idx,
                            error=str(e),
                            will_retry=should_retry,
                        )
                        if should_retry:
                            self._cleanup_runner_state_for_retry(
                                runtime_backend, task_id, trial_idx
                            )
                        else:
                            # Mark as failed only when retries are exhausted.
                            run_state.mark_failed(task_id, trial_idx, str(e))
                            self.state_manager.save_state(run_state)
                            self._events.trial_failed(
                                trial_id=f"{task_id}:{trial_idx}",
                                error=str(e),
                                retryable=True,
                            )

                    # Stop scheduling new work once budget cap is reached.
                    if budget_limit is not None and total_cost_usd >= budget_limit:
                        if not budget_exhausted:
                            budget_exhausted = True
                            self.logger.warning(
                                "Budget limit reached; no new trials will be scheduled",
                                budget_limit_usd=budget_limit,
                                total_cost_usd=round(total_cost_usd, 6),
                                remaining_trials=run_queue.get_counts().get("pending", 0),
                            )
                        continue

                    while len(active_futures) < self.config.effective_workers and submit_one():
                        pass

        counts = run_queue.get_counts()
        remaining = counts.get("pending", 0) + counts.get("leased", 0) + counts.get("running", 0)
        if budget_exhausted and remaining > 0:
            self.state_manager.mark_run_paused()
            self.logger.warning(
                "Run paused due to budget cap",
                pending_trials=remaining,
                total_scheduled_trials=total_trials_scheduled - remaining,
                budget_limit_usd=budget_limit,
                total_cost_usd=round(total_cost_usd, 6),
            )
        else:
            # Mark run as completed
            self.state_manager.mark_run_completed()

        # Cleanup Docker runtime if used
        if runtime_backend:
            runtime_backend.close()
            self.logger.info("Docker runtime closed")

        # Stop TypeSense BEFORE destroying the EngineStack.
        # TypeSense is connected to runner-net (via _connect_typesense_to_runner_network),
        # so it must be removed from that network before the stack can tear it down.
        if hasattr(self, "_typesense_server") and self._typesense_server:
            try:
                self._typesense_server.stop()
                self.logger.info("TypeSense server stopped")
            except Exception as e:
                self.logger.warning(f"Failed to stop TypeSense server: {e}")

        # Cleanup EngineStack if auto-started
        if service_stack is not None:
            try:
                service_stack.destroy()
                self.logger.info("EngineStack destroyed")
            except Exception as e:
                self.logger.warning("Failed to destroy EngineStack", error=str(e))

        # Generate reports
        self._generate_reports(output_dir)

        resolved_output_dir = output_dir.resolve()
        self._events.run_finished(output_dir=resolved_output_dir)
        return resolved_output_dir

    def run_worker(self, output_dir: Path, max_attempts: int | None = None) -> dict[str, Any]:
        """Run as a worker consuming attempts from the durable queue.

        This mode is intended for distributed execution where multiple worker
        processes lease from the same queue backend.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.tasks:
            self.load_tasks()
        if not self.tasks:
            raise ValueError("No tasks loaded for worker execution")

        agent_config = self.config.models.get("agent")
        user_config = self.config.models.get("user")
        if not agent_config:
            raise ValueError("Agent model configuration required")

        # Apply default user model if not configured
        if user_config is None:
            user_config = ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4.6",
                temperature=0.2,
            )
            self.logger.info(
                "Using default user model",
                user_model="openrouter/anthropic/claude-sonnet-4.6",
            )

        # Resolve the run-level judge model and reject the run up front if any
        # selected task needs a judge but none is configured (fail loud).
        judge_config = self._resolve_judge_config()

        # The canonical run_id is whatever the orchestrator that prepared
        # this directory stamped on ``engine_run_state.json``. Workers join
        # an already-prepared run; absence means the operator skipped
        # ``tolokaforge prepare`` (fail loud).
        run_id = read_persisted_run_id(output_dir)
        if not run_id:
            raise RuntimeError(
                f"Worker requires an engine_run_state.json with a run_id in {output_dir}. "
                "Run `tolokaforge prepare` first."
            )

        # Log model configuration for all roles
        self.logger.info(
            "Model configuration",
            agent_model=f"{agent_config.provider}/{agent_config.name}",
            user_model=f"{user_config.provider}/{user_config.name}",
            judge_model=(f"{judge_config.provider}/{judge_config.name}" if judge_config else None),
        )

        agent_client = LLMClient(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.effective_max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.effective_max_requests_per_second)

        runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")

        # Workers join an already-materialised run; if the run's tasks declare
        # env_manifest, the parent orchestrator materialised a task-declared
        # stack whose runner address is dynamic (testcontainers-allocated),
        # not the EXECUTOR_ADDRESS a worker reads from its own env. Fail loud
        # rather than silently connect to a stale/wrong address.
        run_env_manifest = self._extract_run_env_manifest()
        if run_env_manifest is not None and self._injected_runtime_backend is None:
            raise RuntimeError(
                "Distributed worker mode does not currently support runs with "
                "environment_manifest. The parent orchestrator materialises a "
                "task-declared compose stack at a testcontainers-allocated "
                "address; that address is not propagated to workers. Run the "
                "orchestrator single-process (no worker split) for env_manifest "
                "runs, or drop the manifest and use the built-in shared stack."
            )

        runtime_backend: RuntimeBackend
        if self._injected_runtime_backend is not None:
            runtime_backend = self._injected_runtime_backend
        else:
            runtime_backend = self._construct_runtime_backend(runner_address)
        runtime_backend.connect()
        self._verify_isolation_compatibility(runtime_backend)

        env_endpoints = _build_env_endpoints(runner_address)
        self.logger.info(
            "Resolved trial-scoped service endpoints",
            db_url=env_endpoints.db_url,
            rag_url=env_endpoints.rag_url,
            runner_url=env_endpoints.runner_url,
        )

        conductor = self._build_conductor(
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            output_dir=output_dir,
            request_limiter=request_limiter,
        )
        trial_executor = self._build_trial_executor(runtime_backend, conductor)

        task_by_id = {task.task_id: task for task in self.tasks}
        run_queue = create_run_queue(
            self.config.effective_queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.effective_max_attempt_retries,
            postgres_dsn=self.config.effective_queue_postgres_dsn,
        )
        recovered = run_queue.recover_inflight(
            max_lease_age_s=max(300, self.config.orchestrator.timeouts.episode_s * 2)
        )
        if recovered > 0:
            self.logger.warning("Worker recovered stale in-flight attempts", recovered=recovered)

        budget_limit = self.config.effective_max_budget_usd
        total_cost_usd = self._collect_existing_cost(output_dir)
        lease_owner = f"worker:{socket.gethostname()}:{os.getpid()}"
        lease_seconds = max(300, self.config.orchestrator.timeouts.episode_s * 2)

        processed = 0
        completed = 0
        failed = 0
        requeued = 0

        try:
            while True:
                if max_attempts is not None and processed >= max_attempts:
                    break
                if budget_limit is not None and total_cost_usd >= budget_limit:
                    self.logger.warning(
                        "Worker stopping due to budget cap",
                        budget_limit_usd=budget_limit,
                        total_cost_usd=round(total_cost_usd, 6),
                    )
                    break

                lease = run_queue.lease_next(worker_id=lease_owner, lease_seconds=lease_seconds)
                if lease is None:
                    break

                task = task_by_id.get(lease.task_id)
                if task is None:
                    run_queue.mark_failed(
                        lease.id, f"Task not found in loaded set: {lease.task_id}", retryable=False
                    )
                    failed += 1
                    processed += 1
                    continue

                run_queue.mark_running(lease.id, lease_owner)

                try:
                    spec = self._build_trial_spec(
                        task=task,
                        trial_idx=lease.trial_index,
                        attempt_id=lease.retry_count,
                        worker_id=lease_owner,
                        run_id=run_id,
                        agent_client=agent_client,
                        user_config=user_config,
                        judge_config=judge_config,
                        env_endpoints=env_endpoints,
                    )
                    trial_result = trial_executor.execute(spec, task)
                    trajectory = trial_result.trajectory
                    self.results.append(trajectory)
                    trial_cost = trajectory.metrics.cost_usd or 0.0
                    total_cost_usd += trial_cost

                    if self._is_retryable_trajectory(trajectory):
                        reason = (
                            trajectory.termination_reason.value
                            if trajectory.termination_reason
                            else trajectory.status.value
                        )
                        if run_queue.mark_failed(
                            lease.id, f"Retryable failure: {reason}", retryable=True
                        ):
                            self._cleanup_runner_state_for_retry(
                                runtime_backend, lease.task_id, lease.trial_index
                            )
                            requeued += 1
                        else:
                            failed += 1
                    else:
                        run_queue.mark_completed(lease.id, cost_usd=trial_cost)
                        completed += 1
                except Exception as e:
                    if run_queue.mark_failed(lease.id, str(e), retryable=True):
                        self._cleanup_runner_state_for_retry(
                            runtime_backend, lease.task_id, lease.trial_index
                        )
                        requeued += 1
                    else:
                        failed += 1
                    self.logger.error(
                        "Worker attempt failed with exception",
                        task_id=lease.task_id,
                        trial_index=lease.trial_index,
                        error=str(e),
                    )

                processed += 1
        finally:
            if runtime_backend:
                runtime_backend.close()
            if hasattr(self, "_typesense_server") and self._typesense_server:
                try:
                    self._typesense_server.stop()
                except Exception:
                    pass

        summary = {
            "processed_attempts": processed,
            "completed_attempts": completed,
            "failed_attempts": failed,
            "requeued_attempts": requeued,
            "total_cost_usd": round(total_cost_usd, 6),
        }
        self.logger.info("Worker finished", **summary)
        return summary

    def prepare_run(self, output_dir: Path, reset_queue: bool = False) -> dict[str, Any]:
        """Prepare a run directory and seed the durable queue."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = output_dir.name
        if not run_id:
            raise ValueError(
                f"prepare_run requires an output_dir with a non-empty basename; got {output_dir!r}"
            )

        if not self.tasks:
            self.load_tasks()
        if not self.tasks:
            raise ValueError("No tasks found to enqueue")

        # Reject up front (at enqueue time) if any task needs a judge but none is
        # configured — otherwise a misconfigured distributed run reports success
        # here and then every worker dies identically at grade time.
        self._resolve_judge_config()

        run_queue = create_run_queue(
            self.config.effective_queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.effective_max_attempt_retries,
            postgres_dsn=self.config.effective_queue_postgres_dsn,
        )
        if reset_queue:
            run_queue.clear_all()

        items = self._build_pending_trials(self.tasks, self.config.orchestrator.repeats)
        existing_counts = run_queue.get_counts()
        if existing_counts.get("total", 0) > 0:
            self.logger.warning(
                "Queue already populated; skipping enqueue to avoid duplicates. "
                "Pass reset_queue=True to re-enqueue from scratch.",
                existing_counts=existing_counts,
                would_enqueue=len(items),
            )
            counts = existing_counts
            queued_attempts = 0
        else:
            run_queue.enqueue_many(items)
            counts = run_queue.get_counts()
            queued_attempts = len(items)

        # Persist engine-level run state for subprocess workers (the preset
        # overlay path is the only field today). Worker CLIs read this so the
        # overlay set at ``prepare`` time propagates without the operator
        # threading --presets-file through every ``worker`` invocation.
        write_engine_run_state(output_dir, run_id=run_id, presets_file=get_overlay_path())

        summary = {
            "queued_attempts": queued_attempts,
            "queue_counts": counts,
            "queue_backend": self.config.effective_queue_backend,
        }
        self.logger.info("Run prepared", **summary)
        return summary

    def _generate_reports(self, output_dir: Path) -> None:
        """Generate aggregate reports with pass@k"""
        if not self.results:
            self.logger.warning("No results to report")
            return

        # Group trajectories by task
        task_trajectories = {}
        for traj in self.results:
            if traj.task_id not in task_trajectories:
                task_trajectories[traj.task_id] = []
            task_trajectories[traj.task_id].append(traj)
        task_by_id = {task.task_id: task for task in self.tasks}

        # Calculate metrics per task
        all_task_metrics = []
        for task_id, trajectories in task_trajectories.items():
            task_metrics = calculate_task_metrics(trajectories)
            task_metrics["task_id"] = task_id
            task_cfg = task_by_id.get(task_id)
            if task_cfg is not None:
                task_metrics["benchmark_type"] = task_cfg.category
                task_metrics["complexity"] = task_cfg.metadata.complexity
                task_metrics["expected_failure_modes"] = task_cfg.metadata.expected_failure_modes
                task_metrics["tags"] = task_cfg.metadata.tags
            all_task_metrics.append(task_metrics)

        # Calculate aggregate metrics
        aggregate = calculate_aggregate_metrics(all_task_metrics, weighted=True)
        aggregate.update(
            calculate_latency_percentiles([t.metrics.latency_total_s for t in self.results])
        )

        # Metadata-sliced aggregates
        metadata_slices: dict[str, dict[str, Any]] = {
            "by_benchmark_type": {},
            "by_complexity": {},
            "by_tag": {},
            "by_expected_failure_mode": {},
        }
        groups_by_benchmark: dict[str, list[dict[str, Any]]] = {}
        groups_by_complexity: dict[str, list[dict[str, Any]]] = {}
        groups_by_tag: dict[str, list[dict[str, Any]]] = {}
        groups_by_failure_mode: dict[str, list[dict[str, Any]]] = {}

        for task_metrics in all_task_metrics:
            benchmark_type = str(task_metrics.get("benchmark_type") or "unknown")
            complexity = str(task_metrics.get("complexity") or "unspecified")
            groups_by_benchmark.setdefault(benchmark_type, []).append(task_metrics)
            groups_by_complexity.setdefault(complexity, []).append(task_metrics)

            for tag in task_metrics.get("tags", []) or []:
                groups_by_tag.setdefault(str(tag), []).append(task_metrics)
            for failure_mode in task_metrics.get("expected_failure_modes", []) or []:
                groups_by_failure_mode.setdefault(str(failure_mode), []).append(task_metrics)

        for key, group in groups_by_benchmark.items():
            metadata_slices["by_benchmark_type"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )
        for key, group in groups_by_complexity.items():
            metadata_slices["by_complexity"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )
        for key, group in groups_by_tag.items():
            metadata_slices["by_tag"][key] = calculate_aggregate_metrics(group, weighted=True)
        for key, group in groups_by_failure_mode.items():
            metadata_slices["by_expected_failure_mode"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )

        aggregate["schema_version"] = 1

        # Deterministic failure attribution report
        failure_attributions = [
            attribute_failure(traj) for traj in self.results if is_failed_trajectory(traj)
        ]
        failure_summary = summarize_failure_attributions(failure_attributions)
        failure_attribution_payload = {
            "summary": failure_summary,
            "failures": failure_attributions,
        }

        self._run_aggregate_writer.write_run_aggregates(
            output_dir,
            all_task_metrics,
            aggregate,
            metadata_slices,
            failure_attribution_payload,
        )

        # Log summary
        self.logger.info(
            "Aggregate Results",
            total_trials=aggregate["total_trials"],
            total_tasks=aggregate["total_tasks"],
            success_rate_micro=aggregate.get("success_rate_micro"),
            avg_score_micro=aggregate.get("avg_score_micro"),
            avg_latency_s=aggregate["avg_latency_s"],
            latency_p50_s=aggregate.get("latency_p50_s"),
            latency_p90_s=aggregate.get("latency_p90_s"),
            latency_p99_s=aggregate.get("latency_p99_s"),
            total_cost_usd=aggregate.get("total_cost_usd"),
            judge_cost_usd=aggregate.get("judge_cost_usd"),
            total_cost_incl_judge_usd=aggregate.get("total_cost_incl_judge_usd"),
            avg_turns=aggregate["avg_turns"],
            avg_tool_calls=aggregate["avg_tool_calls"],
            stuck_rate=aggregate["stuck_rate"],
            failed_attempts=failure_summary.get("total_failed_attempts"),
            deterministic_attribution_coverage=failure_summary.get(
                "deterministic_attribution_coverage"
            ),
        )
