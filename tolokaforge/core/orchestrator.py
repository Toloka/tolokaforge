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
from typing import TYPE_CHECKING, Any, NoReturn

from tolokaforge.adapters import BaseAdapter, ensure_registered_adapter, get_adapter
from tolokaforge.adapters._task_loader import (
    GradingSourceKind,
    ToolActor,
    actor_tool_block,
    declared_tool_names,
    enabled_tool_names,
    grading_source_under_adapter,
    replay_world_under_adapter,
    seeded_tables_under_adapter,
    tool_inventory_under_adapter,
    validate_grading_yaml,
)
from tolokaforge.core.budgets import (
    BudgetHit,
    CompositeBudget,
    CostBudget,
    write_limit_hit_marker,
)
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.conductor import (
    Conductor,
    ConductorContext,
    ConductorFactory,
    require_rate_limit_probe_support,
)
from tolokaforge.core.engine_run_state import (
    read_persisted_run_id,
    write_engine_run_state,
)
from tolokaforge.core.failure_attribution import (
    TrialOutcomeClass,
    attribute_failure,
    classify_trial_outcome,
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
from tolokaforge.core.model_data_fingerprint import compute_models_fingerprint
from tolokaforge.core.models import (
    ComputeConfig,
    GradingFindingSeverity,
    ModelConfig,
    ProjectConfig,
    RunConfig,
    ServiceSpec,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
    TypeSenseConfig,
    require_user_simulator_config,
)
from tolokaforge.core.output.aggregate_models import AGGREGATE_SCHEMA_VERSION
from tolokaforge.core.output.aggregates import FileAggregateWriter, RunAggregateWriter
from tolokaforge.core.output.artifacts import FileArtifactWriter, TrialArtifactWriter
from tolokaforge.core.output.service_log_rollup import collect_service_log_captures
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    TrialGraderContext,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.resume import RunStateManager
from tolokaforge.core.run_display_events import (
    RunDisplayEvents,
    ServiceSnapshot,
    _NullRunDisplayEvents,
)
from tolokaforge.core.run_queue import AttemptLease, create_run_queue
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial import (
    DEFAULT_TOOL_TIMEOUT_S,
    EnvEndpoints,
    EnvironmentManifest,
    TrialSpec,
)
from tolokaforge.core.trial_executor import TrialExecutor
from tolokaforge.runner.models import AdapterType, PlanShape, StackScope, TaskDescription
from tolokaforge.secrets import register_runtime_secret

if TYPE_CHECKING:
    from tolokaforge.core.search.typesense_server import TypeSenseServerManager
    from tolokaforge.docker.stacks import TypeSenseAddress

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
# Routing matrix, over either actor's block:
# +--------------------------------------+--------------+
# | Signal in task config                | Stack        |
# +--------------------------------------+--------------+
# | tools.<actor>.enabled ∋ browser      | full_stack   |
# | tools.<actor>.enabled ∋ mobile       | full_stack   |
# | tools.<actor>.enabled ∋ search_kb    | full_stack   |
# | initial_state.mock_web is truthy     | full_stack   |
# | initial_state.rag is truthy          | full_stack   |
# | otherwise                            | core_stack   |
# +--------------------------------------+--------------+
_FULL_STACK_TOOL_NAMES: frozenset[str] = frozenset({"browser", "mobile", "search_kb"})

# Where a bridged local TypeSense server answers from inside ``runner-net``.
# The alias is attached when the bridge connects the container to the network,
# and the container port is fixed by the image — only the host-mapped port
# varies, and that one is unreachable from the runner container.
_TYPESENSE_NETWORK_ALIAS = "typesense"
_TYPESENSE_CONTAINER_PORT = 8108
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _tasks_need_playwright(tasks: list[Any]) -> bool:
    """Return True iff any task enables a Playwright-dependent tool.

    Used by :class:`Orchestrator` to decide whether to pass
    ``enable_playwright=True`` to :func:`core_stack`. Either actor's declaration
    counts: the runner reconstructs a user tool through the same wrapper as an
    agent tool, so a user-side ``browser`` needs the same image. Pure function so
    unit tests can construct ``TaskConfig`` instances directly without
    standing up the docker stack.
    """
    return any(_PLAYWRIGHT_TOOL_NAMES & declared_tool_names(task) for task in tasks)


# Tools whose compose variant runs inside a sibling compose service via
# ``docker exec`` from the runner container. Extend this set when a new
# tool ships a compose variant that needs the same runner-side docker CLI
# + socket bind-mount treatment (see ``_run_needs_docker_cli`` and the
# ``mount_docker_socket`` seam threaded through
# :class:`tolokaforge.core.plugin_registry.RuntimeBackendBuildContext`).
_COMPOSE_VARIANT_TOOL_NAMES: frozenset[str] = frozenset({"bash_session", "str_replace_editor"})


def _tasks_use_compose_variant_tools(tasks: list[Any]) -> bool:
    """Return True iff any task routes a shipped tool into a sibling service
    via the compose variant (``tools.<actor>.<tool>.service: <name>``).

    ``bash_session`` and ``str_replace_editor`` each ship a compose variant
    that executes inside a sibling compose service by ``docker exec``-ing
    from the runner container into it. The runner image needs the docker
    CLI when any enabled tool is in that shape — e.g. the Migration Bench
    adapter's task packs (``services.mb-server`` running the workload,
    tools routed there via ``bash_session.service: mb-server``).

    The ``service:`` key is read from the block that enabled the tool, so a
    user-declared tool is routed by ``tools.user.<tool>.service`` and never by
    the agent's block for the same name.
    """
    return any(
        _actor_routes_a_compose_variant(task, actor) for task in tasks for actor in ToolActor
    )


def _actor_routes_a_compose_variant(task: Any, actor: ToolActor) -> bool:
    """Whether *actor*'s block enables a compose-variant tool and names its service."""
    block = actor_tool_block(task, actor)
    for tool_name in _COMPOSE_VARIANT_TOOL_NAMES & enabled_tool_names(task, actor):
        tool_cfg = block.get(tool_name)
        if isinstance(tool_cfg, dict) and tool_cfg.get("service"):
            return True
    return False


def _run_needs_docker_cli(adapter_type: str | None, tasks: list[Any]) -> bool:
    """Return True iff the run needs the docker CLI baked into the runner image.

    Two triggers today:

    - Terminal-bench tasks exec the docker CLI + compose plugin in the runner
      (against the host daemon via the mounted socket).
    - Any task that routes a shipped tool through the compose variant (see
      :func:`_tasks_use_compose_variant_tools`) — the runner ``docker exec``\\ s
      into the sibling service.

    Detected before build so the slim default image ships without the CLI for
    every other run. Pure function for unit testing.
    """
    if adapter_type == AdapterType.TERMINAL_BENCH:
        return True
    return _tasks_use_compose_variant_tools(tasks)


def _compose_service_image_ref(compose_file: Path, service: str) -> str | None:
    """Return the ``image:`` value declared for ``service`` in ``compose_file``.

    Read straight from the file — no ``docker compose config`` shell-out — so
    the skip-when-already-present check the pre-build helper does works
    without touching the daemon. Missing service, missing file, missing
    ``image:`` entry, or unreadable YAML all return ``None``; the caller
    treats that as "cannot determine, build unconditionally".
    """
    import yaml

    try:
        with compose_file.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    services = data.get("services")
    if not isinstance(services, dict):
        return None
    entry = services.get(service)
    if not isinstance(entry, dict):
        return None
    image = entry.get("image")
    return image if isinstance(image, str) and image else None


def _local_image_exists(image_ref: str) -> bool:
    """Return True iff ``docker image inspect <image_ref>`` reports the image
    present in the local daemon's cache. Any daemon / lookup error is treated
    as "not present" — the caller then builds, and a real daemon problem
    surfaces at build time with the actionable error."""
    import subprocess

    result = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _tasks_need_full_stack(tasks: list[Any]) -> bool:
    """Return True iff any task needs ``full_stack`` (mock-web / rag).

    See ``_FULL_STACK_TOOL_NAMES`` for the routing matrix. Detection works
    on both ``ToolsConfig`` / ``InitialStateConfig`` Pydantic models and
    plain dicts (raw YAML), to keep the unit tests simple.
    """
    for task in tasks:
        if _FULL_STACK_TOOL_NAMES & declared_tool_names(task):
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


def _declared_engine_service_snapshots(service_stack: Any) -> list[ServiceSnapshot]:
    """Return one ``ServiceSnapshot`` per declared engine service.

    Fired on ``phase_changed(phase="starting_services", …)`` so the
    panel can render the service list with ``status="created"`` before
    any container has been brought up. Reads ``service_stack.services``
    — the declared :class:`ServiceDefinition` mapping — so it works
    before ``start_all()``.
    """
    snapshots: list[ServiceSnapshot] = []
    for name in service_stack.services:
        snapshots.append(ServiceSnapshot(name=name, status="created", ports={}, role="engine"))
    return snapshots


def _engine_service_snapshots(service_stack: Any) -> list[ServiceSnapshot]:
    """Return one ``ServiceSnapshot`` per running engine service.

    Fired on ``phase_changed(phase="services_ready", …)`` after
    ``start_all()`` completes so the panel can render live status +
    resolved ports. Reads ``service_stack.get_status()`` for the
    lifecycle string and ``.health`` for the probe verdict; the widget
    prefers ``health`` when present because ``status="running"`` on a
    container whose probe is still starting overstates readiness.
    """
    statuses = service_stack.get_status()
    snapshots: list[ServiceSnapshot] = []
    for name, status in statuses.items():
        effective = status.health if status.health not in (None, "unknown") else status.status
        snapshots.append(
            ServiceSnapshot(
                name=name,
                status=effective,
                ports=dict(status.ports),
                role="engine",
            )
        )
    return snapshots


_RunScopeStackSignature = tuple[str, str, str, tuple[tuple[str, str], ...]]
"""One ``run``-scope stack's canonical divergence signature:
``(stack_id, canonical compose bytes, runner_service or "", sorted inputs)``.
"""


def _run_scope_signature(
    manifest: EnvironmentManifest | None,
) -> tuple[_RunScopeStackSignature, ...]:
    """Canonicalise ``manifest``'s ``run``-scope subset for cross-task
    divergence comparison.

    Returns the ordered tuple of per-stack signatures for stacks whose
    ``stack_scope == "run"``, sorted by ``stack_id`` so plan order does
    not cause spurious divergence. A ``None`` manifest or a manifest with
    no ``run``-scope stack returns ``()`` — the empty signature.
    """
    from tolokaforge.core.env_identity import _canonical_compose_bytes

    if manifest is None:
        return ()
    run_stacks = [decl for decl in manifest.stacks if decl.stack_scope == "run"]
    signatures = [
        (
            decl.stack_id,
            _canonical_compose_bytes(_load_yaml_mapping(decl.compose_file)),
            decl.runner_service or "",
            tuple(sorted(decl.inputs.items())),
        )
        for decl in run_stacks
    ]
    return tuple(sorted(signatures, key=lambda s: s[0]))


def _load_yaml_mapping(compose_file: Path) -> dict[str, Any]:
    """Parse ``compose_file`` and return its top-level mapping.

    Every :class:`StackDecl` in a resolved plan points at a valid compose
    file (:meth:`EnvironmentManifest.load_compose` validates the scalar
    mirror at construction, and the multi-stack path routes each entry
    through the same YAML load in
    :func:`tolokaforge.core.project_loader._build_stack_decl`). Reading
    here is safe.
    """
    import yaml

    with compose_file.open() as f:
        return yaml.safe_load(f) or {}


def _services_in_stack(manifest: EnvironmentManifest, decl: Any) -> dict[str, ServiceSpec]:
    """Return the ``ServiceSpec`` map for the compose services declared by
    ``decl``, defaulting to ``ephemeral`` for services not listed on
    :attr:`EnvironmentManifest.services`.

    Multi-stack manifests keep isolation labels on the flat
    :attr:`EnvironmentManifest.services` map (a decision the composer
    already relies on); this helper narrows that map to one stack by
    intersecting compose-declared service names with the labels the
    manifest carries. Services in the stack's compose file that lack a
    manifest entry inherit the fill-defaults convention
    (:func:`tolokaforge.core.project_loader._fill_missing_service_defaults`).
    """
    stack_compose = _load_yaml_mapping(decl.compose_file)
    stack_service_names = list((stack_compose.get("services") or {}).keys())
    services: dict[str, ServiceSpec] = {}
    for name in stack_service_names:
        services[name] = manifest.services.get(name, ServiceSpec(isolation="ephemeral"))
    return services


def _hash_signature(signature: tuple[_RunScopeStackSignature, ...]) -> str:
    """Short hex digest for the divergence-refusal error message."""
    import hashlib
    import json

    payload = json.dumps(
        [
            [stack_id, compose_bytes, runner_service, list(inputs)]
            for stack_id, compose_bytes, runner_service, inputs in signature
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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

    Packs the injection points into a single frozen record so the
    constructor doesn't grow a kwarg per seam. Default construction
    preserves the legacy behaviour: fresh :class:`FileArtifactWriter`
    / :class:`FileAggregateWriter` defaults, no runtime backend override,
    no conductor factory override, no budget, no agent-client factory
    (``run()`` constructs a bare :class:`LLMClient` for the agent).

    ``agent_client_factory`` — when set, called with the resolved agent
    :class:`ModelConfig` to produce the wire client. The CLI wires
    :class:`FallbackLLMClient` through this seam when
    ``--fallback-models`` is passed. The returned object must
    duck-type :class:`LLMClient` (``config``, ``capabilities``,
    ``generate``) — the conductor reads all three.
    """

    artifact_writer: TrialArtifactWriter = field(default_factory=FileArtifactWriter)
    run_aggregate_writer: RunAggregateWriter = field(default_factory=FileAggregateWriter)
    runtime_backend: RuntimeBackend | None = None
    conductor_factory: ConductorFactory | None = None
    events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)
    budget: CompositeBudget | None = None
    agent_client_factory: Callable[[ModelConfig], LLMClient] | None = None


@dataclass(frozen=True)
class GradingCompleteness:
    """Whether the run produced a verdict for everything it measured.

    An ungradeable trial is one the run *attempted and measured* and then could
    not grade — ``Trajectory.grading_error`` is set, so
    :func:`~tolokaforge.core.failure_attribution.classify_trial_outcome` returns
    ``UNGRADEABLE``. A trial the provider or the substrate killed before the
    agent was measured is an ``INFRASTRUCTURE_ABORT`` and is deliberately not
    here: it produces no verdict either, but it sits outside the measured
    denominator by design and is reported in ``infrastructure_aborts``.

    ``ungradeable_trial_ids`` is the whole of the state; the count derives from
    it rather than being carried beside it, so the two cannot disagree.

    ``measured_trials`` / ``scored_trials`` / ``judge_errored_trials`` carry
    the three counts the two completion gates read (see
    ``docs/adr/0041-zero-coverage-exit-signal.md``). ``scored_trials`` is the
    same quantity :func:`tolokaforge.core.metrics._measured_averages` computes
    from ``t.grade is not None`` — one derivation, referenced everywhere.
    """

    total_attempts: int
    ungradeable_trial_ids: tuple[str, ...]
    measured_trials: int = 0
    scored_trials: int = 0
    judge_errored_trials: int = 0

    @property
    def ungradeable(self) -> int:
        return len(self.ungradeable_trial_ids)

    @property
    def is_complete(self) -> bool:
        return not self.ungradeable_trial_ids

    @property
    def zero_coverage(self) -> bool:
        """No trial reached the agent measurement point on a run that had trials.

        See ``docs/adr/0041-zero-coverage-exit-signal.md``.
        """
        return self.total_attempts > 0 and self.measured_trials == 0

    @property
    def zero_judge_graded(self) -> bool:
        """Every produced grade has ``judge_status == ERRORED``.

        The ``judge_errored_trials > 0`` guard keeps a run whose measured
        trials never produced any grade (``scored_trials == 0``) from firing
        this gate: the failure mode is a judge that errored on scoring
        attempts, not the absence of scoring. See
        ``docs/adr/0041-zero-coverage-exit-signal.md``.
        """
        return self.judge_errored_trials > 0 and self.judge_errored_trials == self.scored_trials


class Orchestrator:
    """Orchestrates benchmark runs across tasks and trials"""

    grading_completeness: GradingCompleteness
    """Set by :meth:`run` and :meth:`run_worker` once they finish, and by
    nothing else. Deliberately left unbound until then rather than defaulted:
    a default would let an orchestrator that never computed completeness report
    a complete run, which is the silent fallback AGENTS.md core rule 1 forbids.
    An embedder gets no exit code, so this attribute is its channel."""

    def __init__(
        self,
        config: RunConfig,
        resume: bool = False,
        verbose: bool = False,
        strict: bool = False,
        deps: OrchestratorDeps | None = None,
        project: ProjectConfig | None = None,
        *,
        config_path: Path | None = None,
    ):
        self.config = config
        self.resume = resume
        self.verbose = verbose
        self.strict = strict
        # Path to the run-config YAML the operator invoked with, threaded from
        # the CLI (``tolokaforge run`` / ``prepare`` / ``worker``) and stamped
        # verbatim into ``run_state.json`` at initialize time. ``None`` marks a
        # programmatic caller that supplied no path — the fresh-run branch
        # writes ``""`` to keep :class:`RunState.config_path` a plain ``str``.
        self._config_path: Path | None = config_path
        # Enclosing project (loaded from ``project.yaml``). Adapter
        # instantiation reads ``project.task_defaults`` from here so every
        # task inherits the project-level defaults declared alongside
        # the pack. ``None`` only for programmatic callers that construct
        # the orchestrator without a project; the CLI always resolves an
        # enclosing ``project.yaml``.
        self.project = project
        self.tasks: list[TaskConfig] = []
        self.results: list[Trajectory] = []
        self.state_manager: RunStateManager | None = None
        self.adapter: BaseAdapter | None = None
        # Trial graders whose ``close()`` must fire at run teardown. Populated
        # by :meth:`_build_conductor`; drained in reverse order at the end of
        # :meth:`run` / :meth:`run_worker` so a broker + worker-pool grader
        # (``queue``) shuts down cleanly regardless of the caller's flow.
        self._trial_graders_to_close: list = (
            []
        )  # list[TrialGrader]; annotated bare to avoid a runtime import cycle
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
        # Budget composite driving the graceful-shutdown path. ``None``
        # means "no CLI budget flag AND no legacy ``compute.max_budget_usd``";
        # ``run()`` promotes the legacy field to a :class:`CostBudget`
        # composite when it fires so both entry points share the same
        # code path.
        self._injected_budget: CompositeBudget | None = resolved_deps.budget
        # Factory the CLI wires when ``--fallback-models`` is passed;
        # produces the agent-side wire client at ``run()`` / ``run_worker()``
        # time. ``None`` means "build a bare :class:`LLMClient`".
        self._agent_client_factory: Callable[[ModelConfig], LLMClient] | None = (
            resolved_deps.agent_client_factory
        )
        # Set to ``"<cost|time|sample> limit"`` on the first budget hit
        # and read by the CLI to shape the run-end banner. ``None`` for
        # runs that reached natural completion.
        self._stopped_reason: str | None = None
        # Per-run cache of resolved ``TaskDescription`` objects keyed by
        # task_id. ``adapter.to_task_description()`` reads the system
        # prompt, tool schemas, fixtures, and base64-bundles the task_dir
        # — repeating that K times for ``repeats=K`` trials of the same
        # task is wasted work. Populated by whichever resolver runs first
        # (the pre-run grading gate, backend selection, or trial-spec
        # building) and held for the life of the run.
        self._task_desc_cache: dict[str, TaskDescription] = {}
        # Run-wide trial ordering: ``(task_id, trial_index) → total_index``
        # (0..total-1). Populated by :meth:`_build_pending_trials` and
        # read at the ``trial_started`` emission site so the panel can
        # render a global ``[N/M]`` prefix.
        self._total_index_by_key: dict[tuple[str, int], int] = {}
        # Handle on the TypeSense server this process started for the run —
        # ``None`` for a remote plane, no plane, or a run handed pre-loaded
        # tasks so ``load_tasks()`` never ran. A server of ours is also a
        # bridge, which is why the injected address is derived from it.
        self._typesense_server: TypeSenseServerManager | None = None

        # Initialize logger
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = get_logger("orchestrator", level=log_level, strict=strict)

        # Docker submodules use `logging.getLogger(__name__)`; the root
        # handler installed by `configure_root_logging` renders them via
        # propagation. Set the namespace threshold so INFO-level progress
        # messages emit even when root sits at WARNING.
        logging.getLogger("tolokaforge.docker").setLevel(log_level)

    def _adapter_fingerprints(self) -> dict[str, Any]:
        """The installed adapter's self-report, keyed by its adapter type.

        Empty when no adapter is loaded or the loaded one reports nothing.
        The key mirrors :meth:`_create_adapter`'s resolution, as a plain
        ``str``: ``HarnessAdapterConfig.type`` already is one, so only the
        enum constant of the unconfigured branch needs ``.value``.
        """
        if self.adapter is None:
            return {}
        payload = self.adapter.fingerprint()
        if payload is None:
            return {}
        adapter_config = self.config.evaluation.harness_adapter
        adapter_type = adapter_config.type if adapter_config else AdapterType.NATIVE.value
        return {adapter_type: payload}

    def _create_adapter(self) -> BaseAdapter:
        """Create adapter based on configuration"""
        adapter_config = self.config.evaluation.harness_adapter

        if adapter_config:
            adapter_type = adapter_config.type
            params = adapter_config.params.copy()
        else:
            adapter_type = AdapterType.NATIVE
            params = {}

        # Coding-harness selector: canonical home is ``models.agent.harness``
        # (adapter-agnostic). Inject it into adapter params here so adapters
        # that already read ``params["agent_harness"]`` keep working; the
        # legacy ``harness_adapter.params.agent_harness`` shape is lifted to
        # ``models.agent`` at parse time, so nothing else in this method sees
        # the old location. ``models.agent.name`` doubles as the model the
        # CLI receives — the same field the engine loop reads.
        agent_model_config = self.config.models.get("agent") if self.config.models else None
        if agent_model_config is not None and agent_model_config.harness is not None:
            params.setdefault("agent_harness", agent_model_config.harness)
            params.setdefault("agent_model", agent_model_config.name)

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

        typesense_config = self.config.orchestrator.effective_typesense()
        if typesense_config is not None:
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

        # The record factory scrubs message text, not extras, so a key in
        # this dump would render verbatim regardless of the redaction set.
        log_params = params
        if typesense_config is not None:
            log_params = {**params, "typesense": typesense_config.model_dump(exclude={"api_key"})}
        self.logger.info("Creating adapter", type=adapter_type, params=log_params)
        return get_adapter(adapter_type, params)

    def _resolve_budget(self, *, initial_cost_usd: float) -> CompositeBudget | None:
        """Return the budget composite driving graceful shutdown.

        Preference order:

        1. ``deps.budget`` — a composite the CLI or a test built with all
           active limits and its own cost seed.
        2. Legacy ``compute.max_budget_usd`` — promoted to a single
           :class:`CostBudget` seeded with ``initial_cost_usd`` (spend
           already recorded under the run directory), so resumed runs
           re-enter with prior cost counted.

        Returns ``None`` when neither is set — the wait loop skips the
        budget branch entirely.
        """
        if self._injected_budget is not None:
            return self._injected_budget
        legacy_cost_limit = self.config.effective_max_budget_usd
        if legacy_cost_limit is None:
            return None
        return CompositeBudget(
            [
                CostBudget(
                    limit_usd=legacy_cost_limit,
                    initial_cost_usd=initial_cost_usd,
                )
            ]
        )

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
    def _is_auth_failure(trajectory: Trajectory) -> bool:
        """Return True when a trajectory terminated on a provider auth error.

        Auth errors are deterministic — the same request will fail the same
        way on retry — so they classify as non-retryable regardless of the
        broader ``API_ERROR`` bucket. Signal comes from the trailing SYSTEM
        message the loop appends on ``TerminationReason.API_ERROR``:
        ``"API error: LLM API call failed: … AuthenticationError …"``.
        """
        if trajectory.termination_reason != TerminationReason.API_ERROR:
            return False
        messages = getattr(trajectory, "messages", None) or []
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if not isinstance(content, str):
                continue
            # Match litellm-wrapped provider auth strings.
            if "AuthenticationError" in content:
                return True
            if '"code":401' in content or '"code": 401' in content:
                return True
            if '"code":403' in content or '"code": 403' in content:
                return True
            # Only inspect the most recent narrative-carrying message.
            break
        return False

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

        Auth-shaped ``API_ERROR`` trajectories short-circuit to
        non-retryable via :meth:`_is_auth_failure` — bad keys are
        deterministic across attempts.
        """
        if trajectory.termination_reason == TerminationReason.PROVISION_ERROR:
            return False
        if Orchestrator._is_auth_failure(trajectory):
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

        Also the single choke point where rate-limit probe mode's two halves
        meet: the orchestrator arms the agent client, but the simulator probe,
        the per-task budget re-check and the telemetry accumulator belong to the
        conductor. A conductor that does not declare support gets a raise here,
        because the alternative is a run that really absorbs 429s while its
        artifacts read all-default — see
        :func:`~tolokaforge.core.conductor.require_rate_limit_probe_support`.
        """
        if self.adapter is None:
            raise RuntimeError(
                "Conductor cannot be built before the adapter is loaded. "
                "Ensure load_tasks() has run successfully."
            )
        # ``None`` when the backend has no runner surface (in-memory / test,
        # or ``PerTrialRuntimeBackend`` — each trial owns its own runner
        # endpoint so no single address applies); the built-in
        # address-needing factories (``grader_rpc``, ``queue``) raise loudly
        # when they observe it. Downstream graders that do not need a
        # network address receive the ``None`` verbatim and ignore it, and
        # ``runner_rpc`` picks the ``runtime_backend`` dispatch path below
        # instead of dialling.
        runner_address = getattr(runtime_backend, "runner_address", None)
        # ``config.grader.name`` overrides the adapter's default for this
        # run; the queue subblock rides the same context so
        # ``queue_trial_grader_factory`` can build its broker + worker pool
        # without a second config lookup. Absent block keeps every existing
        # run's behaviour.
        grader_config = self.config.grader
        grader_name = (
            grader_config.name if grader_config and grader_config.name else None
        ) or self.adapter.trial_grader_name
        # In-process routing shim: only populated when the backend has no
        # static runner endpoint (``PerTrialRuntimeBackend`` — each trial
        # owns its own endpoint). Shared-stack keeps its address-only,
        # orchestrator-independent grader client so ADR-0038's seam
        # invariant is preserved for every case that can honour it.
        in_process_backend_shim = runtime_backend if runner_address is None else None
        trial_grader = load_trial_grader(grader_name)(
            TrialGraderContext(
                runner_address=runner_address,
                logger=self.logger,
                grader_config=grader_config,
                runtime_backend=in_process_backend_shim,
            )
        )
        # Drain any leftover from a prior aborted run on this orchestrator
        # instance before recording the fresh grader — a re-entered ``run()``
        # on the same instance must never close a stale grader from the
        # previous attempt.
        if self._trial_graders_to_close:
            self._close_trial_graders()
        self._trial_graders_to_close.append(trial_grader)

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
        factory = self._conductor_factory or load_conductor("in_process")
        conductor = factory(ctx)
        # Conductors are a plugin group, so the resolved implementation may be
        # anything a downstream package registered — and the agent client handed
        # in above is already armed.
        require_rate_limit_probe_support(
            conductor,
            self.config.orchestrator.rate_limit_probe,
            source="orchestrator",
        )
        return conductor

    def _task_description(self, task_id: str) -> TaskDescription:
        """The task's wire-format description, resolved once per adapter configuration.

        The registration check runs on the build, so every description in the
        cache has had its declared backend verified against the host registry —
        writing the cache around this method would skip it.

        "Once per adapter configuration", not "once per run": the TypeSense
        Docker rewrite invalidates the cache, so descriptions resolved before
        the stack starts are rebuilt against the rewritten params (#925).
        """
        if self.adapter is None:
            raise RuntimeError("Task descriptions cannot be resolved before the adapter is loaded.")
        cached = self._task_desc_cache.get(task_id)
        if cached is not None:
            return cached
        description = self.adapter.to_task_description(task_id)
        ensure_registered_adapter(description.adapter_type)
        self._task_desc_cache[task_id] = description
        return description

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

        ``run_id`` is supplied by the caller (computed once at the top of
        ``run()`` / read from the engine run-state file in ``run_worker()``)
        so trial identity is independent of where artifacts are written.
        """
        task_desc = self._task_description(task.task_id)
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
        """Return a representative manifest for the run's shared ``run``-scope
        stacks, or ``None`` if no ``run``-scope stack is declared.

        Each task carries an :class:`EnvironmentPatch` (input shape,
        pre-resolve); this method calls
        :func:`tolokaforge.core.project_loader.resolve` per task to bind
        the project-side and task-side patches into an
        :class:`EnvironmentManifest`, then compares only the ``run``-scope
        subset of each task's resolved plan.

        Per-scope INV-1 (ADR-0044 §5): every task's ordered ``run``-scope
        stack sequence must canonicalise to the same signature — same
        ``stack_id``, same round-tripped compose bytes, same
        ``runner_service``, same ``inputs``. ``task`` and ``trial`` scopes
        may differ freely across tasks; the composer resolves those
        per-task at ``provision_trial`` time. On agreement, the first
        task's manifest is returned verbatim as the representative (the
        composer filters to ``stack_scope=="run"`` at ``materialise_run``
        time, so noise scopes in the returned manifest are ignored on
        the run-connect path).

        Short-circuits to ``None``:

        - ``orchestrator.runtime="per_trial"`` coerces every stack to
          ``trial`` scope; the composer materialises per-trial from each
          task's own manifest.
        - Every task's ``plan_shape`` is ``TRIAL_SCOPED_ONLY`` — same
          reasoning; no ``run``-scope stack to materialise.
        - Every task agrees on an empty ``run``-scope subset (e.g. a
          ``MULTI_SCOPE`` plan mixing ``task`` and ``trial`` scopes,
          with no ``run``-scope stack in any task).

        Two call sites read this return value:

        - :meth:`run`: threads the representative into the composer's
          ``connect`` path so ``materialise_run`` sees a plan.
        - :meth:`run_worker`: raises when the return is non-``None``
          because a worker joining a run-scope-materialised run would
          connect to the parent's testcontainers-allocated address,
          which isn't propagated to workers.
        """
        from tolokaforge.core.project_loader import resolve

        override = self.config.orchestrator.runtime
        if override == "per_trial":
            return None
        if override is None and self.adapter is not None:
            task_manifests = [
                self._task_description(task.task_id).environment_manifest for task in self.tasks
            ]
            present = [m for m in task_manifests if m is not None]
            if present and all(m.plan_shape == PlanShape.TRIAL_SCOPED_ONLY for m in present):
                return None

        if not self.tasks:
            return None

        project_env = self.project.default_environment if self.project is not None else None
        resolved_by_task: dict[str, EnvironmentManifest | None] = {}
        for task in self.tasks:
            resolved_by_task[task.task_id] = resolve(project_env, task.environment_manifest)

        signatures_by_task: dict[str, tuple[_RunScopeStackSignature, ...]] = {
            task_id: _run_scope_signature(manifest)
            for task_id, manifest in resolved_by_task.items()
        }
        distinct_signatures = set(signatures_by_task.values())
        if len(distinct_signatures) > 1:
            digests = {task_id: _hash_signature(sig) for task_id, sig in signatures_by_task.items()}
            offending_stack_ids = sorted(
                {stack_id for sig in distinct_signatures for stack_id, *_ in sig}
            )
            raise RuntimeError(
                "Run has tasks that disagree on the run-scope subset of their "
                "composition plan — every task must declare the same ordered "
                "sequence of run-scope stacks (matching stack_id, compose bytes, "
                "runner_service, and inputs). Task/trial-scope stacks may differ "
                "freely (composer materialises those per-trial). "
                f"Per-task run-scope digests: {digests!r}. "
                f"Offending stack ids: {offending_stack_ids!r}. "
                "Split into separate runs or converge the run-scope subset."
            )

        (only_signature,) = distinct_signatures
        if not only_signature:
            return None
        for manifest in resolved_by_task.values():
            if manifest is not None:
                return manifest
        return None

    def _any_task_declares_environment_manifest(self) -> bool:
        """True iff at least one task's resolved description declares an
        ``environment_manifest``.

        The composer owns substrate provisioning whenever any task's
        manifest is present — the built-in engine containers stay unused
        (only their images are built for ``:local`` aliases). Callers
        that decide between "start built-in engine" and "images-only"
        route on this signal.
        """
        if self.adapter is None:
            return False
        for task in self.tasks:
            if self._task_description(task.task_id).environment_manifest is not None:
                return True
        return False

    def _coerce_plan_shape_for_override(self, override: str | None) -> None:
        """Rewrite every task's resolved plan ``stack_scope`` to match the
        operator's ``orchestrator.runtime`` coercion knob.

        ``"shared"`` coerces every stack to ``stack_scope="run"``;
        ``"per_trial"`` coerces to ``stack_scope="trial"``. Refuses
        ``RuntimeError`` on a task whose plan has more than one stack —
        the two coercion knobs are defined for single-stack packs only,
        and multi-stack packs must declare per-stack scope explicitly
        (ADR-0044 §6 deprecation).

        Any other ``override`` value (registered non-shared, non-per_trial
        backend name — only ``"in_memory"`` in-tree today) is a legit
        backend swap that carries no plan-shape semantics; the helper
        no-ops in that case.

        Called at :meth:`run` / :meth:`run_worker` entry BEFORE
        :meth:`_extract_run_env_manifest` fires, so the composer at
        ``provision_trial`` time reads the already-coerced scopes.
        """
        if override is None or override not in ("shared", "per_trial"):
            return
        target_scope: StackScope = "run" if override == "shared" else "trial"
        if self.adapter is None:
            return
        for task in self.tasks:
            manifest = self._task_description(task.task_id).environment_manifest
            if manifest is None:
                continue
            if len(manifest.stacks) > 1:
                raise RuntimeError(
                    f"orchestrator.runtime={override!r} cannot coerce task "
                    f"{task.task_id!r}: its plan declares {len(manifest.stacks)} "
                    "stacks and the coercion knob is defined for single-stack "
                    "packs only. Multi-stack packs must declare stack_scope on "
                    "each stack directly. See ADR-0044 §6 (orchestrator.runtime "
                    "deprecation)."
                )
            for decl in manifest.stacks:
                decl.stack_scope = target_scope

    def _build_log_capture(self, output_dir: Path) -> LogCaptureConfig:
        """Build the run's per-service log-capture policy from ``compute``.

        Reads ``compute.log_tail`` + ``compute.capture_logs_on_success``
        (their schema defaults apply when no ``compute`` block is declared),
        anchoring capture under ``output_dir``. One instance is shared by the
        runtime backend and the trial executor so both write to the same tree.
        """
        compute = self.config.compute or ComputeConfig()
        return LogCaptureConfig(
            output_root=output_dir,
            tail=compute.log_tail,
            on_success=compute.capture_logs_on_success,
        )

    def _construct_runtime_backend(
        self,
        runner_address: str,
        env_manifest: EnvironmentManifest | None = None,
        run_id: str = "run",
        log_capture: LogCaptureConfig | None = None,
    ) -> RuntimeBackend:
        """Construct the composer-driven runtime backend.

        Automatic path and the ``orchestrator.runtime`` coercion knobs
        (``"shared"`` / ``"per_trial"``) both resolve to
        :class:`SharedStackRuntimeBackend` — the composer sequences the
        resolved plan's per-scope substrate at ``connect`` and
        ``provision`` time. When ``env_manifest`` is passed the composer
        materialises the task-declared plan; without it the backend
        connects to the built-in engine at ``runner_address``.

        Any other registered ``orchestrator.runtime`` value (only
        ``"in_memory"`` in-tree today) is a legit backend swap and
        routes through :func:`load_runtime_backend` unchanged.

        ``log_capture`` is threaded onto the backend so its
        provision-failure path can capture per-service logs before
        teardown.

        Called when no backend is injected via
        ``Orchestrator.__init__(runtime_backend=...)``.
        """
        override = self.config.orchestrator.runtime
        if override is None:
            runtime_choice = "shared"
            source = "composed"
        elif override in ("shared", "per_trial"):
            runtime_choice = "shared"
            source = f"override:{override}"
        else:
            runtime_choice = override
            source = f"override:{override}"

        factory = load_runtime_backend(runtime_choice)
        adapter_type = (
            self.config.evaluation.harness_adapter.type
            if self.config.evaluation.harness_adapter
            else None
        )
        # No run-scope manifest survives extraction when the plan is fully
        # trial-scoped (automatic short-circuit) or when the operator coerced
        # to ``per_trial``. In that case the composer still owns provisioning
        # from each task's own manifest — ``per_trial_mode`` pins the backend's
        # per-trial branch so ``connect`` no-ops ``materialise_run`` and
        # ``provision`` routes through ``composer.provision_trial``. Signalled
        # only when at least one task actually declares a manifest; a pack
        # that declares none stays on built-in-engine mode.
        per_trial_mode = (
            env_manifest is None
            and override != "shared"
            and self._any_task_declares_environment_manifest()
        )
        backend = factory(
            RuntimeBackendBuildContext(
                runner_address=runner_address,
                env_manifest=env_manifest,
                run_id=run_id,
                seeds=self._project_seed_registry(),
                log_capture=log_capture,
                events=self._events,
                mount_docker_socket=_run_needs_docker_cli(adapter_type, self.tasks),
                per_trial_mode=per_trial_mode,
            )
        )
        self.logger.info(
            "runtime.backend.selected",
            backend=type(backend).__name__,
            source=source,
        )
        return backend

    def _project_seed_registry(self) -> dict[str, Any]:
        """Return the project's ``assets.seeds`` map for backend
        construction. Empty dict when the project has no assets block."""
        if self.project is None or self.project.assets is None:
            return {}
        return dict(self.project.assets.seeds)

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

    def _perform_declared_compose_image_builds(self, stack_requirements: Any) -> None:
        """Build adapter-declared compose images once per run, before any
        trial provisions.

        Iterates ``DockerStackRequirements.image_builds`` and, for each
        entry, invokes ``docker compose -f <compose_file> build <service>``
        — skipped when the service's pinned image already resolves locally,
        so cache-hit runs pay no build cost. Raises on the first build
        failure so a broken Dockerfile aborts the run at prep time, rather
        than surfacing as a ``PROVISION_ERROR`` naming compose in every
        trial of that task.
        """
        if stack_requirements is None:
            return
        image_builds = getattr(stack_requirements, "image_builds", ())
        for build in image_builds:
            self._perform_one_compose_image_build(build)

    def _perform_one_compose_image_build(self, build: Any) -> None:
        """Build one adapter-declared compose service image, skipping the
        subprocess when the pinned image already resolves locally."""
        import subprocess

        image_ref = _compose_service_image_ref(build.compose_file, build.service)
        if image_ref is not None and _local_image_exists(image_ref):
            self.logger.info(
                "compose image already resolves locally; skipping declared build",
                compose_file=str(build.compose_file),
                service=build.service,
                image=image_ref,
            )
            return
        self.logger.info(
            "Building adapter-declared compose image",
            compose_file=str(build.compose_file),
            service=build.service,
        )
        subprocess.run(
            ["docker", "compose", "-f", str(build.compose_file), "build", build.service],
            check=True,
        )

    def _build_trial_executor(
        self,
        runtime_backend: RuntimeBackend,
        conductor: Conductor,
        output_dir: Path,
    ) -> TrialExecutor:
        """Compose the per-run :class:`TrialExecutor` (ADR-0015).

        The executor owns the per-trial substrate lifecycle bracket
        (``provision`` / ``await_ready`` / ``endpoints`` / ``teardown``)
        around ``conductor.run``. The orchestrator submits
        ``trial_executor.execute`` to the worker pool in place of
        ``conductor.run``; the bracket runs on the worker thread so
        provisioning parallelism equals worker count.

        Threads :attr:`_events` in so the executor can fire
        ``trial_provisioned`` after :meth:`RuntimeBackend.await_ready`
        returns — the runtime is the only place with a handle on the
        materialised infrastructure snapshot.

        ``output_dir`` is the run's output root, threaded so the executor can
        amend a trial's ``metrics.yaml`` with host-side per-trial values.
        """
        from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

        return ProvisioningTrialExecutor(
            runtime_backend=runtime_backend,
            conductor=conductor,
            logger=self.logger,
            output_dir=output_dir,
            artifact_writer=self._artifact_writer,
            events=self._events,
        )

    def _verify_isolation_compatibility(self, runtime_backend: RuntimeBackend) -> None:
        """Refuse to start the run when the runtime backend's composer
        has no dispatcher registered for a service's ``isolation`` label.

        Per-service dispatcher admission (ADR-0044 §5, INV-2): every
        service's ``isolation`` label must have a registered dispatcher
        on the backend's composer. The composer's own cycle-time refusal
        (:mod:`tolokaforge.core.service_lifecycle_dispatchers` raising
        :class:`ProvisionError` at ``stage="cycle"``) is the second line
        of defence for a registry that changes after startup; this
        pre-flight catches the structural gap up front.

        The :attr:`IsolationMode.PER_TRIAL_STACK` short-circuit at the
        top covers backends that materialise every stack per trial —
        every dispatcher label is honourable there regardless of the
        composer's registry.

        Reads ``runtime_backend.composer.dispatcher_registry`` when
        present. A backend without a composer (or a composer without a
        registry) falls back on the module-global
        :data:`~tolokaforge.core.service_lifecycle_dispatchers.DISPATCHER_REGISTRY`,
        which carries the three built-in labels
        (``shared`` / ``reset`` / ``ephemeral``) — every label the closed
        :data:`~tolokaforge.runner.models.ServiceIsolation` vocab
        defines. The refusal fires only when a composer is constructed
        with a partial registry.

        Raises :class:`RuntimeError` naming
        ``(task_id, stack_id, service_name, isolation, stack_scope)``
        for each violation.
        """
        from tolokaforge.core.runtime import IsolationMode
        from tolokaforge.core.service_lifecycle_dispatchers import (
            DISPATCHER_REGISTRY,
        )

        if runtime_backend.isolation_mode is IsolationMode.PER_TRIAL_STACK:
            return

        if self.adapter is None:
            raise RuntimeError(
                "Isolation-compatibility check requires the adapter to be loaded first."
            )

        composer = getattr(runtime_backend, "composer", None)
        dispatcher_registry = getattr(composer, "dispatcher_registry", None)
        if dispatcher_registry is None:
            dispatcher_registry = DISPATCHER_REGISTRY
        available_labels = frozenset(dispatcher_registry.keys())

        violations: list[tuple[str, str, str, str, str]] = []
        for task in self.tasks:
            manifest = self._task_description(task.task_id).environment_manifest
            if manifest is None:
                continue
            for decl in manifest.stacks:
                stack_services = _services_in_stack(manifest, decl)
                for service_name, spec in sorted(stack_services.items()):
                    if spec.isolation not in available_labels:
                        violations.append(
                            (
                                task.task_id,
                                decl.stack_id,
                                service_name,
                                spec.isolation,
                                decl.stack_scope,
                            )
                        )

        if violations:
            raise RuntimeError(
                "Composer-driven runtime cannot honour every requested "
                "`isolation` label — the composer's dispatcher registry has "
                "no entry for one or more labels declared by the tasks. "
                "Offending (task_id, stack_id, service_name, isolation, "
                f"stack_scope): {violations!r}. Register the missing "
                "dispatcher(s), or move the affected services onto a "
                "`trial`-scope stack (per-trial materialisation handles every "
                "label uniformly)."
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

        Populates :attr:`_total_index_by_key` as a side effect so the
        ``trial_started`` emission site can render a run-wide
        ``[N/M]`` prefix without recomputing.
        """
        pending_trials: list[tuple[str, int]] = []
        for task in tasks:
            for trial_idx in range(repeats):
                if skip_completed and skip_completed(task.task_id, trial_idx):
                    continue
                pending_trials.append((task.task_id, trial_idx))

        if self.config.orchestrator.shuffle_trials:
            random.shuffle(pending_trials)
        self._total_index_by_key = {key: idx for idx, key in enumerate(pending_trials)}
        return pending_trials

    def _ensure_typesense_started(self) -> None:
        """Start TypeSense server if configured for local mode.

        This must be called before adapter creation to ensure the adapter
        gets resolved port/api_key values, and before any log record that
        could carry the API key: registering the key is what puts it in the
        redaction set and in the ``TOLOKAFORGE_SECRETS_JSON`` payload the
        runner container is built with. A key pinned in the run config is
        registered before the start block, so the start path's own records
        are already redacted; a generated key exists only after the server
        resolves it, so it is registered on the way out.
        """
        typesense_config = self.config.orchestrator.effective_typesense()
        if typesense_config is None:
            return
        if typesense_config.api_key is not None:
            register_runtime_secret("TYPESENSE_API_KEY", typesense_config.api_key)
        needs_start = typesense_config.mode == "local" and (
            typesense_config.port == "auto" or typesense_config.api_key is None
        )
        if needs_start:
            typesense_config = self._start_local_typesense_server(typesense_config)
        if typesense_config.api_key is not None:
            register_runtime_secret("TYPESENSE_API_KEY", typesense_config.api_key)

    def _start_local_typesense_server(self, typesense_config: TypeSenseConfig) -> TypeSenseConfig:
        """Start the local TypeSense container and return the resolved config.

        The returned config carries the port and API key the server settled
        on; ``self.config.orchestrator.typesense`` is replaced with it, so the
        adapter reads the same resolved values.
        """
        try:
            from tolokaforge.core.search.typesense_server import create_typesense_server

            # The record factory scrubs message text, not extras, so a key in
            # this dump would render verbatim regardless of the redaction set.
            self.logger.info(
                "Starting local TypeSense server",
                config=typesense_config.model_dump(exclude={"api_key"}),
            )
            self._typesense_server = create_typesense_server(
                port=typesense_config.port,
                api_key=typesense_config.api_key,
                data_dir=typesense_config.data_dir,
                image=typesense_config.image,
                container_name=typesense_config.container_name,
                timeout=typesense_config.timeout,
                cleanup_on_exit=typesense_config.cleanup_on_exit,
            )
            started = self._typesense_server.start()
        except ImportError as e:
            raise RuntimeError(
                f"TypeSense is configured but the server module is not available: {e}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to start TypeSense server: {e}") from e

        # Outside the try on purpose: raised inside, this message would reach the
        # operator wrapped in the "Failed to start TypeSense server" prefix above.
        if not started:
            # ``port`` is resolved inside ``start()``, so a failure before that
            # point — an unimportable Docker foundation layer — leaves the
            # manager's sentinel, which reads as a real address if rendered.
            where = (
                f"at {self._typesense_server.host}:{self._typesense_server.port}"
                if self._typesense_server.port > 0
                else f"on {self._typesense_server.host} (no port was ever resolved)"
            )
            raise RuntimeError(
                f"orchestrator.typesense: the local TypeSense server {where} never became "
                f"ready — either the Docker foundation layer is unavailable, or the "
                f"container did not answer its health and collections probes within "
                f"timeout={typesense_config.timeout}s. Aborting the run: every "
                f"search_policy call would run against a search plane that is not there. "
                f"Check `docker ps` and the TypeSense container logs, raise "
                f"orchestrator.typesense.timeout, or set orchestrator.typesense.enabled to "
                f"false to run without a knowledge base."
            )

        # Update config object with resolved port/api_key for adapter use
        resolved_config = typesense_config.model_dump()
        resolved_config["port"] = self._typesense_server.port
        resolved_config["api_key"] = self._typesense_server.api_key
        resolved = TypeSenseConfig(**resolved_config)
        self.config.orchestrator.typesense = resolved
        self.logger.info(
            "TypeSense server started",
            host=self._typesense_server.host,
            port=self._typesense_server.port,
        )
        return resolved

    def _injected_typesense_address(self, typesense_config: TypeSenseConfig) -> "TypeSenseAddress":
        """Return the address the runner container is created with.

        A server this process started is bridged onto ``runner-net``, so the
        runner reaches it at the alias on the container port — an address that
        is static by construction. The run config still holds the *host-side*
        address at this point, and inside the runner container that address is
        the runner itself (#925), so it is never the answer for a bridged
        server.

        Raises:
            RuntimeError: no server of ours is running and the configured port
                is still ``"auto"``, so there is no address to inject. Reached
                when ``run()`` is handed pre-loaded tasks and therefore never
                calls ``load_tasks()``.
            RuntimeError: the address that would be injected verbatim is a
                loopback host, which inside the runner container is the runner
                itself. Reached by ``mode: local`` with both a pinned port and
                a pinned api_key (the managed start is skipped, so nothing is
                bridged) and by ``mode: remote`` with ``host`` left at its
                loopback default.
        """
        from tolokaforge.docker.stacks import TypeSenseAddress

        if self._typesense_server is not None:
            return TypeSenseAddress(host=_TYPESENSE_NETWORK_ALIAS, port=_TYPESENSE_CONTAINER_PORT)
        if typesense_config.port == "auto":
            raise RuntimeError(
                f"orchestrator.typesense: the TypeSense plane is enabled in mode "
                f"'{typesense_config.mode}' but its port is still 'auto' and no server was "
                f"started for this run, so there is no address to give the runner "
                f"container. Aborting the run: 'auto' is not an address, and a runner "
                f"created with it would fail every search_policy call. Pin "
                f"orchestrator.typesense.port to the port the server answers on, or set "
                f"orchestrator.typesense.enabled to false to run without a knowledge base."
            )
        if typesense_config.host in _LOOPBACK_HOSTS:
            self._refuse_loopback_injection(typesense_config)
        return TypeSenseAddress(host=typesense_config.host, port=typesense_config.port)

    def _refuse_loopback_injection(self, typesense_config: TypeSenseConfig) -> NoReturn:
        """Abort the run: the address about to be injected is a loopback host.

        Inside the runner container a loopback address is the runner itself
        (#925), so no TypeSense server answers there and every
        ``search_policy`` call in every trial would fail against a plane the
        run claims to have. A bridged server never reaches this refusal — it
        is injected as the network alias before the loopback check runs.
        """
        if typesense_config.mode == "local":
            remedy = (
                "Fix: set orchestrator.typesense.port to 'auto' (or leave api_key unset) "
                "so this process starts and bridges its own server — a bridged server is "
                "injected as the network alias, never as a loopback address — or point "
                "orchestrator.typesense.host at an address reachable from inside the "
                "runner container."
            )
        else:
            remedy = (
                "Fix: set orchestrator.typesense.host to the external server's address "
                "as the runner container reaches it — a loopback host names no external "
                "server — or set orchestrator.typesense.enabled to false to run "
                "without a knowledge base."
            )
        raise RuntimeError(
            f"orchestrator.typesense: the address that would be injected into the "
            f"runner container is '{typesense_config.host}:{typesense_config.port}', a "
            f"loopback address. Inside the runner container a loopback address is the "
            f"runner itself, so no TypeSense server answers there and every "
            f"search_policy call would fail. Aborting the run before any trial "
            f"(mode '{typesense_config.mode}'). {remedy}"
        )

    def _typesense_stack_kwargs(self) -> dict[str, "TypeSenseAddress"]:
        """Stack-factory kwargs carrying this run's TypeSense address.

        Empty when the run has no plane, so the runner container is created
        without the variables and "variable present" == "a plane was
        configured".

        A pinned API key is registered here as well as at server start:
        ``run()`` skips ``load_tasks()`` when handed pre-loaded tasks, so this
        method — which runs on every stack build, before the factory
        serializes the manager into ``TOLOKAFORGE_SECRETS_JSON`` — is the only
        registration site on that path.
        """
        typesense_config = self.config.orchestrator.effective_typesense()
        if typesense_config is None:
            return {}
        if typesense_config.api_key is not None:
            register_runtime_secret("TYPESENSE_API_KEY", typesense_config.api_key)
        return {"typesense_address": self._injected_typesense_address(typesense_config)}

    def _connect_typesense_to_runner_network(self, service_stack: Any) -> None:
        """Connect the TypeSense container to the core stack's Docker network.

        The runner container was created knowing the alias and the container
        port, which are static by construction — but that address resolves only
        while TypeSense is a member of ``runner-net``. Building that membership
        is the whole of this method: nothing here touches the run config, the
        adapter, or the description cache.

        Every failure aborts the run. Without the bridge the runner asks for an
        alias no network resolves, and every ``search_policy`` call in every
        trial fails.
        """
        import docker as docker_lib

        injected = f"{_TYPESENSE_NETWORK_ALIAS}:{_TYPESENSE_CONTAINER_PORT}"
        typesense_config = self.config.orchestrator.typesense
        if typesense_config is None:
            raise RuntimeError(
                "orchestrator.typesense: the TypeSense bridge ran with no TypeSense "
                "configuration. A server was started for this run, so the run config "
                "must still carry an orchestrator.typesense block here — it names the "
                "host-side address a failed bridge reports."
            )
        host_side = f"{typesense_config.host}:{typesense_config.port}"

        ts_stack = self._typesense_server._stack
        ts_container_obj = ts_stack._containers.get("typesense") if ts_stack else None
        if ts_container_obj is None:
            raise RuntimeError(
                f"orchestrator.typesense: the TypeSense stack holds no 'typesense' "
                f"container to bridge onto the runner network — the shape a start that "
                f"was rolled back leaves behind. Aborting the run: the runner asks for "
                f"{injected} and no container would answer it, while the host side "
                f"expects a server at {host_side}. Check `docker ps` and the TypeSense "
                f"container logs."
            )

        runner_net = service_stack._networks.get("runner-net")
        if runner_net is None:
            raise RuntimeError(
                f"orchestrator.typesense: the core stack exposes no 'runner-net' network, "
                f"so the TypeSense container at {host_side} cannot be joined to the one "
                f"the runner resolves {injected} on. Aborting the run: every "
                f"search_policy call would fail. Check that the core stack started "
                f"before the bridge ran."
            )

        client = docker_lib.from_env()
        ts_container = client.containers.get(ts_container_obj.container_id)
        docker_network = client.networks.get(runner_net.network_id)

        docker_network.connect(ts_container, aliases=[_TYPESENSE_NETWORK_ALIAS])
        self.logger.info(
            "Connected TypeSense to runner network",
            network=runner_net.name,
            container=ts_container.name,
            alias=injected,
        )

    def load_tasks(self) -> None:
        """Load tasks using configured adapter"""
        # Ensure TypeSense is started BEFORE adapter creation
        # This allows the adapter to get resolved port/api_key
        self._ensure_typesense_started()

        # Create adapter if not already created
        if self.adapter is None:
            self.adapter = self._create_adapter()

        # Coding-harness capability gate: refuse a run declaring
        # ``models.agent.harness`` on an adapter that has not opted into the
        # harness surface (``supports_coding_harness`` class attr from
        # ``CodingHarnessAdapterMixin``). Fail here — before any container
        # work — with a message that names the adapter and the harness slug
        # so the operator sees which side of the pair does not match.
        agent_model_config = self.config.models.get("agent") if self.config.models else None
        if agent_model_config is not None and agent_model_config.harness is not None:
            if not getattr(self.adapter, "supports_coding_harness", False):
                adapter_type_name = (
                    getattr(
                        self.config.evaluation.harness_adapter,
                        "type",
                        "native",
                    )
                    if self.config.evaluation.harness_adapter
                    else "native"
                )
                raise RuntimeError(
                    f"models.agent.harness={agent_model_config.harness!r} but "
                    f"adapter {adapter_type_name!r} does not opt into coding-"
                    "harness mode. An adapter opts in by inheriting "
                    "``tolokaforge_coding_harnesses.adapter_support."
                    "CodingHarnessAdapterMixin`` (which sets "
                    "``supports_coding_harness = True``). Either drop "
                    "``models.agent.harness`` to run the engine's LLM loop, "
                    "or switch to an adapter that supports the harness "
                    "surface (currently: terminal_bench, native)."
                )

        # Get task IDs from adapter
        task_ids = self.adapter.get_task_ids()

        # Load each task
        strict = self.config.orchestrator.strict_task_load
        loaded: list[TaskConfig] = []
        for task_id in task_ids:
            try:
                loaded.append(self.adapter.get_task(task_id))
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"Failed to load task {task_id!r}: {e} "
                        "(orchestrator.strict_task_load=true — the run refuses "
                        "to start with a silently shorter task list)"
                    ) from e
                self.logger.error("Failed to load task", task_id=task_id, error=str(e))
        self.tasks.extend(loaded)

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

    def _reject_ungradeable_packs(self) -> None:
        """Refuse a run whose selected packs cannot be graded as written.

        One pass over every selected task, before the first trial is paid for.
        Each task's grading block goes through the same predicate
        ``tolokaforge validate`` applies, so a tool name no actor of the task can
        call, an argument name its schema forbids, an uncompilable ``regex``, a
        state hash nothing reads, and every migration rejection the typed grading
        blocks carry are all heard here rather than at grade time — where the
        first two are charged to the agent and the rest lose the trial. A task
        naming no grading block at all is refused by the same pass, on the same
        grounds: the adapter it declares grades from a file it has not supplied.

        Every grading offender is named in one raise: an author fixing a run's
        packs wants the list, not the first entry. What the gate could not check
        is logged and fails nothing.

        The aggregate is over what the grading predicate rejects. Resolving each
        task's description happens outside the per-task catch, so whatever *that*
        raises — an adapter the host has not installed, a grading file that is
        not YAML, or a malformed grading shape, the file's own or one of its keys,
        the last two being what the native adapter answers while it builds the
        description — aborts on the first task carrying it, and the tasks after
        it are never read. The list is of packs that load and cannot be graded;
        a pack that does not load stops the pass where it stands.
        """
        fail_on = self.config.evaluation.grading_validation.fail_on
        rejected: list[str] = []
        for task in self.tasks:
            failure = self._grading_rejection(task, fail_on=fail_on)
            if failure is not None:
                rejected.append(failure)
        if not rejected:
            return
        raise ValueError(
            "These selected tasks cannot be graded as written, so no trial was run:\n"
            + "\n".join(rejected)
            + f"\nevaluation.grading_validation.fail_on is {fail_on.value!r}, so a "
            "finding of that class or more severe fails the run. `tolokaforge validate "
            "--tasks <glob>` reports the same findings against the same packs, and "
            "decides its own exit code by the default fail_on rather than this run's."
        )

    def _grading_rejection(
        self, task: TaskConfig, *, fail_on: GradingFindingSeverity
    ) -> str | None:
        """What one task's grading block costs the run, or ``None`` if nothing.

        The description is resolved through :meth:`_task_description` rather than
        the adapter directly, so the adapter-registration guard is part of this
        gate and a task naming an uninstalled backend is rejected here too.

        An authoring defect the grading predicate answers becomes a named line
        rather than propagating, because the run's operator wants the list. The
        boundary is which surface answers it, not how bad it is: a defect the
        *loader* answers — a malformed grading shape, on either the file or one of
        its keys — is raised while the description above is resolved, ahead of the
        ``try`` below, so it aborts the pass with its own sentence instead of
        joining the list. #880 owns moving that class into it. Anything outside
        both sets is the harness's own bug and propagates, rather than sending an
        author to read a file that is fine.

        The task's grading source is resolved before its block is read, so a task
        that names none is refused here too: the adapter that grades from a file
        otherwise finds out while the trial's artifacts are written, with every
        token already spent.
        """
        adapter_type = self._task_description(task.task_id).adapter_type
        task_dir = self.adapter.get_task_dir(task.task_id)
        source = grading_source_under_adapter(task, task_dir, adapter_type)
        if source.kind is GradingSourceKind.WITHHELD:
            return f"* {task.task_id} — {source.reason}"
        if source.path is None:
            self._warn_grading_unchecked(task.task_id, "grading", source.reason)
            return None
        try:
            report = validate_grading_yaml(
                source.path,
                inventory=tool_inventory_under_adapter(task, task_dir, adapter_type),
                replay_world=replay_world_under_adapter(task, task_dir, adapter_type),
                hash_sources=self.adapter.grading_hash_source_layer(task, task_dir),
                seeded_tables=seeded_tables_under_adapter(task, task_dir, adapter_type),
                combine_layer=self.adapter.grading_combine_layer(),
                fail_on=fail_on,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return f"* {task.task_id} — {exc}"
        for skip in report.unchecked:
            self._warn_grading_unchecked(task.task_id, skip.where, skip.reason)
        return None

    def _warn_grading_unchecked(self, task_id: str, where: str, reason: str) -> None:
        """Report one thing the gate could not check beside the task it read.

        A gate that checked nothing must not read as a clean bill of health, whether
        the unanswerable part is a rule inside the block or the whole grading source.
        """
        self.logger.warning(
            "Grading validation could not check part of this task's block",
            task_id=task_id,
            where=where,
            reason=reason,
        )

    def _build_agent_client(self, agent_config: ModelConfig) -> LLMClient:
        """Build the agent-side wire client for this run.

        The factory seam routes through :class:`FallbackLLMClient` when the CLI
        wired ``--fallback-models``; otherwise the bare client ships, carrying
        the run's rate-limit probe config.

        A fallback chain and probe mode are mutually exclusive: a chain that
        switches models mid-probe would attribute one model's 429s to another
        and corrupt the measurement the probe exists to produce. The
        combination is rejected here rather than silently dropping either side.
        """
        probe = self.config.orchestrator.rate_limit_probe
        if self._agent_client_factory is not None:
            if probe.enabled:
                raise ValueError(
                    "orchestrator.rate_limit_probe.enabled is incompatible with a "
                    "fallback model chain (--fallback-models): switching models "
                    "mid-probe corrupts the served-throughput measurement. Run the "
                    "probe against a single model."
                )
            return self._agent_client_factory(agent_config)
        return LLMClient(agent_config, rate_limit_probe=probe)

    def run(
        self,
        *,
        run_id: str | None = None,
        output_dir: Path | None = None,
    ) -> Path:
        """Execute all tasks with configured trials.

        Returns the resolved absolute path of the timestamped run directory
        created by this invocation. The path is the same directory the
        orchestrator wrote every trial artifact, report, and run-state file
        into; callers publish or open it after the run completes.

        Callers that need to know the run directory before ``run()`` returns
        (e.g. the CLI, which prints a banner naming the directory before the
        run begins) resolve it via :func:`resolve_run_directory` and pass the
        pair back in via ``run_id`` and ``output_dir``. When both are
        ``None``, ``run()`` calls :func:`resolve_run_directory` itself. Both
        must be supplied together — supplying exactly one raises
        :class:`ValueError`.
        """
        if (run_id is None) != (output_dir is None):
            missing = "output_dir" if run_id is not None else "run_id"
            raise ValueError(
                f"Orchestrator.run requires run_id and output_dir together; missing {missing}"
            )
        if run_id is None:
            run_id, output_dir = resolve_run_directory(self.config.evaluation.output_dir)
        assert output_dir is not None
        output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure TypeSense is started and tasks are loaded
        if not self.tasks:
            self._events.phase_changed(phase="loading_tasks")
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
                config_path=str(self._config_path) if self._config_path is not None else "",
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
        write_engine_run_state(
            output_dir,
            run_id=run_id,
            presets_file=get_overlay_path(),
            models_fingerprint=compute_models_fingerprint(),
            adapter_fingerprints=self._adapter_fingerprints(),
        )

        # Create agent and user clients
        agent_config = self.config.models.get("agent")
        user_config = self.config.models.get("user")

        if not agent_config:
            self.logger.error("Agent model configuration required")
            raise ValueError("Agent model configuration required")

        user_config = require_user_simulator_config(user_config)

        # Resolve the run-level judge model and reject the run up front if any
        # selected task needs a judge but none is configured (fail loud).
        judge_config = self._resolve_judge_config()
        self._reject_ungradeable_packs()

        # Log model configuration for all roles
        self.logger.info(
            "Model configuration",
            agent_model=f"{agent_config.provider}/{agent_config.name}",
            user_model=f"{user_config.provider}/{user_config.name}",
            judge_model=(f"{judge_config.provider}/{judge_config.name}" if judge_config else None),
        )

        agent_client = self._build_agent_client(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.effective_max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.effective_max_requests_per_second)
            self.logger.info(
                "Global request limiter enabled",
                max_requests_per_second=self.config.effective_max_requests_per_second,
            )

        # The deprecated ``orchestrator.runtime`` override coerces every task's
        # plan-shape here — the composer at ``provision_trial`` time reads the
        # already-coerced scopes. Fires BEFORE ``_extract_run_env_manifest`` so
        # the extract sees the coerced plan.
        self._coerce_plan_shape_for_override(self.config.orchestrator.runtime)

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
                # The runner container is created knowing where TypeSense is,
                # so nothing has to rewrite a task's address after the fact.
                core_stack_kwargs.update(self._typesense_stack_kwargs())
                adapter_type = (
                    self.config.evaluation.harness_adapter.type
                    if self.config.evaluation.harness_adapter
                    else None
                )
                if _run_needs_docker_cli(adapter_type, self.tasks):
                    self.logger.info(
                        "Docker CLI required in runner image "
                        "(terminal-bench adapter or compose-variant tools detected)"
                    )
                    core_stack_kwargs["enable_docker_cli"] = True
                    # Fail loud before any container work: INSTALL_DOCKER_CLI
                    # is a Dockerfile build arg the orchestrator sets, so it
                    # only takes effect on a locally-built runner. A pulled
                    # runner would die on the first tool call with
                    # `[Errno 2] No such file or directory: 'docker'`.
                    import tolokaforge as _engine_pkg
                    from tolokaforge.core.models.docker_config import DockerConfig
                    from tolokaforge.docker.builder import repo_root
                    from tolokaforge.docker.image_source_policy import (
                        check_runner_docker_cli_available,
                    )

                    _docker_cfg = self.config.docker or DockerConfig()
                    check_runner_docker_cli_available(
                        needs_docker_cli=True,
                        request=_docker_cfg.image_source,
                        is_wheel_install=not (repo_root() / "pyproject.toml").is_file(),
                        engine_version=_engine_pkg.__version__,
                    )
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
                # Thread the run's docker config through to the stack so
                # ``docker.image_source`` (CLI flag, TOLOKAFORGE_IMAGE_SOURCE
                # env, or YAML) actually reaches ``EngineStack._maybe_pull_
                # service_image``. Without this explicit forwarding the
                # entire pull-vs-build policy stays inert — the stack
                # factories default ``config=None`` → ``DockerConfig()`` →
                # ``image_source='auto'``, so an operator setting
                # ``--image-source build`` on a wheel install would still
                # get a silent pull.
                from tolokaforge.core.models.docker_config import DockerConfig

                docker_config = self.config.docker or DockerConfig()
                # Route ``grader.expose_substrate`` from the run config into
                # the runner container so it registers the SubstrateService
                # gRPC servicer alongside RunnerService (same listen port).
                # Absent block == absent field == off.
                if self.config.grader is not None and self.config.grader.expose_substrate:
                    core_stack_kwargs["expose_substrate"] = True
                service_stack = stack_factory(config=docker_config, **core_stack_kwargs)
                # Any task-declared manifest hands the substrate to the
                # composer — the task-declared compose owns the runner +
                # db-service, so the built-in engine containers go unused.
                # The engine images still need to be BUILT so task compose
                # files can reference the ``:local`` aliases, but the
                # container-start is skipped.
                task_stack_mode = self._any_task_declares_environment_manifest()
                if task_stack_mode:
                    self.logger.info(
                        "Preparing Docker engine images (task-declared-stack mode: "
                        "images built + aliased, built-in containers not started)..."
                    )
                    # Fire the same phase_changed pair the shared branch fires,
                    # so the Components widget populates during the (long)
                    # image-build window instead of staying empty until the
                    # first trial provisions. Snapshots carry declared status
                    # only — the engine containers themselves are never
                    # started in task-declared-stack mode.
                    self._events.phase_changed(
                        phase="starting_services",
                        detail="building engine images",
                        services=_declared_engine_service_snapshots(service_stack),
                    )
                    service_stack.build_and_prepare()
                    self._ensure_engine_image_local_aliases(service_stack)
                    self._perform_declared_compose_image_builds(stack_requirements)
                    self._events.phase_changed(
                        phase="services_ready",
                        detail="engine images ready (per-trial stacks own runtime)",
                        services=_declared_engine_service_snapshots(service_stack),
                    )
                    runner_address = None
                    self.logger.info("EngineStack prepared (images ready, no containers started)")
                else:
                    self.logger.info(
                        "Building Docker images and starting containers "
                        "(this may take a few minutes on first run)..."
                    )
                    self._events.phase_changed(
                        phase="starting_services",
                        detail="docker compose up",
                        services=_declared_engine_service_snapshots(service_stack),
                    )
                    service_stack.start_all(wait=True)
                    self._events.phase_changed(
                        phase="services_ready",
                        services=_engine_service_snapshots(service_stack),
                    )
                    self._ensure_engine_image_local_aliases(service_stack)
                    self._perform_declared_compose_image_builds(stack_requirements)
                    # Use localhost address — the orchestrator runs on the host,
                    # not inside Docker, so Docker container names don't resolve.
                    runner_url = service_stack.get_service_url("runner", 50051)
                    # get_service_url returns "http://localhost:{port}" — strip scheme for gRPC
                    runner_address = runner_url.replace("http://", "")
                    self.logger.info("EngineStack started", runner_address=runner_address)

                # Connect TypeSense to core stack network so Runner can reach it
                if self._typesense_server is not None:
                    if task_stack_mode:
                        raise RuntimeError(
                            "TypeSense KB is enabled but the run uses a task-declared "
                            "compose stack (either --runtime per_trial or a run whose tasks "
                            "declare environment_manifest under --runtime shared). The "
                            "TypeSense bridge joins TypeSense to the shared 'runner-net', "
                            "which is the only network 'typesense:8108' resolves on — "
                            "task-declared runners live on task-side networks and never "
                            "see that alias. Either drop the TypeSense KB block or drop "
                            "the task-declared environment_manifest."
                        )
                    self._connect_typesense_to_runner_network(service_stack)
            except Exception as e:
                # Special-case ``ImagePullError`` so its kind and the
                # operator-actionable hint (e.g. "Docker Hub rate limit
                # hit. Configure authenticated pulls…") land in the log
                # verbatim, not folded into a generic stack trace. The
                # exception is re-raised so callers still see the crash;
                # this only makes sure the actionable text is prominent
                # in the operator's terminal.
                from tolokaforge.docker.image import ImagePullError

                if isinstance(e, ImagePullError):
                    retry_after = e.response_headers.get("Retry-After")
                    self.logger.error(
                        "Failed to auto-start services: pull failed",
                        kind=e.kind,
                        image=e.full_tag,
                        message=str(e),
                        retry_after=retry_after,
                    )
                else:
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
        self._events.phase_changed(phase="connecting_runtime")
        runtime_backend.connect()
        self.logger.info("Runtime backend connected")
        self._verify_isolation_compatibility(runtime_backend)

        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

        env_endpoints = _build_env_endpoints(runner_address)
        # Any task-declared manifest hands endpoint resolution to the composer:
        # the backend either materialises run-scope stacks at ``connect`` or
        # ``provision_trial`` per trial. The default ``env_endpoints`` built
        # from ``runner_address`` are phantom in that case, so logging them
        # would misdirect log-based diagnosis.
        if self._any_task_declares_environment_manifest():
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
        # ``try/finally`` from here to the run's return guarantees the
        # grader teardown (broker close + worker join, gRPC channel close)
        # fires even when the trial loop or a mid-run cleanup raises. The
        # rest of the existing sequential teardown lives inside the try
        # body — a mid-run exception still surfaces to the caller after
        # the grader is closed.
        try:
            trial_executor = self._build_trial_executor(
                runtime_backend, conductor, output_dir=output_dir
            )

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

            total_cost_usd = self._collect_existing_cost(output_dir)
            total_trials_scheduled = len(pending_trials)
            if total_cost_usd > 0:
                self.logger.info(
                    "Loaded existing run spend", total_cost_usd=round(total_cost_usd, 6)
                )
            budget = self._resolve_budget(initial_cost_usd=total_cost_usd)
            budget_exhausted = False
            last_hit: BudgetHit | None = None
            if budget is not None:
                hit = budget.poll()
                if hit is not None:
                    budget_exhausted = True
                    last_hit = hit
                    self._stopped_reason = f"{hit.which} limit"
                    write_limit_hit_marker(output_dir, hit)
                    self.logger.warning(
                        "Budget already exhausted at run start; no trials will be scheduled",
                        limit_kind=hit.which,
                        threshold=hit.threshold,
                        value_at_hit=round(hit.value_at_hit, 6),
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
                            lease.id,
                            f"Task not found in loaded set: {lease.task_id}",
                            retryable=False,
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
                        total_index=self._total_index_by_key.get(
                            (lease.task_id, lease.trial_index), 0
                        ),
                        agent_model=f"{agent_config.provider}/{agent_config.name}",
                        user_model=f"{user_config.provider}/{user_config.name}",
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
                            if budget is not None:
                                budget.record_generation_cost(trial_cost)

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
                                    if budget is not None:
                                        budget.record_trial_terminated()
                                self.logger.info(
                                    "Trial failed (transient)",
                                    task_id=task_id,
                                    trial_index=trial_idx,
                                    trial_cost_usd=trial_cost,
                                    total_cost_usd=round(total_cost_usd, 6),
                                )
                            else:
                                run_queue.mark_completed(lease.id, cost_usd=trial_cost)
                                # An ungraded trial records no verdict: a 0.0 here
                                # is indistinguishable from a task the agent failed.
                                run_state.mark_completed(
                                    task_id,
                                    trial_idx,
                                    trajectory.grade.binary_pass if trajectory.grade else None,
                                    trajectory.grade.score if trajectory.grade else None,
                                )
                                self.state_manager.save_state(run_state)

                                self._events.trial_completed(
                                    trial_id=f"{task_id}:{trial_idx}",
                                    binary_pass=(
                                        trajectory.grade.binary_pass if trajectory.grade else None
                                    ),
                                    score=trajectory.grade.score if trajectory.grade else None,
                                )
                                if budget is not None:
                                    budget.record_trial_terminated()

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
                                if budget is not None:
                                    budget.record_trial_terminated()

                        # Stop scheduling new work once any active budget cap is reached.
                        if budget is not None and not budget_exhausted:
                            hit = budget.poll()
                            if hit is not None:
                                budget_exhausted = True
                                last_hit = hit
                                self._stopped_reason = f"{hit.which} limit"
                                write_limit_hit_marker(output_dir, hit)
                                self.logger.warning(
                                    "Budget limit reached; no new trials will be scheduled",
                                    limit_kind=hit.which,
                                    threshold=hit.threshold,
                                    value_at_hit=round(hit.value_at_hit, 6),
                                    total_cost_usd=round(total_cost_usd, 6),
                                    remaining_trials=run_queue.get_counts().get("pending", 0),
                                )
                        if budget_exhausted:
                            continue

                        while len(active_futures) < self.config.effective_workers and submit_one():
                            pass

            counts = run_queue.get_counts()
            remaining = (
                counts.get("pending", 0) + counts.get("leased", 0) + counts.get("running", 0)
            )
            if budget_exhausted and remaining > 0:
                self.state_manager.mark_run_paused()
                self.logger.warning(
                    "Run paused due to budget cap",
                    pending_trials=remaining,
                    total_scheduled_trials=total_trials_scheduled - remaining,
                    limit_kind=last_hit.which if last_hit is not None else None,
                    threshold=last_hit.threshold if last_hit is not None else None,
                    total_cost_usd=round(total_cost_usd, 6),
                )

            # Cleanup runtime backend if used; SharedStackRuntimeBackend
            # logs its own "Shared-stack runtime closed" line, no dup needed.
            if runtime_backend:
                runtime_backend.close()

            # Stop TypeSense BEFORE destroying the EngineStack.
            # TypeSense is connected to runner-net (via _connect_typesense_to_runner_network),
            # so it must be removed from that network before the stack can tear it down.
            if self._typesense_server is not None:
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

            # Publish completeness and generate reports before stamping the
            # run as completed, so ``run_state.json``'s completion gates are
            # derived from the published counts.
            if not (budget_exhausted and remaining > 0):
                self._finalize_run_reports_and_status(output_dir)

            resolved_output_dir = output_dir.resolve()
            self._events.run_finished(output_dir=resolved_output_dir)
            return resolved_output_dir
        finally:
            self._close_trial_graders()

    def _close_trial_graders(self) -> None:
        """Release every ``TrialGrader`` built during this orchestrator's
        lifetime, in reverse construction order.

        The queue transport owns a broker + a worker pool that must shut
        down before the process exits, and the ``grader_rpc`` transport
        owns a gRPC channel. Every built-in Protocol implementation ships
        a ``close()`` (a noop for the transport-less ones); a downstream
        registered grader that lacks one is tolerated so an old
        registration does not fail a new run at teardown.
        """
        while self._trial_graders_to_close:
            grader = self._trial_graders_to_close.pop()
            close = getattr(grader, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 — teardown must survive
                self.logger.warning(
                    "Trial grader close() raised",
                    grader=type(grader).__name__,
                    error=str(exc),
                )

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

        user_config = require_user_simulator_config(user_config)

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

        agent_client = self._build_agent_client(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.effective_max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.effective_max_requests_per_second)

        runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")

        # Coerce plan-shape for the deprecated override BEFORE the worker's
        # ``_extract_run_env_manifest`` fires, so worker and parent agree on
        # what the plan looks like.
        self._coerce_plan_shape_for_override(self.config.orchestrator.runtime)

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

        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

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
        trial_executor = self._build_trial_executor(
            runtime_backend, conductor, output_dir=output_dir
        )

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

        # Silent on fresh queues (no completions yet) — the reattach line is
        # for operators joining an in-progress run, not for cold starts.
        queue_counts = run_queue.get_counts()
        if queue_counts.get("completed", 0) > 0:
            self.logger.info(
                f"Reattaching to run dir {run_id}: "
                f"{queue_counts['completed']}/{queue_counts['total']} completed, "
                f"{queue_counts['pending']} pending in queue.",
                run_id=run_id,
                completed=queue_counts["completed"],
                total=queue_counts["total"],
                pending=queue_counts["pending"],
            )

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
            if self._typesense_server is not None:
                try:
                    self._typesense_server.stop()
                except Exception:
                    pass
            # Broker close + worker join happen even on an exception in
            # the leased-work loop above. Sequential with the runtime
            # teardown; each failure is logged rather than raised so the
            # outer flow still surfaces the original exception.
            self._close_trial_graders()

        self._publish_grading_completeness()
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
        # configured, or if any selected pack's grading cannot be graded as
        # written — otherwise a misconfigured distributed run reports success
        # here and then every worker dies identically at grade time.
        self._resolve_judge_config()
        self._reject_ungradeable_packs()

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

        # Persist engine-level run state for subprocess workers. Worker CLIs
        # read this so the overlay set at ``prepare`` time propagates without
        # the operator threading --presets-file through every ``worker``
        # invocation.
        write_engine_run_state(
            output_dir,
            run_id=run_id,
            presets_file=get_overlay_path(),
            models_fingerprint=compute_models_fingerprint(),
            adapter_fingerprints=self._adapter_fingerprints(),
        )

        summary = {
            "queued_attempts": queued_attempts,
            "queue_counts": counts,
            "queue_backend": self.config.effective_queue_backend,
        }
        self.logger.info("Run prepared", **summary)
        return summary

    def _warn_on_degraded_coverage(self, all_task_metrics: list[dict[str, Any]]) -> None:
        """Announce every task whose sample was reduced by an infrastructure abort.

        An aborted trial leaves the rate denominators — which is correct — but it
        also shrinks the sample ``pass@k`` is estimated from, and a ``pass@5``
        that silently became ``null`` because one trial died is
        indistinguishable in JSON from a task that never ran five trials.
        """
        for task_metrics in all_task_metrics:
            aborts = task_metrics["infrastructure_aborts"]
            if not sum(aborts.values()):
                continue
            measured = task_metrics["measured_trials"]
            lost_k = [
                k
                for k in (1, 5, 10)
                if task_metrics.get(f"pass@{k}") is None and k <= task_metrics["total_trials"]
            ]
            self.logger.warning(
                "Task coverage degraded by infrastructure aborts",
                task_id=task_metrics.get("task_id"),
                total_trials=task_metrics["total_trials"],
                measured_trials=measured,
                infrastructure_aborts={reason: count for reason, count in aborts.items() if count},
                pass_at_k_without_coverage=lost_k,
            )

    def _finalize_run_reports_and_status(self, output_dir: Path) -> None:
        """Publish completeness, generate reports, and stamp completion status.

        ``zc`` / ``zjg`` initialize to False before the try so a
        ``BaseException`` raised inside (``KeyboardInterrupt``, ``SystemExit``,
        anything ``except Exception`` would miss) still stamps a well-defined
        state via ``finally``. The invariant is
        ``status == "completed"`` on a non-paused run regardless of exception
        class — resume-detection reads ``status`` alone. See ADR-0041.
        """
        zc = False
        zjg = False
        try:
            self._generate_reports(output_dir)
            self._publish_grading_completeness()
            zc = self.grading_completeness.zero_coverage
            zjg = self.grading_completeness.zero_judge_graded
        finally:
            self.state_manager.mark_run_completed(zero_coverage=zc, zero_judge_graded=zjg)

    def _publish_grading_completeness(self) -> None:
        """Stamp :attr:`grading_completeness` from the attempts this process ran.

        ``measured_trials`` counts trials that reached the agent measurement
        point; ``scored_trials`` follows the single derivation
        :func:`tolokaforge.core.metrics._measured_averages` uses
        (``t.grade is not None``); ``judge_errored_trials`` counts scored
        trials whose judge component errored. The two completion-gate
        booleans are derived properties on ``GradingCompleteness``.
        """
        from tolokaforge.core.models.grade import JudgeStatus

        self.grading_completeness = GradingCompleteness(
            total_attempts=len(self.results),
            ungradeable_trial_ids=tuple(
                f"{trajectory.task_id}:{trajectory.trial_index}"
                for trajectory in self.results
                if classify_trial_outcome(trajectory) is TrialOutcomeClass.UNGRADEABLE
            ),
            measured_trials=sum(
                1
                for trajectory in self.results
                if classify_trial_outcome(trajectory) is TrialOutcomeClass.MEASURED
            ),
            scored_trials=sum(1 for trajectory in self.results if trajectory.grade is not None),
            judge_errored_trials=sum(
                1
                for trajectory in self.results
                if trajectory.grade is not None
                and trajectory.grade.judge_status is JudgeStatus.ERRORED
            ),
        )

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

        self._warn_on_degraded_coverage(all_task_metrics)

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

        aggregate["schema_version"] = AGGREGATE_SCHEMA_VERSION
        aggregate["captured_service_logs"] = collect_service_log_captures(output_dir).model_dump(
            by_alias=True, mode="json"
        )

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
