"""Native adapter for file-based TolokaForge tasks"""

import base64
import glob as glob_module
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tolokaforge.adapters._task_loader import (
    ToolActor,
    _detect_task_root,
    actor_tool_block,
    declared_tool_names,
    load_task_yaml,
    refuse_malformed_grading_shapes,
    resolve_tool_schemas,
    seeded_tables_from_task,
    tool_configs,
)
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.grading.checks_helpers import custom_checks_enabled
from tolokaforge.core.grading.config_validation import (
    CombineLayer,
    HashSourceLayer,
    authored_hash_block,
)
from tolokaforge.core.grading.golden_replay import require_replayable_golden_actions
from tolokaforge.core.grading.state_composition import StateHashConfig, refuse_retired_hash_keys
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import EnvironmentPatch, GradingConfig, TaskConfig
from tolokaforge.core.project_loader import (
    construct_config,
    project_grading_combine,
    resolve_effective_grading_combine,
    resolve_effective_judge_customization,
)
from tolokaforge.core.project_loader import resolve as resolve_environment
from tolokaforge.runner.id_resolution import check_id_fields_against_seeded_tables

if TYPE_CHECKING:
    from tolokaforge.runner.models import SearchConfig, TaskDescription, ToolSchema

logger = get_logger(__name__)


def _actor_tool_schemas(task: TaskConfig, task_dir: Path, actor: ToolActor) -> list["ToolSchema"]:
    """The wire tool set ``tools.<actor>`` declares, with every schema resolved.

    A builtin carries no :class:`ToolSource` — the runner's source-less dispatch
    arm routes it by name via the unified builtin registry, and ``tool_config``
    carries any per-task init kwargs. A block naming an ``mcp_server`` carries the
    script relative to the task dir, which the runner resolves against its
    extracted artifacts dir.

    Raises:
        RuntimeError: If the block names an ``mcp_server`` script that is absent.
        ValueError: If a ``tools.<actor>.<name>`` block is not a mapping.
    """
    from tolokaforge.runner.models import InvocationStyle, ToolSchema, ToolSource
    from tolokaforge.runner.tool_factory import create_search_kb_schema

    block = actor_tool_block(task, actor)
    mcp_server_ref: str | None = block.get("mcp_server")
    if mcp_server_ref and not (task_dir / mcp_server_ref).exists():
        raise RuntimeError(f"MCP server script not found: {task_dir / mcp_server_ref}")

    configs = tool_configs(task, actor)
    rich_schemas = resolve_tool_schemas(task, task_dir, actor, allow_subprocess=True)

    schemas: list[ToolSchema] = []
    for tool_name in block.get("enabled", []):
        if tool_name == "search_kb":
            # The runner reconstructs search_kb as a RAGSearchToolWrapper
            # (source-less, RAG dispatch). Carry the canonical schema so
            # the LLM sees the real {query, top_k, alpha} parameters.
            schemas.append(create_search_kb_schema())
            continue
        rich = rich_schemas.get(tool_name, {})
        source = (
            ToolSource(
                toolset=task.category or "native",
                module_path="mcp_server",
                class_name=tool_name,
                invocation_style=InvocationStyle.MCP_SERVER,
                mcp_server_script=mcp_server_ref,
            )
            if mcp_server_ref
            else None
        )
        schemas.append(
            ToolSchema(
                name=tool_name,
                description=rich.get(
                    "description", f"{actor.value.capitalize()} tool: {tool_name}"
                ),
                parameters=rich.get("parameters", {"type": "object", "properties": {}}),
                category="compute",
                timeout_s=30.0,
                source=source,
                tool_config=configs.get(tool_name, {}),
            )
        )
    return schemas


