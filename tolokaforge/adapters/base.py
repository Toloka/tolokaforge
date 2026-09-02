"""Base adapter class for harness integration"""

import glob as glob_module
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.config_validation import (
    CombineLayer,
    HashSourceLayer,
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Grade, GradingConfig, TaskConfig, Trajectory

if TYPE_CHECKING:
    from tolokaforge.core.agent_driver import StagedTask
    from tolokaforge.tools.registry import Tool

logger = get_logger(__name__)


@dataclass(frozen=True)
class ComposeImageBuild:
    """One task-declared compose image the orchestrator builds before any trial provisions.

    Adapters that ship compose stacks whose services carry local ``build:``
    contexts declare the (compose file, service) pair here so the
    orchestrator can build the image once per run — outside every trial's
    provision path — instead of every trial paying the build cost and
    surfacing a ``PROVISION_ERROR`` on a broken Dockerfile.

    Attributes:
        compose_file: Absolute path to the compose file that defines
            ``service``. Passed as ``docker compose -f <compose_file>``.
        service: Compose service name to build.
    """

    compose_file: Path
    service: str


@dataclass
class DockerStackRequirements:
    """Adapter's declarative needs for the runtime Docker stack.

    The orchestrator composes these into ``core_stack(**kwargs)`` so adapters
    can opt into non-default mounts, sidecars, or socket access without the
    orchestrator hard-coding adapter types.

    Attributes:
        task_pack_mounts: Host directories to bind-mount into the Runner at
            their absolute path. Used when the Runner spawns sibling
            containers that must resolve task files at the same path on the
            host Docker daemon.
        extra_runner_binds: Additional ``(host_path, container_path)`` bind
            mounts for the Runner — typically a shared log directory.
        mount_docker_socket: Bind-mount ``/var/run/docker.sock`` into the
            Runner so it can drive the host Docker daemon directly.
        enable_dind: Add a Docker-in-Docker sidecar so the Runner can manage
            Docker Compose stacks without touching the host daemon.
        needs_rag_service: The adapter emits search-enabled TaskDescriptions
            (``TaskDescription.search.enabled``), so the run must use the
            stack that actually provisions ``rag-service`` (``full_stack``).
            The Runner hard-fails ``RegisterTrial`` for search-enabled tasks
            when no RAG client is configured, and ``core_stack`` deliberately
            omits ``RAG_SERVICE_URL`` (issue #95: "env present" ==
            "rag-service running") - so adapters whose search signal is not
            visible in task tool names or ``initial_state`` (e.g. a
            domain-shipped ``docindex/`` knowledge base) must declare the
            need here for the orchestrator's stack selection.
        image_builds: Task-declared compose images the orchestrator builds
            once per run, immediately after the engine ``:local`` aliases are
            in place. Adapters emit these when a task's compose stack has a
            service with a local ``build:`` context whose image would
            otherwise be built lazily at first-trial provision and surface a
            ``PROVISION_ERROR`` (naming compose, not the Dockerfile) on
            failure. Each entry becomes ``docker compose -f <compose_file>
            build <service>``, skipped when the service's pinned image
            already resolves locally.
    """

    task_pack_mounts: list[Path] = field(default_factory=list)
    extra_runner_binds: list[tuple[Path, str]] = field(default_factory=list)
    mount_docker_socket: bool = False
    enable_dind: bool = False
    needs_rag_service: bool = False
    image_builds: list[ComposeImageBuild] = field(default_factory=list)

    def to_core_stack_kwargs(self) -> dict[str, Any]:
        """Render to ``core_stack()`` kwargs, omitting empty defaults.

        An empty requirements object yields ``{}`` so default callers stay
        unchanged.

        ``needs_rag_service`` and ``image_builds`` are deliberately NOT
        rendered: the first selects the stack factory (``core_stack`` vs
        ``full_stack``), the second is the orchestrator's declarative
        pre-build seam. Neither stack factory accepts them.
        """
        kwargs: dict[str, Any] = {}
        if self.task_pack_mounts:
            kwargs["task_pack_mounts"] = list(self.task_pack_mounts)
        if self.extra_runner_binds:
            kwargs["extra_runner_binds"] = list(self.extra_runner_binds)
        if self.mount_docker_socket:
            kwargs["mount_docker_socket"] = True
        if self.enable_dind:
            kwargs["enable_dind"] = True
        return kwargs


@dataclass
class NativeTaskBundle:
    """Result of converting an external task to native TolokaForge format.

    Each field maps to a file written by ``bundle_writer.write_bundle()``:

    * ``task_config``   → ``task.yaml``
    * ``grading_config`` → ``grading.yaml``
    * ``initial_state`` → ``initial_state.json``
    * ``system_prompt`` → ``system_prompt.md``
    * ``fixtures``      → ``fixtures/`` directory (tools.json, golden_actions.json, …)
    * ``metadata``      → ``fixtures/metadata.json``

    Plain dicts are used for *task_config* and *grading_config* (not Pydantic
    models) to avoid serialisation complexity.  The dicts **must** be valid
    YAML-serialisable structures that match the ``TaskConfig`` /
    ``GradingConfig`` schema.
    """

    task_config: dict[str, Any]
    grading_config: dict[str, Any]
    initial_state: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    fixtures: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class AdapterEnvironment:
    """Runtime environment state holder created by adapter"""

    def __init__(
        self,
        data: dict[str, Any],
        tools: list[type],
        wiki: str,
        rules: list[str],
        task_dir: Path | None = None,
    ):
        """
        Initialize environment.

        Args:
            data: Initial data state (e.g., database tables)
            tools: List of tool classes
            wiki: System prompt / wiki content
            rules: List of policy rules
            task_dir: Path to task directory
        """
        self.data = data
        self.tools = tools
        self.wiki = wiki
        self.rules = rules
        self.task_dir = task_dir


class BaseAdapter(ABC):
    """
    Abstract base class for benchmark adapters.

    Adapters provide a unified interface for loading tasks and environments
    from different sources (native YAML files, Tau-bench, SWE-bench, etc.).
    """

    def __init__(self, params: dict[str, Any]):
        """
        Initialize adapter with configuration parameters.

        Args:
            params: Adapter-specific configuration. Common params:
                - tasks_glob: Path pattern or directory for tasks
                - base_dir: Base directory for resolving paths
        """
        self.params = params
        self.base_dir = Path(params.get("base_dir", ".")).resolve()
        self.task_packs = self._normalize_task_packs(params.get("task_packs", []))

    def _normalize_task_packs(self, value: Any) -> list[Path]:
        """Normalize configured task pack roots to absolute Paths.

        Supports list[str] and comma-separated str.
        """
        if isinstance(value, str):
            entries = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, list):
            entries = [str(part).strip() for part in value if str(part).strip()]
        else:
            entries = []

        roots: list[Path] = []
        for entry in entries:
            path = Path(entry).expanduser()
            if not path.is_absolute():
                path = (self.base_dir / path).resolve()
            roots.append(path)
        return roots

    def _resolve_glob_patterns(self, pattern: str) -> list[str]:
        """Resolve a glob against task packs when configured.

        Rules:
        - When task packs are configured, pattern must be relative and is expanded
          under each task pack root in configured order.
        - When task packs are not configured, absolute patterns are used as-is.
        - If no task packs are configured, pattern is resolved from base_dir.
        """
        path_pattern = Path(pattern).expanduser()
        if self.task_packs and path_pattern.is_absolute():
            raise ValueError(
                "tasks_glob must be relative when evaluation.task_packs is set. "
                "Add the absolute root to task_packs and keep tasks_glob relative."
            )

        if path_pattern.is_absolute():
            return [str(path_pattern)]

        roots = self.task_packs or [self.base_dir]
        return [str((root / pattern).resolve()) for root in roots]

    @staticmethod
    def _expand_braces(pattern: str) -> list[str]:
        """Expand bash-style brace patterns like ``{a,b,c}`` into multiple strings.

        Python's :mod:`glob` module does not support brace expansion, so we
        pre-expand them here.  Supports multiple brace groups and nested-free
        patterns (e.g. ``tasks/{a,b}/{x,y}/task.yaml``).

        Returns the original pattern unchanged when no braces are present.
        """
        # Find all top-level {alt1,alt2,...} groups
        brace_re = re.compile(r"\{([^{}]+)\}")
        groups: list[list[str]] = []
        parts: list[str] = []
        pos = 0
        for m in brace_re.finditer(pattern):
            parts.append(pattern[pos : m.start()])
            groups.append([alt.strip() for alt in m.group(1).split(",")])
            pos = m.end()
        if not groups:
            return [pattern]
        parts.append(pattern[pos:])

        # Cartesian product of all brace groups
        expanded: list[str] = []
        for combo in product(*groups):
            result: list[str] = []
            for i, part in enumerate(parts):
                result.append(part)
                if i < len(combo):
                    result.append(combo[i])
            expanded.append("".join(result))
        return expanded

    def _iter_glob_matches(self, pattern: str, recursive: bool = True) -> list[Path]:
        """Return de-duplicated glob matches for a pattern across configured roots.

        Supports bash-style brace expansion (e.g. ``{a,b,c}``) via
        :meth:`_expand_braces`.
        """
        matches: list[Path] = []
        seen: set[Path] = set()
        for resolved_pattern in self._resolve_glob_patterns(pattern):
            for expanded in self._expand_braces(resolved_pattern):
                for match in glob_module.glob(expanded, recursive=recursive):
                    path = Path(match).resolve()
                    if path in seen:
                        continue
                    seen.add(path)
                    matches.append(path)
        return matches

    def _resolve_path_from_roots(self, value: str | Path, must_exist: bool = False) -> Path:
        """Resolve a path by checking absolute, task-pack roots, then base_dir."""
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if must_exist and not candidate.exists():
                raise FileNotFoundError(f"Path not found: {candidate}")
            return candidate.resolve()

        search_roots = self.task_packs + [self.base_dir]
        for root in search_roots:
            resolved = (root / candidate).resolve()
            if not must_exist or resolved.exists():
                return resolved
        fallback = (self.base_dir / candidate).resolve()
        if must_exist:
            raise FileNotFoundError(f"Path not found in task packs or base dir: {candidate}")
        return fallback

    def grading_combine_layer(self) -> CombineLayer:
        """What this adapter's projects supply beneath a task's own ``combine`` block.

        The pre-run authoring gate resolves a task's effective combine from this and
        the task's own block. ``unresolvable`` is the honest default: an adapter that
        synthesises grading config rather than reading a project tree cannot say what
        a project supplies, and reporting "no defaults" instead would refuse a task
        whose weights are inherited.
        """
        return CombineLayer.unresolvable()

    @classmethod
    def grading_hash_source_layer(cls, task: TaskConfig, task_dir: Path) -> HashSourceLayer:
        """What this adapter supplies beneath a task's authored ``state_checks.hash`` block.

        Facts, not verdicts: report the source you compute the comparison from and
        whether it is usable, missing or empty, and the pre-run gates decide what is
        fatal — the same division :meth:`grading_combine_layer` draws. An adapter whose
        source lives in a fixture the authored block never names reports it here, and a
        block enabling the hash with nothing declared then passes on a usable source and
        is refused before any trial is paid for on a lost one. ``unresolvable`` is the
        honest default and the only answer that keeps the shape uncheckable.

        A classmethod, unlike :meth:`grading_combine_layer`: ``tolokaforge validate``
        is a static gate that holds no adapter instance and must keep validating packs
        whose adapter package is not installed, so every fact reported here has to be a
        function of the task and its directory alone.
        """
        return HashSourceLayer.unresolvable()

    @classmethod
    def grading_tool_inventory(cls, task: TaskConfig, task_dir: Path) -> ToolInventory:
        """The tool set this adapter presents at runtime, for tool-name checking.

        The pre-run authoring gate reads this to hold ``present`` / ``absent`` matchers
        and tool references in trace checks against a real set of names. An adapter
        whose runtime tool set is not the native reading of ``tools.agent.enabled`` —
        one that resolves tools from its own registry or a fixture the pack does not
        name — reports its own set here so the same rules protect its packs.
        :meth:`ToolInventory.unresolvable` is the honest default and the answer that
        keeps every tool-aware rule reported rather than refused for adapters that
        cannot answer.

        A classmethod, matching :meth:`grading_hash_source_layer`: ``tolokaforge
        validate`` holds no adapter instance and must keep validating packs whose
        adapter package is not installed, so every fact reported here has to be a
        function of the task and its directory alone. See ADR-0042.
        """
        return ToolInventory.unresolvable()

    @classmethod
    def grading_replay_world(cls, task: TaskConfig, task_dir: Path) -> ReplayWorld:
        """What this adapter gives a golden-action replay to be executed against.

        Two facts, neither of them readable from ``grading.yaml``: what the state a
        replay loads is (an initial-state JSON file, an inline mapping, or nothing),
        and whether the pack ships the tool module those actions call. The native
        reading is ``initial_state.json_db`` and ``tools.agent.mcp_server``; an
        adapter that builds a world from its own fixtures reports through this hook
        instead. :meth:`ReplayWorld.unresolvable` is the honest default. See
        ADR-0042.
        """
        return ReplayWorld.unresolvable()

    @classmethod
    def grading_seeded_tables(cls, task: TaskConfig, task_dir: Path) -> SeededTablesLayer:
        """The tables this adapter seeds, which the pack's ``id_fields`` decl keys.

        The pre-run authoring gate reads this to hold a declared primary key against
        a real view of the state the trial starts on. The native reading is
        ``initial_state.json_db``; an adapter whose seeded state lives elsewhere —
        a database fixture, a compose service's own seed dump — reports its tables
        through this hook. :meth:`SeededTablesLayer.unresolvable` is the honest
        default and the answer that leaves the declaration reported rather than
        refused where the tables cannot be read. See ADR-0042.
        """
        return SeededTablesLayer.unresolvable()

    @abstractmethod
    def get_task_ids(self) -> list[str]:
        """
        Get list of available task IDs.

        Returns:
            List of task identifiers
        """
        pass

    @abstractmethod
    def get_task(self, task_id: str) -> TaskConfig:
        """
        Load task configuration.

        Args:
            task_id: Task identifier

        Returns:
            TolokaForge TaskConfig
        """
        pass

    @abstractmethod
    def get_task_dir(self, task_id: str) -> Path:
        """
        Get directory containing task files.

        Args:
            task_id: Task identifier

        Returns:
            Path to task directory
        """
        pass

    @abstractmethod
    def create_environment(self, task_id: str) -> AdapterEnvironment:
        """
        Create and initialize environment for task.

        Args:
            task_id: Task identifier

        Returns:
            AdapterEnvironment with data, tools, wiki, rules
        """
        pass

    @abstractmethod
    def get_tools(self, task_id: str) -> list[Any]:
        """
        Get raw tools for task (adapter-specific format).

        Args:
            task_id: Task identifier

        Returns:
            List of tool classes/objects in adapter-native format
        """
        pass

    @abstractmethod
    def get_registry_tools(self, task_id: str, env: "AdapterEnvironment") -> list["Tool"]:
        """
        Get Tool instances ready for registry.

        This is the primary method orchestrator should use to get tools.
        Tools are pre-configured to work with the adapter's environment.

        Args:
            task_id: Task identifier
            env: AdapterEnvironment instance (tools will operate on env.data)

        Returns:
            List of Tool instances compatible with ToolRegistry
        """
        pass

    @abstractmethod
    def get_system_prompt(self, task_id: str) -> str:
        """
        Get system prompt for task.

        Args:
            task_id: Task identifier

        Returns:
            System prompt string
        """
        pass

    @abstractmethod
    def get_grading_config(self, task_id: str) -> GradingConfig:
        """
        Get grading configuration for task.

        Args:
            task_id: Task identifier

        Returns:
            GradingConfig instance
        """
        pass

    @abstractmethod
    def reset_environment(self, env: AdapterEnvironment) -> None:
        """
        Reset environment to initial state.

        Args:
            env: Environment to reset
        """
        pass

    @abstractmethod
    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        """
        Compute expected state hash by executing golden actions.

        Args:
            task_id: Task identifier
            env: Environment instance

        Returns:
            SHA256 hash of expected final state, or None if not applicable
        """
        pass

    @abstractmethod
    def to_task_description(self, task_id: str) -> Any:
        """Convert task to a TaskDescription for Docker Runner registration.

        The returned TaskDescription is serialized to JSON and sent to the
        Runner gRPC service via RegisterTrial. The Runner uses it to set up
        tools, environment state, and grading configuration.

        Args:
            task_id: Task identifier

        Returns:
            TaskDescription Pydantic model from tolokaforge.runner.models

        Raises:
            ValueError: If task_id not found
            NotImplementedError: If adapter does not support Docker runtime
        """
        pass

    # Adapters shipping a custom TrialGrader (registered under the
    # tolokaforge.trial_graders entry-point group) override this; the default
    # keeps runner-owned RPC grading.
    trial_grader_name: str = "runner_rpc"

    def docker_stack_requirements(self) -> DockerStackRequirements:
        """Declare extra Docker stack needs for this adapter.

        Default returns an empty requirements object — the orchestrator calls
        ``core_stack()`` with no extra kwargs. Adapters that need bind-mounts,
        a Docker socket, or a DinD sidecar override this.
        """
        return DockerStackRequirements()

    def stage_task(self, task_id: str) -> "StagedTask | None":
        """Return the per-task staging root a driver can layer onto.

        Adapters that ship container-based tasks (a per-task compose file
        + pack directory) override this to materialise a staging dir with
        a synthesised compose file the driver can rewrite in place. See
        :class:`~tolokaforge.core.agent_driver.StagedTask` for the fields
        the driver reads.

        The default returns ``None`` — this adapter has no container a
        driver can target. The orchestrator refuses a run whose driver
        needs staging (see
        :meth:`~tolokaforge.core.agent_driver.AgentDriver.needs_container_stage`)
        against an adapter that does not stage.
        """
        del task_id
        return None

    def fingerprint(self) -> dict[str, Any] | None:
        """What this adapter reports about the resolved inputs it ran on.

        A self-report. The engine records the returned payload verbatim under
        ``adapter_fingerprints[<adapter type>]`` on ``engine_run_state.json``
        and neither validates nor interprets it, so it must be JSON-safe.

        The default returns ``None``: an adapter reports nothing until it has
        resolved inputs worth naming.
        """
        return None

    def convert_to_native(self, task_id: str) -> NativeTaskBundle:
        """Convert an external task to native TolokaForge format.

        Conversion adapters override this to produce a
        :class:`NativeTaskBundle` that can be written to disk with
        :func:`tolokaforge.adapters.bundle_writer.write_bundle`.

        The default implementation raises :class:`NotImplementedError` — this
        is intentional: only adapters that wrap an *external* format need to
        implement conversion; :class:`NativeAdapter` already speaks native
        format.

        Args:
            task_id: Task identifier

        Returns:
            NativeTaskBundle ready for serialisation

        Raises:
            NotImplementedError: If the adapter does not support conversion.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support convert_to_native()")

    def write_shared_resources(self, output_dir: Path, bundle: NativeTaskBundle) -> None:
        """Write any shared, cross-task resources to *output_dir*.

        Called once per ``tolokaforge adapter convert`` run, with the first
        task's :class:`NativeTaskBundle`. The default is a **no-op** — most
        adapters emit nothing shared.

        Conversion adapters that produce shared resources (for example a
        ``_domain/`` bundle of libraries, a tool registry, or a knowledge base)
        override this to materialise them under *output_dir*. The
        implementation should be idempotent, since it may run alongside
        per-task :func:`bundle_writer.write_bundle` output in the same
        directory.

        Args:
            output_dir: The conversion output root (the same directory that
                receives the per-task ``{task_id}/`` folders).
            bundle: The first task's converted bundle, whose ``metadata`` an
                adapter can use to locate the resources to copy.
        """
        return None

    def grade(
        self,
        task_id: str,
        trajectory: Trajectory,
        final_state: dict[str, Any],
        env: AdapterEnvironment,
    ) -> Grade:
        """
        Grade a trajectory using adapter-specific logic.

        Default implementation uses GradingEngine with get_grading_config().
        Subclasses can override for specialized grading (e.g., hash comparison).

        Args:
            task_id: Task identifier
            trajectory: Trial trajectory with messages and metrics
            final_state: Final environment state
            env: Adapter environment with data after tool execution

        Returns:
            Grade with score and components
        """
        from tolokaforge.core.grading.combine import GradingEngine

        grading_config = self.get_grading_config(task_id)
        task_dir = self.get_task_dir(task_id)
        task = self.get_task(task_id)

        # Get MCP server ref from task config
        mcp_server_ref = task.tools.agent.get("mcp_server") if task.tools.agent else None

        # Create grading engine for the deterministic components. The rubric judge
        # is NOT run here — it lives runner-side (GradeTrial → core/grading/judge.py).
        grading_engine = GradingEngine(
            grading_config,
            task_domain=task.category if task.category else "general",
            task_dir=task_dir,
            task_initial_state=task.initial_state,
            task_mcp_server=mcp_server_ref,
        )

        return grading_engine.grade_trajectory(trajectory, final_state)
