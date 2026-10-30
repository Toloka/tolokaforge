"""Bring up the engine service stack a run needs, independent of the orchestrator.

A stackless task — one that declares no ``environment_manifest`` — resolves to
the shared runtime and dials a runner at ``EXECUTOR_ADDRESS``. A single-box
``tolokaforge run`` starts that runner (plus db-service, plus a search plane
when configured) before any trial provisions. :func:`build_engine_stack` and
:func:`start_engine_stack` factor that bring-up into free functions the
orchestrator and any library caller share, so no caller has to re-derive the
service set a run needs.

:func:`run_trial_with_stack` is the library route a fresh-sandbox caller uses:
it starts the stack for one trial, points ``EXECUTOR_ADDRESS`` at the started
runner, runs the pure node entry
:func:`~tolokaforge.core.trial_node.run_trial_from_inputs`, and tears the stack
down. The node entry stays pure — it dials whatever ``EXECUTOR_ADDRESS`` names
and owns no service lifecycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never

from tolokaforge.adapters._task_loader import (
    ToolActor,
    declared_search_backend,
    declared_tool_names,
    effective_mcp_server,
    enabled_tool_names,
    search_declaration,
    tool_configs,
    uses_search,
)
from tolokaforge.core.compose_materialisation import run_needs_docker_cli
from tolokaforge.core.env_var import parse_env_positive_float
from tolokaforge.core.hosts import HTTP_REQUEST_DEFAULT_HOSTS, MOCK_WEB_URL, host_listed
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import ComputeConfig
from tolokaforge.core.plugin_registry import RAG_SERVICE_STACK_SERVICE
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    RunDisplayEvents,
    ServiceSnapshot,
)
from tolokaforge.docker.health import HealthProbe, HealthProbeError

if TYPE_CHECKING:
    from tolokaforge.adapters import BaseAdapter
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import RunConfig, TaskConfig
    from tolokaforge.core.models.docker_config import EngineServiceMode, EngineServicesConfig
    from tolokaforge.core.trial import TrialNodeInputs, TrialResult
    from tolokaforge.docker.stack import EngineStack

# Tools that need Playwright + Chromium baked into the runner image. The task
# list is scanned before the docker stack starts and the ``INSTALL_PLAYWRIGHT``
# build arg is enabled when any task uses one of them. ``MobileTool`` subclasses
# ``BrowserTool`` so it has the same Playwright dependency — keeping the list
# inline (vs. inferring from the class hierarchy) avoids importing the tool
# modules at import time.
_PLAYWRIGHT_TOOL_NAMES: frozenset[str] = frozenset({"browser", "mobile"})

# The optional engine services, resolved one by one on top of the runner and
# db-service (:func:`resolve_engine_services`). ``browser`` and ``mobile`` reach
# mock-web for app/site URLs, and so does ``http_request`` whenever its hosts
# admit mock-web, which its default hosts do; a task may also declare
# ``initial_state.mock_web`` without enabling those tools. Knowledge-base search
# needs rag-service when the task's search backend says so: not by the tool's
# name, which a task may choose, but by the ``stack_service`` its backend
# (``initial_state.rag.backend``, default ``rag_service``) declares, for a task
# that searches at all, i.e. declares a corpus or enables its search tool
# (``initial_state.rag.tool.name``, default ``search_kb``). An adapter whose search
# signal no task shows declares ``needs_rag_service``.
#
# Signals, over either actor's block:
# +------------------------------------------------------+--------------+
# | Signal                                               | Service      |
# +------------------------------------------------------+--------------+
# | tools.<actor>.enabled ∋ browser                      | mock-web     |
# | tools.<actor>.enabled ∋ mobile                       | mock-web     |
# | tools.<actor>.enabled ∋ the builtin http_request,    | mock-web     |
# |   and tools.<actor>.http_request.allowed_hosts       |              |
# |   (default: HTTP_REQUEST_DEFAULT_HOSTS) admits       |              |
# |   mock-web                                           |              |
# | initial_state.mock_web is truthy                     | mock-web     |
# | the task searches, and its backend's stack_service   | rag-service  |
# |   is rag_service (the default backend's)             |              |
# | the adapter declares needs_rag_service               | rag-service  |
# +------------------------------------------------------+--------------+
#
# ``docker.services.<service>`` starts one ``always`` or forbids it (``never``).
# A run that starts neither gets ``core_stack``; one that starts either gets
# ``full_stack`` with only the services it starts.
_MOCK_WEB_TOOL_NAMES: frozenset[str] = frozenset({"browser", "mobile"})


def _tasks_need_playwright(tasks: list[Any]) -> bool:
    """Return True iff any task enables a Playwright-dependent tool.

    Decides whether to pass ``enable_playwright=True`` to :func:`core_stack`.
    Either actor's declaration counts: the runner reconstructs a user tool
    through the same wrapper as an agent tool, so a user-side ``browser`` needs
    the same image. Pure function so unit tests can construct ``TaskConfig``
    instances directly without standing up the docker stack.
    """
    return any(_PLAYWRIGHT_TOOL_NAMES & declared_tool_names(task) for task in tasks)


def _run_uses_coding_harness(config: RunConfig) -> bool:
    """Return True iff the run config selects a coding-harness CLI.

    A native coding-harness pack drives its bash tool through
    ``docker compose exec`` (the mixin's harness tool schema uses the
    ``docker_compose_exec`` invocation style). The runner container therefore
    needs the docker CLI even though ``NativeAdapter.requires_docker_cli_in_
    runner`` is ``False`` at class level.
    """
    agent_model = config.models.get("agent") if config.models else None
    return agent_model is not None and agent_model.harness is not None


def _search_needs_rag_service(task: Any) -> bool:
    """Whether the task searches with a backend that declares the rag-service stack service.

    A typed ``rag: {}`` block declares no corpus, so it needs rag-service only if
    an actor enables its search tool.
    """
    search = search_declaration(task)
    if not uses_search(task, search):
        return False
    return declared_search_backend(search).stack_service == RAG_SERVICE_STACK_SERVICE


def _http_request_reaches_mock_web(task: Any) -> bool:
    """Whether an actor's ``http_request`` may request mock-web.

    The tool reads its hosts as the task lists them, or its default hosts when the
    task lists none, and those admit mock-web. A task that points ``http_request``
    at other hosts only, an app world's for one, does not need mock-web. An
    ``http_request`` an ``mcp_server`` serves is that server's tool, which the
    builtin's default hosts say nothing about.
    """
    for actor in ToolActor:
        if "http_request" not in enabled_tool_names(task, actor):
            continue
        if effective_mcp_server(task, actor) is not None:
            continue
        kwargs = tool_configs(task, actor).get("http_request", {})
        allowed = kwargs.get("allowed_hosts") or HTTP_REQUEST_DEFAULT_HOSTS
        if host_listed(MOCK_WEB_URL, allowed):
            return True
    return False


def _task_needs_mock_web(task: Any) -> bool:
    """Whether the task reaches mock-web: see the signal table above.

    Detection works on both ``InitialStateConfig`` Pydantic models and plain
    dicts (raw YAML), to keep the unit tests simple.
    """
    if _MOCK_WEB_TOOL_NAMES & declared_tool_names(task):
        return True
    if _http_request_reaches_mock_web(task):
        return True
    initial_state = task.initial_state
    if initial_state is None:
        return False
    mock_web = (
        initial_state.mock_web
        if hasattr(initial_state, "mock_web")
        else (initial_state.get("mock_web") if isinstance(initial_state, dict) else None)
    )
    return bool(mock_web)


class EngineServiceConflictError(ValueError):
    """The run config forbids (``never``) an engine service the run needs."""


@dataclass(frozen=True)
class EngineServiceChoice:
    """Whether a run starts one optional engine service, and why."""

    start: bool
    reason: str


@dataclass(frozen=True)
class EngineServicePlan:
    """The optional engine services a run starts on top of the runner and db-service."""

    rag_service: EngineServiceChoice
    mock_web: EngineServiceChoice

    @property
    def starts_any(self) -> bool:
        """Whether the run needs more than the core stack."""
        return self.rag_service.start or self.mock_web.start


_BESIDE_TASK_DECLARED_STACKS = EngineServiceChoice(
    start=False, reason="the tasks declare their own stacks, so no built-in service starts"
)
_TASK_DECLARED_STACKS_PLAN = EngineServicePlan(
    rag_service=_BESIDE_TASK_DECLARED_STACKS, mock_web=_BESIDE_TASK_DECLARED_STACKS
)
"""The plan of a run whose tasks bring their own stacks: only the runner and
db-service images are built, for the ``:local`` aliases their compose files use."""

_NEEDERS_NAMED = 3
"""How many tasks a reason names before it counts the rest."""


def _named(needers: list[str]) -> str:
    shown = ", ".join(needers[:_NEEDERS_NAMED])
    rest = len(needers) - _NEEDERS_NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


def _choose(field: str, mode: EngineServiceMode, needers: list[str]) -> EngineServiceChoice:
    """Apply ``docker.services.<field>`` to what the run needs."""
    if mode == "never":
        if needers:
            raise EngineServiceConflictError(
                f"docker.services.{field} is 'never', but the run needs it: {_named(needers)}. "
                "Remove the override, or run without what needs the service."
            )
        return EngineServiceChoice(start=False, reason=f"docker.services.{field} is 'never'")
    if mode == "always":
        return EngineServiceChoice(start=True, reason=f"docker.services.{field} is 'always'")
    if mode == "auto":
        if needers:
            return EngineServiceChoice(start=True, reason=f"needed by {_named(needers)}")
        return EngineServiceChoice(start=False, reason="no task or adapter needs it")
    assert_never(mode)


def resolve_engine_services(
    tasks: list[Any], stack_requirements: Any, services: EngineServicesConfig
) -> EngineServicePlan:
    """Decide which optional engine services the run starts, one by one.

    rag-service: a task searches with a backend that declares the rag-service
    stack service, or the adapter declares ``needs_rag_service``. Adapters whose
    search signal is not visible in task tool names or ``initial_state`` (e.g. a
    domain-shipped ``docindex/`` knowledge base surfaced as
    ``TaskDescription.search.enabled``) declare it on
    :meth:`BaseAdapter.docker_stack_requirements`, since the Runner hard-fails
    ``RegisterTrial`` for a search-enabled task when no RAG client is configured.
    mock-web: a task reaches it (:func:`_task_needs_mock_web`).

    ``services`` (the run's ``docker.services``) then starts either ``always`` or
    forbids it; forbidding one the run needs raises
    :class:`EngineServiceConflictError`, before any engine container work. Duck-typed on
    ``stack_requirements`` so unit tests can pass lightweight stand-ins.
    """
    adapter_needs_rag = stack_requirements is not None and getattr(
        stack_requirements, "needs_rag_service", False
    )
    rag_needers = ["the adapter (needs_rag_service)"] if adapter_needs_rag else []
    rag_needers += [f"task {task.task_id}" for task in tasks if _search_needs_rag_service(task)]
    mock_web_needers = [f"task {task.task_id}" for task in tasks if _task_needs_mock_web(task)]
    return EngineServicePlan(
        rag_service=_choose("rag_service", services.rag_service, rag_needers),
        mock_web=_choose("mock_web", services.mock_web, mock_web_needers),
    )


def _declared_engine_service_snapshots(service_stack: Any) -> list[ServiceSnapshot]:
    """Return one ``ServiceSnapshot`` per declared engine service.

    Fired on ``phase_changed(phase="starting_services", …)`` so the panel can
    render the service list with ``status="created"`` before any container has
    been brought up. Reads ``service_stack.services`` — the declared
    :class:`ServiceDefinition` mapping — so it works before ``start_all()``.
    """
    snapshots: list[ServiceSnapshot] = []
    for name in service_stack.services:
        snapshots.append(ServiceSnapshot(name=name, status="created", ports={}, role="engine"))
    return snapshots


def _engine_service_snapshots(service_stack: Any) -> list[ServiceSnapshot]:
    """Return one ``ServiceSnapshot`` per running engine service.

    Fired on ``phase_changed(phase="services_ready", …)`` after ``start_all()``
    completes so the panel can render live status + resolved ports. Reads
    ``service_stack.get_status()`` for the lifecycle string and ``.health`` for
    the probe verdict; the widget prefers ``health`` when present because
    ``status="running"`` on a container whose probe is still starting overstates
    readiness.
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


@dataclass(frozen=True)
class StartedEngineStack:
    """A started engine stack and the host address its runner answers on."""

    service_stack: EngineStack
    runner_address: str
    stack_requirements: Any


def build_engine_stack(
    config: RunConfig,
    adapter: BaseAdapter | None,
    tasks: list[TaskConfig],
    *,
    typesense_stack_kwargs: dict[str, Any] | None = None,
    grpc_max_message_mb: int,
    state_read_timeout_s: float,
    logger: StructuredLogger,
    optional_services: bool = True,
) -> tuple[EngineStack, Any]:
    """Build (but do not start) the engine stack a run's tasks need.

    Resolves the adapter's declarative stack requirements and the optional
    engine services the run starts (:func:`resolve_engine_services`, which
    refuses a ``never`` service the run needs before any engine container work), enables
    Playwright / the docker CLI / the search plane as the tasks demand, picks
    ``core_stack`` (db-service + runner) or ``full_stack`` with only the services
    it starts, and constructs the :class:`EngineStack`. Returns the unstarted
    stack and the adapter's ``DockerStackRequirements`` (``None`` when no adapter
    is given).

    ``typesense_stack_kwargs`` carries the run's in-network search address when
    one is configured; the caller resolves it (and registers its API key) so the
    search-server lifecycle stays with the caller. ``optional_services=False``
    skips the resolution and builds the core stack: for a run whose tasks declare
    their own stacks, where no built-in container starts, so a service
    ``docker.services`` says ``always`` is refused.
    """
    from tolokaforge.core.models.docker_config import DockerConfig
    from tolokaforge.docker.stacks import core_stack, full_stack

    logger.info("Auto-starting Docker services via EngineStack")
    stack_requirements = adapter.docker_stack_requirements() if adapter is not None else None
    docker_config = config.docker or DockerConfig()
    if optional_services:
        engine_services = resolve_engine_services(tasks, stack_requirements, docker_config.services)
    else:
        engine_services = _TASK_DECLARED_STACKS_PLAN
        forced = [
            field
            for field in ("rag_service", "mock_web")
            if getattr(docker_config.services, field) == "always"
        ]
        if forced:
            raise EngineServiceConflictError(
                f"docker.services.{forced[0]} is 'always', but the tasks declare their own "
                "stacks, where no built-in service starts. Remove the override."
            )
    logger.info(
        "Engine services resolved",
        rag_service=engine_services.rag_service.start,
        rag_service_reason=engine_services.rag_service.reason,
        mock_web=engine_services.mock_web.start,
        mock_web_reason=engine_services.mock_web.reason,
    )
    core_stack_kwargs = stack_requirements.to_core_stack_kwargs() if stack_requirements else {}
    # Ensure host-side paths for any extra bind mounts exist so Docker doesn't
    # create them as root-owned at start-up.
    for host_path, _ in core_stack_kwargs.get("extra_runner_binds", []):
        Path(host_path).mkdir(parents=True, exist_ok=True)
    # Both browser and mobile (a BrowserTool subclass) need Playwright/Chromium
    # baked in; skipping the install for the common case keeps image builds fast.
    if _tasks_need_playwright(tasks):
        logger.info("Playwright-dependent tool detected in tasks — enabling Playwright")
        core_stack_kwargs["enable_playwright"] = True
    # The runner container is created knowing where the search plane is, so
    # nothing has to rewrite a task's address after the fact.
    core_stack_kwargs.update(typesense_stack_kwargs or {})
    adapter_type = (
        config.evaluation.harness_adapter.type if config.evaluation.harness_adapter else None
    )
    if run_needs_docker_cli(adapter_type, tasks) or _run_uses_coding_harness(config):
        logger.info(
            "Docker CLI required in runner image "
            "(terminal-bench adapter, compose-variant tools, "
            "or coding-harness mode detected)"
        )
        core_stack_kwargs["enable_docker_cli"] = True
        # Fail loud before any container work: the docker-CLI install is a
        # Dockerfile build arg, so it only takes effect on a locally-built
        # runner. A pulled runner would die on the first tool call with
        # `[Errno 2] No such file or directory: 'docker'`.
        import tolokaforge as _engine_pkg
        from tolokaforge.docker.builder import repo_root
        from tolokaforge.docker.image_source_policy import check_runner_docker_cli_available

        check_runner_docker_cli_available(
            needs_docker_cli=True,
            request=docker_config.image_source,
            is_wheel_install=not (repo_root() / "pyproject.toml").is_file(),
            engine_version=_engine_pkg.__version__,
        )
    # ``full_stack`` accepts every kwarg ``core_stack`` does, so the
    # playwright/binds plumbing above still applies; its two service flags add
    # only the optional services the run starts.
    if engine_services.starts_any:
        stack_factory = full_stack
        service_flags = {
            "rag_service": engine_services.rag_service.start,
            "mock_web": engine_services.mock_web.start,
        }
    else:
        stack_factory = core_stack
        service_flags = {}
    logger.info(
        "Creating service stack",
        services=["db-service", "runner"]
        + [
            name
            for name, choice in (
                ("rag-service", engine_services.rag_service),
                ("mock-web", engine_services.mock_web),
            )
            if choice.start
        ],
    )
    # Route ``grader.expose_substrate`` into the runner container so it
    # registers the SubstrateService gRPC servicer alongside RunnerService (same
    # listen port). Absent block == absent field == off.
    if config.grader is not None and config.grader.expose_substrate:
        core_stack_kwargs["expose_substrate"] = True
    # The runner container serves with the bound the engine's client channels
    # use (``compute.grpc_max_message_mb``).
    core_stack_kwargs["grpc_max_message_mb"] = grpc_max_message_mb
    core_stack_kwargs["state_read_timeout_s"] = state_read_timeout_s
    service_stack = stack_factory(config=docker_config, **core_stack_kwargs, **service_flags)
    return service_stack, stack_requirements


def start_engine_stack(
    config: RunConfig,
    adapter: BaseAdapter | None,
    tasks: list[TaskConfig],
    *,
    typesense_stack_kwargs: dict[str, Any] | None = None,
    grpc_max_message_mb: int,
    state_read_timeout_s: float,
    logger: StructuredLogger,
    events: RunDisplayEvents = _NULL_EVENTS,
    runtime_connect_budget: tuple[float, float] | None = None,
) -> StartedEngineStack:
    """Build and start the engine stack, returning the runner's host address.

    Builds the stack via :func:`build_engine_stack`, starts every service, and
    resolves the host ``host:port`` the runner's gRPC server answers on. When
    ``runtime_connect_budget`` is given, the runner's published host port is
    probed before returning so a caller that dials it never races the
    port-publish. The built-in engine containers are started here; task-declared
    compose stacks (image aliasing, per-trial compose) are a separate concern.
    """
    service_stack, stack_requirements = build_engine_stack(
        config,
        adapter,
        tasks,
        typesense_stack_kwargs=typesense_stack_kwargs,
        grpc_max_message_mb=grpc_max_message_mb,
        state_read_timeout_s=state_read_timeout_s,
        logger=logger,
    )
    logger.info(
        "Building Docker images and starting containers "
        "(this may take a few minutes on first run)..."
    )
    events.phase_changed(
        phase="starting_services",
        detail="docker compose up",
        services=_declared_engine_service_snapshots(service_stack),
    )
    service_stack.start_all(wait=True)
    events.phase_changed(
        phase="services_ready",
        services=_engine_service_snapshots(service_stack),
    )
    # Use the localhost address — the caller runs on the host, not inside
    # Docker, so Docker container names don't resolve. get_service_url returns
    # "http://localhost:{port}"; strip the scheme for gRPC.
    runner_url = service_stack.get_service_url("runner", 50051)
    runner_address = runner_url.replace("http://", "")
    logger.info("EngineStack started", runner_address=runner_address)
    if runtime_connect_budget is not None:
        prewarm_runner_host_endpoint(runner_address, budget=runtime_connect_budget, logger=logger)
    return StartedEngineStack(
        service_stack=service_stack,
        runner_address=runner_address,
        stack_requirements=stack_requirements,
    )


def teardown_engine_stack(service_stack: Any, *, logger: StructuredLogger) -> None:
    """Best-effort teardown of a started engine stack.

    Destroys the stack, releasing the fixed-name runner / db-service containers
    and ``runner-net``. Never raises, so it is safe to call from a ``finally``;
    a ``service_stack`` of ``None`` is a no-op.
    """
    if service_stack is None:
        return
    try:
        service_stack.destroy()
        logger.info("EngineStack destroyed")
    except Exception as e:
        logger.warning("Failed to destroy EngineStack", error=str(e))


def resolve_runtime_connect_budget(
    config: RunConfig, *, logger: StructuredLogger
) -> tuple[float, float]:
    """Resolve the runner health-check budget (env → YAML → default).

    Same precedence at every use site. Env-var values that fail to parse fall
    back to the YAML block with a warning.
    """
    runtime_connect = config.orchestrator.runtime_connect
    timeout_s = parse_env_positive_float(
        "TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S",
        default=runtime_connect.timeout_s,
        logger=logger,
    )
    retry_interval_s = parse_env_positive_float(
        "TOLOKAFORGE_RUNNER_CONNECT_RETRY_INTERVAL_S",
        default=runtime_connect.retry_interval_s,
        logger=logger,
    )
    assert timeout_s is not None and retry_interval_s is not None
    return timeout_s, retry_interval_s


def prewarm_runner_host_endpoint(
    runner_address: str, *, budget: tuple[float, float], logger: StructuredLogger
) -> None:
    """Probe the runner's PUBLISHED host port before a caller dials it.

    Compose's internal ``HEALTHCHECK`` says the gRPC server bound its port
    inside the container, but the published host port has propagation lag
    (particularly under Docker Desktop). A TCP-level readiness gate here means
    the caller's ``connect()`` never races the port-publish — it dials a socket
    already listening from the host. TCP (not gRPC) because the runner serves
    its health check on ``RunnerService.HealthCheck``, not the standard
    ``grpc.health.v1.Health/Check``; port reachability is what closes the
    compose-vs-host-publish gap, and gRPC-level health is covered by the
    client-side connect retry.

    A refusal raises an actionable :class:`RuntimeError` naming the resolved
    host:port and the knob to raise.
    """
    timeout_s, retry_interval_s = budget
    host, port_str = runner_address.rsplit(":", 1)
    port = int(port_str)
    try:
        HealthProbe.tcp(
            host=host,
            port=port,
            timeout_s=timeout_s,
            interval_s=retry_interval_s,
        ).wait()
    except HealthProbeError as exc:
        raise RuntimeError(
            f"Runner host-side readiness probe failed at {host}:{port} after "
            f"{timeout_s}s. Raise orchestrator.runtime_connect.timeout_s in the "
            f"run config, or set TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S, if the "
            f"container needs longer to cold-boot. Detail: {exc}"
        ) from exc
    logger.info(
        "Runner host-side readiness confirmed",
        host=host,
        port=port,
        budget_s=timeout_s,
    )


def run_trial_with_stack(
    inputs: TrialNodeInputs,
    run_config: RunConfig,
    *,
    adapter: BaseAdapter | None = None,
    logger: StructuredLogger | None = None,
) -> TrialResult:
    """Bring up the engine stack for one trial, run it, and tear the stack down.

    The library route for a fresh-sandbox / sandbox-per-trial caller: it starts
    the runner (+ db-service, + the search plane when the task needs one) via
    :func:`start_engine_stack`, points ``EXECUTOR_ADDRESS`` at the started
    runner, runs the pure node entry
    :func:`~tolokaforge.core.trial_node.run_trial_from_inputs`, and destroys the
    stack in a ``finally``. The caller no longer has to copy the orchestrator's
    choice of services for a stackless task.

    Scoped to a stackless task — one that declares no ``environment_manifest``
    and resolves to the shared runtime. A task that declares its own compose
    stack materialises its substrate per trial and does not route here.

    ``EXECUTOR_ADDRESS`` is restored to its prior value (or unset) on return,
    so the process environment is left as it was found.
    """
    from tolokaforge.core.trial_node import build_single_task_adapter, run_trial_from_inputs

    log = logger or get_logger(inputs.trial_spec.run_id)
    task = inputs.task
    resolved_adapter = adapter or build_single_task_adapter(task, inputs.task_pack_dir)
    compute = run_config.compute or ComputeConfig()
    started = start_engine_stack(
        run_config,
        resolved_adapter,
        [task],
        grpc_max_message_mb=inputs.grpc_max_message_mb,
        state_read_timeout_s=compute.state_read_timeout_s,
        logger=log,
        runtime_connect_budget=resolve_runtime_connect_budget(run_config, logger=log),
    )
    previous = os.environ.get("EXECUTOR_ADDRESS")
    os.environ["EXECUTOR_ADDRESS"] = started.runner_address
    try:
        return run_trial_from_inputs(inputs)
    finally:
        if previous is None:
            os.environ.pop("EXECUTOR_ADDRESS", None)
        else:
            os.environ["EXECUTOR_ADDRESS"] = previous
        teardown_engine_stack(started.service_stack, logger=log)