class NativeAdapter(BaseAdapter):
    """
    Adapter for native TolokaForge file-based tasks.

    This is the default adapter used when no harness_adapter is specified.
    It provides the same interface as external adapters while loading tasks
    from YAML files.

    Expected structure:
        tasks_glob: "tasks/project/tasks/**/task.yaml"

    Each task directory contains:
        - task.yaml: Task configuration
        - grading.yaml: Grading configuration
    """

    def __init__(self, params: dict[str, Any]):
        """
        Initialize native adapter.

        Args:
            params: Configuration parameters
                - tasks_glob: Glob pattern for task files (required)
                - base_dir: Base directory for resolving paths (default: ".")
                - task_packs: Optional list of pack root directories to search
        """
        super().__init__(params)
        self.tasks_glob = params["tasks_glob"]
        self.base_dir = Path(params.get("base_dir", "."))
        self.task_packs: list[str] = params.get("task_packs", [])
        # project.task_defaults dict from the enclosing project — layered
        # under every task.yaml at load time so shared task-level defaults
        # don't have to be repeated in each task file. Empty when the
        # caller (typically the Orchestrator) has no project context.
        self._project_task_defaults: dict[str, Any] = params.get("project_task_defaults", {})
        # project.default_environment patch from the enclosing project.
        # Bound to each task's own environment patch by
        # :func:`resolve_environment` in :meth:`to_task_description`.
        # ``None`` when the caller has no project or the project sets no
        # default environment.
        self._project_default_environment: EnvironmentPatch | None = params.get(
            "project_default_environment"
        )

        # Validate: tasks_glob must be relative when task_packs is provided
        if self.task_packs and Path(self.tasks_glob).is_absolute():
            raise ValueError(
                "tasks_glob must be relative when task_packs is provided, "
                f"got absolute path: {self.tasks_glob}"
            )

        # task_id -> task.yaml path. Populated in _discover_tasks.
        self._task_files: dict[str, Path] = {}
        # task_id -> effective task directory. For shared-domain tasks
        # (<dom>/testcases/<case>/task.yaml) this is <dom>; for flat-layout
        # tasks it is the task.yaml parent. Populated in _discover_tasks
        # alongside _task_files so callers asking only for the dir don't
        # incur a TaskConfig validation pass.
        self._task_roots: dict[str, Path] = {}
        # task_id -> validated TaskConfig. Populated lazily on first
        # get_task() call via :func:`load_task_yaml`.
        self._tasks: dict[str, TaskConfig] = {}

    def _discover_tasks(self) -> None:
        """Discover tasks matching glob pattern, optionally across task packs."""
        if self._task_files:
            return  # Already discovered

        if self.task_packs:
            # Search across all task packs
            for pack_root in self.task_packs:
                pack_path = Path(pack_root)
                pattern = str(pack_path / self.tasks_glob)
                self._discover_from_pattern(pattern)
        else:
            pattern = str(self.base_dir / self.tasks_glob)
            self._discover_from_pattern(pattern)

    def _discover_from_pattern(self, pattern: str) -> None:
        """Discover tasks matching a specific glob pattern.

        Supports bash-style brace expansion (e.g. ``{a,b,c}``) via
        :meth:`BaseAdapter._expand_braces`.
        """
        for expanded_pattern in self._expand_braces(pattern):
            for task_file in glob_module.glob(expanded_pattern, recursive=True):
                self._process_discovered_task_file(Path(task_file))

    def _process_discovered_task_file(self, task_path: Path) -> None:
        """Index a discovered task file by its task_id.

        Validation and the shared-domain merge happen lazily in
        :func:`load_task_yaml` on first :meth:`get_task` call. We only need
        ``task_id`` here to build the discovery index — reading the full file
        and merging the domain dict for every glob match would be wasteful
        when the caller only wants a list of IDs.
        """
        try:
            with open(task_path) as f:
                task_data = yaml.safe_load(f)
        except Exception:
            logger.warning(f"Invalid task file; skipping: {task_path}")
            return

        if not isinstance(task_data, dict):
            logger.warning(f"Invalid task file; skipping: {task_path}")
            return

        task_id = task_data.get("task_id")
        if not task_id:
            logger.warning(f"Task file missing task_id; skipping: {task_path}")
            return

        # First match wins for duplicate task_ids
        if task_id not in self._task_files:
            self._task_files[task_id] = task_path
            self._task_roots[task_id] = _detect_task_root(task_path)

    def get_task_ids(self) -> list[str]:
        """Get list of discovered task IDs"""
        self._discover_tasks()
        return list(self._task_files.keys())

    def get_task(self, task_id: str) -> TaskConfig:
        """Load and validate task configuration, applying any shared-domain merge."""
        self._discover_tasks()

        cached = self._tasks.get(task_id)
        if cached is not None:
            return cached

        if task_id not in self._task_files:
            raise ValueError(f"Task {task_id} not found")

        task_path = self._task_files[task_id]
        task, task_dir = load_task_yaml(
            task_path,
            project_task_defaults=self._project_task_defaults or None,
        )
        self._tasks[task_id] = task
        # Loader is the authority on the effective task dir; refresh the cache
        # populated by _discover_tasks so the two stay consistent. (They agree
        # by construction unless _detect_task_root semantics ever drift.)
        self._task_roots[task_id] = task_dir
        return task

    def register_preloaded_task(self, task: TaskConfig, task_dir: Path) -> None:
        """Seed the discovery caches with an already-validated task.

        Lets an in-process caller that already holds a :class:`TaskConfig` and
        its pack directory (e.g. :func:`tolokaforge.runner.run_trial`) resolve
        ``task.task_id`` through the standard asset-resolution methods
        (:meth:`to_task_description`, :meth:`get_grading_config`,
        :meth:`create_environment`, :meth:`get_system_prompt`) without a
        filesystem glob. Seeding ``_task_files`` also short-circuits
        :meth:`_discover_tasks`.
        """
        task_dir = Path(task_dir)
        self._task_files[task.task_id] = task_dir / "task.yaml"
        self._task_roots[task.task_id] = task_dir
        self._tasks[task.task_id] = task

    def get_task_dir(self, task_id: str) -> Path:
        """Get effective task directory.

        For shared-domain tasks (``<dom>/testcases/<case>/task.yaml``) this is
        the domain root so that downstream consumers — notably
        :meth:`_bundle_task_artifacts` — pick up ``_shared/`` siblings.

        Pure path lookup: never triggers TaskConfig validation. Callers that
        need the merged config call :meth:`get_task` separately.
        """
        self._discover_tasks()
        if task_id not in self._task_roots:
            raise ValueError(f"Task {task_id} not found")
        return self._task_roots[task_id]

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        """Create environment from task's initial_state config.

        Raises:
            RuntimeError: when ``initial_state.json_db`` is a string path that
                does not exist, or when ``system_prompt`` is set but the
                referenced file is missing. The previous silent fallback to an
                empty dict / empty wiki masked path-rewrite bugs as
                "agent-acted-on-empty-state" trial failures.
        """
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        data: dict[str, Any] = {}
        if task.initial_state.json_db:
            json_db = task.initial_state.json_db
            if isinstance(json_db, str):
                json_db_path = task_dir / json_db
                if not json_db_path.exists():
                    raise RuntimeError(
                        f"initial_state.json_db file not found for task {task_id!r}: "
                        f"{json_db_path} (ref: {json_db!r} relative to {task_dir})"
                    )
                with open(json_db_path) as f:
                    data = json.load(f)
            elif isinstance(json_db, dict):
                data = json_db

        wiki = ""
        if task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if not system_prompt_path.exists():
                raise RuntimeError(
                    f"system_prompt file not found for task {task_id!r}: "
                    f"{system_prompt_path} (ref: {task.system_prompt!r} relative to {task_dir})"
                )
            wiki = system_prompt_path.read_text()

        return AdapterEnvironment(
            data=data,
            tools=[],  # Tools loaded separately via MCP server
            wiki=wiki,
            rules=[],
            task_dir=task_dir,
        )

    def get_tools(self, task_id: str) -> list[Any]:
        """
        Get raw tools for task.

        For native tasks, tools are loaded via MCP server.
        This method returns empty list - MCP server provides tools.
        """
        return []

    def get_registry_tools(self, task_id: str, env: "AdapterEnvironment") -> list[Any]:
        """
        Get Tool instances ready for registry.

        For native tasks, tools are loaded via MCP server dynamically
        by the orchestrator. This returns empty list as MCP server
        provides tools directly to the registry.

        Future: Move MCP server loading into adapter for full encapsulation.
        """
        return []

    def get_system_prompt(self, task_id: str) -> str:
        """Get system prompt from task's system_prompt file.

        Raises ``RuntimeError`` when ``system_prompt`` is set in the task
        config but the file does not exist. Returns ``""`` only when the task
        legitimately has no system prompt — same reason as
        :meth:`create_environment`: silent empty-string masks path-rewrite
        bugs as "agent ignored its instructions" failures.
        """
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        if not task.system_prompt:
            return ""

        system_prompt_path = task_dir / task.system_prompt
        if not system_prompt_path.exists():
            raise RuntimeError(
                f"system_prompt file not found for task {task_id!r}: "
                f"{system_prompt_path} (ref: {task.system_prompt!r} relative to {task_dir})"
            )
        return system_prompt_path.read_text()

    def get_grading_config(self, task_id: str) -> GradingConfig:
        """Load grading configuration from task's grading file.

        Raises:
            ValueError: If the task names no grading file, or names one that is not
                on disk, or its ``state_checks.hash`` block populates a key in
                :data:`~tolokaforge.core.grading.state_composition.RETIRED_HASH_KEYS`.
            RuntimeError: If the file, or any grading key it declares, is neither a
                mapping nor absent.
        """
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        if task.grading is None:
            raise ValueError(
                f"task {task_id!r} has no grading configured "
                "(no `grading:` field and no sibling grading.yaml)"
            )

        grading_path = task_dir / task.grading
        if grading_path.exists():
            with open(grading_path) as f:
                grading_data = yaml.safe_load(f)
            refuse_malformed_grading_shapes(grading_data, grading_path=grading_path)
            refuse_retired_hash_keys(
                authored_hash_block(grading_data or {}),
                context=f"Grading file {grading_path}",
            )
            task_combine = grading_data.pop("combine", None)
            combine = resolve_effective_grading_combine(
                self._project_combine_defaults(), task_combine
            )
            return construct_config(
                GradingConfig,
                {**grading_data, "combine": combine},
                source=grading_path,
                section="grading",
            )

        raise ValueError(f"Grading config not found: {grading_path}")

    def grading_combine_layer(self) -> CombineLayer:
        return CombineLayer(self._project_combine_defaults())

    @classmethod
    def grading_hash_source_layer(cls, task: TaskConfig, task_dir: Path) -> HashSourceLayer:
        """Nothing beneath the block: a native pack's authored keys are the whole layer.

        An answer rather than an inability to answer, which is what makes an enabled
        hash declaring no source the authoring defect the gates refuse.
        """
        return HashSourceLayer()

    def _project_combine_defaults(self) -> dict[str, Any] | None:
        return project_grading_combine(self._project_task_defaults)

    def _project_judge_customization_defaults(self) -> dict[str, Any] | None:
        """The project's ``task_defaults.grading_defaults.llm_judge.customization``
        sub-dict, the base layer under each task's own
        ``grading.yaml.llm_judge.customization``. ``None`` when unset."""
        return (
            self._project_task_defaults.get("grading_defaults", {})
            .get("llm_judge", {})
            .get("customization")
        )

    def reset_environment(self, env: AdapterEnvironment) -> None:
        """Reset environment to initial state by reloading data"""
        # For native tasks, environment state is managed by MCP server
        # Reset is handled by orchestrator
        pass

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        """Return ``None``: no hash source a native pack can declare resolves here.

        Both remaining sources are evaluated by the substrate grading the trial, each in
        its own hash algebra — golden actions need the MCP server the grading engine
        holds, and the initial state is hashed beside the trial's own. #836 owns
        deleting the method.
        """
        return None

    def to_task_description(self, task_id: str) -> "TaskDescription":
        """
        Convert Native task to serializable TaskDescription for Docker Runner.

        Extracts:
        - Tools from MCP server configuration
        - Initial state from json_db
        - Initialization actions from task.yaml
        - Grading config from grading.yaml
        - System prompt from system_prompt file

        Args:
            task_id: Task identifier

        Returns:
            TaskDescription ready for Docker Runner

        Raises:
            ValueError: If task_id not found
            RuntimeError: If required files cannot be loaded, or if the grading file
                or any grading key it declares is neither a mapping nor absent
            pydantic.ValidationError: If a block the description is built from declares
                a key its model does not, or a value its model refuses —
                ``state_checks.hash`` included, which this errand constructs for itself
                rather than reading key by key.
            GoldenReplayError: ``state_checks.hash.golden_actions`` is truthy and is not
                the list of actions to replay, or an element of it is no mapping at all.
                Refused here rather than lowered onto the wire, this being the last
                surface before ``RegisterTrial``. A mapping element whose ``name`` is
                absent or empty is **not** refused: it reaches the wire as
                ``tool_name=""`` and fails at resolve time, which is #886.
        """
        from datetime import datetime, timezone

        from tolokaforge.runner.models import (
            AdapterType,
            DbProbe,
            GoldenAction,
            RunnerGradingConfig,
            RunnerInitializationAction,
            RunnerInitialStateConfig,
            RunnerStateChecksConfig,
            RunnerUserSimulatorConfig,
            TaskDescription,
            TraceChecksConfig,
            TranscriptRulesConfig,
        )

        logger.info(
            "Building TaskDescription", task_id=task_id, adapter_type=AdapterType.NATIVE.value
        )

        # Ensure tasks are discovered
        self._discover_tasks()

        if task_id not in self._task_files:
            raise ValueError(f"Task {task_id} not found in Native adapter")

        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        # Load system prompt
        system_prompt = ""
        if task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                system_prompt = system_prompt_path.read_text()
            else:
                raise RuntimeError(f"System prompt file not found: {system_prompt_path}")

        agent_tools = _actor_tool_schemas(task, task_dir, ToolActor.AGENT)
        user_tools = _actor_tool_schemas(task, task_dir, ToolActor.USER)
        mcp_server_ref: str | None = actor_tool_block(task, ToolActor.AGENT).get("mcp_server")

        initial_tables = seeded_tables_from_task(task, task_dir)

        # Build initialization actions. The core-side ``InitializationAction``
        # names the invoked tool ``func_name`` (author-facing); the runner-side
        # wire type names it ``tool_name``. Map here so the wire consumer sees
        # its expected shape without the author having to know either detail.
        initialization_actions: list[RunnerInitializationAction] = []
        if task.initial_state and task.initial_state.initialization_actions:
            for action in task.initial_state.initialization_actions:
                init_action = RunnerInitializationAction(
                    env_type=action.env_type,
                    tool_name=action.func_name,
                    arguments=action.arguments,
                )
                initialization_actions.append(init_action)

        # Load grading config
        grading_data = None
        if task.grading:
            grading_path = task_dir / task.grading
            if grading_path.exists():
                with open(grading_path) as f:
                    grading_data = yaml.safe_load(f)
                refuse_malformed_grading_shapes(grading_data, grading_path=grading_path)
                refuse_retired_hash_keys(
                    authored_hash_block(grading_data or {}),
                    context=f"Grading file {grading_path}",
                )

        # Build grading config
        state_checks = None
        transcript_rules = None

        if grading_data:
            # Build state checks
            state_checks_data = grading_data.get("state_checks", {})
            if state_checks_data:
                # Extract golden actions
                golden_actions: list[GoldenAction] = []
                # This errand never runs ``get_grading_config``, so the block is
                # constructed here too: a key it does not declare would otherwise reach
                # the wire as an absent hash. Only ``None`` — the key written bare — is
                # the empty block; every other value is the model's to answer, so
                # ``hash: 0`` is refused on both errands rather than on only one.
                raw_hash = state_checks_data.get("hash")
                hash_config = (
                    StateHashConfig()
                    if raw_hash is None
                    else StateHashConfig.model_validate(raw_hash)
                )
                if hash_config.enabled:
                    for golden_action in require_replayable_golden_actions(
                        hash_config.golden_actions,
                        context=f"Grading file {grading_path}",
                    ):
                        golden_actions.append(
                            GoldenAction(
                                tool_name=golden_action.get("name", ""),
                                arguments=golden_action.get("kwargs", {}),
                            )
                        )

                db_probes = [DbProbe(**probe) for probe in state_checks_data.get("db_probes", [])]

                id_fields_declared = dict(state_checks_data.get("id_fields", {}))
                relaxed_validation = bool(state_checks_data.get("relaxed_validation", False))
                err = check_id_fields_against_seeded_tables(
                    id_fields_declared,
                    initial_tables,
                    context=task_id,
                    relaxed=relaxed_validation,
                )
                if err:
                    raise ValueError(err)

                state_checks = RunnerStateChecksConfig(
                    hash_enabled=hash_config.enabled,
                    expect_initial_state=hash_config.expect_initial_state,
                    golden_actions=golden_actions,
                    hash_weight=hash_config.weight,
                    jsonpath_checks=state_checks_data.get("jsonpaths", []),
                    db_probes=db_probes,
                    numeric_string_fields=list(state_checks_data.get("numeric_string_fields", [])),
                    id_fields=id_fields_declared,
                    relaxed_validation=relaxed_validation,
                )

            # Build transcript rules. One model serves the authored block and the
            # wire, so the block is validated rather than copied field by field:
            # a key it does not declare, or an element missing one it requires, is
            # refused here instead of reaching the runner as a default.
            transcript_data = grading_data.get("transcript_rules", {})
            if transcript_data:
                transcript_rules = TranscriptRulesConfig(**transcript_data)

        # Build LLM judge config
        #
        # ``rubric`` is now a structured ``Rubric`` (criteria + optional
        # reference), not free text. The old ``rubric: str`` / ``output_schema``
        # shape is rejected by ``Rubric``/``LLMJudgeConfig`` (extra="forbid"),
        # surfacing a migration error during validate.
        # Gate on rubric presence — the judge model moved to the run config
        # (models.judge), so ``model_ref`` no longer exists on this block. A
        # lingering ``model_ref`` is rejected loudly by ``LLMJudgeConfig``.
        llm_judge_config = None
        llm_judge_data = (grading_data.get("llm_judge") if grading_data else None) or {}
        # ``customization`` (sibling of ``rubric``) layers project→task; attach it
        # only when a layer actually set it, so a task with no block parses to an
        # identical ``LLMJudgeConfig`` with no nested customization object attached
        # (the wire carries a plain ``"customization": null``).
        task_customization = llm_judge_data.pop("customization", None)
        project_customization = self._project_judge_customization_defaults()
        effective_customization = None
        if task_customization is not None or project_customization is not None:
            effective_customization = resolve_effective_judge_customization(
                project_customization, task_customization
            )
        if llm_judge_data and llm_judge_data.get("rubric"):
            from tolokaforge.runner.models import LLMJudgeConfig as RunnerLLMJudgeConfig

            llm_judge_config = RunnerLLMJudgeConfig(
                **llm_judge_data, customization=effective_customization
            )

        # Build combined grading config
        combine_data = grading_data.get("combine") if grading_data else None
        effective_combine = resolve_effective_grading_combine(
            self._project_combine_defaults(), combine_data
        )
        custom_checks_data = grading_data.get("custom_checks") if grading_data else None
        trace_checks_data = grading_data.get("trace_checks") if grading_data else None
        grading_config = RunnerGradingConfig(
            combine_method=effective_combine.method,
            weights=effective_combine.weights,
            pass_threshold=effective_combine.pass_threshold,
            state_checks=state_checks,
            transcript_rules=transcript_rules,
            trace_checks=TraceChecksConfig(**trace_checks_data) if trace_checks_data else None,
            llm_judge=llm_judge_config,
            custom_checks=custom_checks_data,
        )

        # Build user simulator config
        sim = task.resolve_user_simulator()
        user_simulator = RunnerUserSimulatorConfig(
            mode=sim.mode,
            persona=sim.persona,
            backstory=sim.backstory or "",
        )

        # Build filesystem state from initial_state.filesystem.copy so the
        # Runner can provision agent-visible files at RegisterTrial time.
        initial_filesystem: dict[str, str] = {}
        if task.initial_state and task.initial_state.filesystem:
            copy_spec = task.initial_state.filesystem.get("copy", [])
            for file_spec in copy_spec:
                src_path = task_dir / file_spec["from"]
                dest_path = file_spec["to"]
                if src_path.exists():
                    content = src_path.read_text(encoding="utf-8")
                    initial_filesystem[dest_path] = content
                else:
                    logger.warning(
                        "Filesystem file not found for initial state",
                        src=str(src_path),
                        dest=dest_path,
                    )

        # Build initial state config
        initial_state = RunnerInitialStateConfig(
            tables=initial_tables,
            schemas=[],
            unstable_fields=[],
            filesystem=initial_filesystem,
        )

        # Build source files for debugging
        source_files = {
            "task": str(self._task_files[task_id]),
        }
        if task.grading:
            source_files["grading"] = str(task_dir / task.grading)
        if task.system_prompt:
            source_files["system_prompt"] = str(task_dir / task.system_prompt)

        # Bundle task directory files as base64 artifacts for Docker Runner.
        # The Runner runs in a separate container without access to the host
        # filesystem, so we transfer all necessary files via gRPC/TaskDescription.
        # Custom checks packs (with or without an MCP server) also need the
        # bundle so the runner can resolve ``custom_checks.file`` and every
        # ``relative_imports`` path under the trial's ``artifacts_dir``. Either
        # actor's server counts: a user tool is reconstructed from the same
        # script path the agent's is.
        needs_bundle = (
            mcp_server_ref
            or actor_tool_block(task, ToolActor.USER).get("mcp_server")
            or custom_checks_enabled(custom_checks_data)
        )
        tool_artifacts = self._bundle_task_artifacts(task_dir) if needs_bundle else {}

        # mcp_server.py loads its initial state from ``initial_state.json``
        # next to itself (see ``create_server`` in tools_interface.py). For the
        # shared-domain layout the mcp_server lives under ``_shared/`` while
        # each testcase ships its own per-case state file — copy that
        # per-case JSON to ``<mcp_dir>/initial_state.json`` so the per-trial
        # subprocess loads the right state.
        if mcp_server_ref and task.initial_state and isinstance(task.initial_state.json_db, str):
            json_db_rel = task.initial_state.json_db
            json_db_abs = task_dir / json_db_rel
            mcp_dir_rel = Path(mcp_server_ref).parent
            target_rel = (
                f"{mcp_dir_rel.as_posix()}/initial_state.json"
                if mcp_dir_rel != Path(".")
                else "initial_state.json"
            )
            if json_db_abs.exists() and target_rel != json_db_rel:
                tool_artifacts[target_rel] = base64.b64encode(json_db_abs.read_bytes()).decode(
                    "ascii"
                )

        # Resolve per-trial RAG search from ``initial_state.rag``. When a
        # corpus is declared, its files travel in ``tool_artifacts`` (keyed
        # under the declared ``corpus_dir``) so the runner can index them.
        search_config = self._resolve_search_config(task, task_dir, task_id)
        if search_config.documents_path:
            tool_artifacts.update(
                self._bundle_corpus_artifacts(task_dir, search_config.documents_path)
            )

        # Bind the project's default_environment patch to the task's own
        # patch via :func:`resolve_environment`; the resolver merges them
        # (with the atomic-``stack`` rule) and constructs the
        # ``EnvironmentManifest`` — the point where the compose file's
        # existence and safety validators run.
        environment_manifest = resolve_environment(
            self._project_default_environment,
            task.environment_manifest,
        )

        # Create TaskDescription
        task_description = TaskDescription(
            task_id=task_id,
            name=task.name or task_id,
            category=task.category or "native",
            description=task.description or "",
            adapter_type=AdapterType.NATIVE,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
            user_tools=user_tools,
            initial_state=initial_state,
            initialization_actions=initialization_actions,
            user_simulator=user_simulator,
            search=search_config,
            grading=grading_config,
            source_files=source_files,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "mcp_server_ref": mcp_server_ref,
            },
            tool_artifacts=tool_artifacts,
            environment_manifest=environment_manifest,
        )

        logger.info(
            "Built TaskDescription",
            task_id=task_id,
            agent_tools_count=len(agent_tools),
            user_tools_count=len(user_tools),
            tables_count=len(initial_tables),
            initialization_actions_count=len(initialization_actions),
            tool_artifacts_count=len(tool_artifacts),
        )

        return task_description

    # ------------------------------------------------------------------
    # Task artifact bundling (for Docker execution)
    # ------------------------------------------------------------------

    def _resolve_search_config(
        self,
        task: TaskConfig,
        task_dir: Path,
        task_id: str,
    ) -> "SearchConfig":
        """Build the trial's ``SearchConfig`` from ``initial_state.rag``.

        A task that declares ``initial_state.rag.corpus_dir`` opts into
        per-trial RAG indexing: the corpus files travel in ``tool_artifacts``
        and the runner indexes them so ``search_kb`` returns the corpus's
        documents. ``documents_path`` is the declared ``corpus_dir`` verbatim,
        resolved runner-side against the extracted artifacts dir, and the plane
        serving it is declared ``rag_service`` so a run that also configures
        TypeSense does not pull the corpus onto the other plane. Tasks that
        declare no corpus keep search disabled.

        Raises:
            ValueError: if a corpus is declared without ``search_kb`` in either
                actor's tools (the corpus could never be searched), or the
                declared ``corpus_dir`` does not resolve to a directory.
        """
        from tolokaforge.runner.models import SearchConfig, SearchPlane

        rag = task.initial_state.rag
        corpus_dir = rag.get("corpus_dir") if rag else None
        if not corpus_dir:
            return SearchConfig(enabled=False)
        if not isinstance(corpus_dir, str):
            raise ValueError(
                f"Task {task_id!r} initial_state.rag.corpus_dir must be a string path, "
                f"got {type(corpus_dir).__name__}={corpus_dir!r}"
            )

        if "search_kb" not in declared_tool_names(task):
            raise ValueError(
                f"Task {task_id!r} declares initial_state.rag.corpus_dir "
                f"{corpus_dir!r} but no actor enables the 'search_kb' tool; "
                f"the corpus would never be searchable. Add 'search_kb' to "
                f"tools.agent.enabled or tools.user.enabled, or drop the rag corpus."
            )

        corpus_path = task_dir / corpus_dir
        if not corpus_path.is_dir():
            raise ValueError(
                f"Task {task_id!r} declares initial_state.rag.corpus_dir "
                f"{corpus_dir!r} but {corpus_path} is not a directory."
            )

        return SearchConfig(
            enabled=True,
            plane=SearchPlane.RAG_SERVICE,
            domain_name=task.category or task_id,
            documents_path=corpus_dir,
        )

    def _bundle_corpus_artifacts(self, task_dir: Path, corpus_dir: str) -> dict[str, str]:
        """Bundle the RAG corpus's ``.md``/``.txt`` files as base64 artifacts.

        Only the corpus files travel, keyed under the declared *corpus_dir*
        prefix, so the runner resolves ``artifacts_dir / documents_path`` to
        the same tree. Globs are flat (non-recursive), matching
        ``load_documents_from_directory``. The whole task directory is
        deliberately NOT bundled — that would ship ``grading.yaml`` (which may
        carry a planted retrieval fact) into the runner.

        Raises:
            ValueError: if the corpus directory holds no ``.md``/``.txt`` files.
        """
        corpus_path = task_dir / corpus_dir
        artifacts: dict[str, str] = {}
        for pattern in ("*.md", "*.txt"):
            for file_path in sorted(corpus_path.glob(pattern)):
                if not file_path.is_file():
                    continue
                rel_path = f"{corpus_dir}/{file_path.name}"
                artifacts[rel_path] = base64.b64encode(file_path.read_bytes()).decode("ascii")
        if not artifacts:
            raise ValueError(f"RAG corpus at {corpus_path} contains no .md or .txt files to index.")
        return artifacts

    def _bundle_task_artifacts(self, task_dir: Path) -> dict[str, str]:
        """Bundle task directory files as base64-encoded artifacts.

        Reads Python sources, JSON/YAML data files, Markdown, and plain text
        from *task_dir* (recursively) and encodes them so the Docker Runner
        can extract them into a temporary directory and launch
        ``mcp_server.py`` as a subprocess without requiring host filesystem
        access.

        The keys are relative paths (e.g. ``"mcp_server.py"``,
        ``"tools/orders.py"``, ``"_shared/system_prompt.md"``) and the values
        are base64-encoded file contents. The Runner reconstructs the same
        layout in a temp directory and passes the resolved absolute path to
        :class:`MCPServerToolWrapper`.

        Recursive globs (``**/*``) are required by the shared-domain layout
        where ``task_dir`` is the domain root and the actual files live under
        ``_shared/`` and ``testcases/<case>/``.

        Returns:
            dict mapping relative path → base64-encoded content.
        """
        artifacts: dict[str, str] = {}

        for pattern in (
            "*.py",
            "**/*.py",
            "*.json",
            "**/*.json",
            "*.yaml",
            "**/*.yaml",
            "*.yml",
            "**/*.yml",
            "*.md",
            "**/*.md",
            "*.txt",
            "**/*.txt",
        ):
            for file_path in task_dir.glob(pattern):
                if not file_path.is_file():
                    continue
                rel_path = file_path.relative_to(task_dir).as_posix()
                if rel_path in artifacts:
                    continue
                try:
                    artifacts[rel_path] = base64.b64encode(file_path.read_bytes()).decode("ascii")
                except Exception as e:
                    logger.warning("Could not bundle artifact", path=rel_path, error=str(e))

        logger.info("Bundled task artifacts", count=len(artifacts), task_dir=str(task_dir))
        return artifacts
