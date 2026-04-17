"""Base adapter class for harness integration"""

import glob as glob_module
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Grade, GradingConfig, TaskConfig, Trajectory

if TYPE_CHECKING:
    from tolokaforge.tools.registry import Tool

logger = get_logger(__name__)


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

    def _iter_glob_matches(self, pattern: str, recursive: bool = True) -> list[Path]:
        """Return de-duplicated glob matches for a pattern across configured roots."""
        matches: list[Path] = []
        seen: set[Path] = set()
        for resolved_pattern in self._resolve_glob_patterns(pattern):
            for match in glob_module.glob(resolved_pattern, recursive=recursive):
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

    def convert_to_native(self, task_id: str) -> NativeTaskBundle:
        """Convert an external task to native TolokaForge format.

        External adapters (Tau, TlkMcpCore, …) override this to produce a
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
        from tolokaforge.core.models import ModelConfig

        grading_config = self.get_grading_config(task_id)
        task_dir = self.get_task_dir(task_id)
        task = self.get_task(task_id)

        # Create judge model if configured
        judge_model = None
        if grading_config.llm_judge and grading_config.llm_judge.model_ref:
            provider, model_name = grading_config.llm_judge.model_ref.split("/", 1)
            judge_model = ModelConfig(
                provider=provider,
                name=model_name,
                temperature=0.0,
            )

        # Get MCP server ref from task config
        mcp_server_ref = task.tools.agent.get("mcp_server") if task.tools.agent else None

        # Create grading engine with all required parameters for tau-style grading
        grading_engine = GradingEngine(
            grading_config,
            judge_model,
            task_domain=task.category if task.category else "general",
            task_dir=task_dir,
            task_initial_state=task.initial_state,
            task_mcp_server=mcp_server_ref,
        )

        # Determine workspace_dir for agentic judge file reading.
        # In non-docker mode: agent_visible_dir from final_state or env.
        # In docker mode: materialize filesystem to a temp dir if needed.
        workspace_dir = None
        agent_visible = final_state.get("agent_visible_dir")
        if agent_visible:
            workspace_dir = Path(agent_visible)
        elif getattr(env, "task_dir", None):
            workspace_dir = env.task_dir

        return grading_engine.grade_trajectory(trajectory, final_state, workspace_dir=workspace_dir)
