"""Native adapter for file-based TolokaForge tasks"""

import glob as glob_module
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import GradingConfig, TaskConfig

if TYPE_CHECKING:
    from tolokaforge.runner.models import TaskDescription

logger = get_logger(__name__)

# Map of builtin tool names → lazy loaders that return (description, parameters).
# We avoid importing the tool classes at module level to keep the adapter lightweight.
_BUILTIN_TOOL_CLASSES: dict[str, tuple[str, str]] = {
    "read_file": ("tolokaforge.tools.builtin.files", "ReadFileTool"),
    "write_file": ("tolokaforge.tools.builtin.files", "WriteFileTool"),
    "bash": ("tolokaforge.tools.builtin.bash", "BashTool"),
}


def _builtin_tool_schemas(tool_names: list[str]) -> dict[str, dict]:
    """Return rich schemas for known builtin tools.

    For each tool name in *tool_names* that corresponds to a builtin
    implementation, instantiate the tool and extract its ``get_schema()``
    information so the LLM gets proper parameter descriptions.
    """
    import importlib

    schemas: dict[str, dict] = {}
    for name in tool_names:
        entry = _BUILTIN_TOOL_CLASSES.get(name)
        if entry is None:
            continue
        module_path, class_name = entry
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            tool = cls()
            raw = tool.get_schema()
            # get_schema() returns {"type": "function", "function": {…}}
            func_def = raw.get("function", raw)
            schemas[name] = {
                "description": func_def.get("description", f"Builtin tool: {name}"),
                "parameters": func_def.get("parameters", {"type": "object", "properties": {}}),
            }
        except Exception as exc:
            logger.debug("Could not load builtin schema", tool_name=name, error=str(exc))
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

        # Validate: tasks_glob must be relative when task_packs is provided
        if self.task_packs and Path(self.tasks_glob).is_absolute():
            raise ValueError(
                "tasks_glob must be relative when task_packs is provided, "
                f"got absolute path: {self.tasks_glob}"
            )

        # Cached data
        self._task_files: dict[str, Path] = {}  # task_id -> task.yaml path
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
        """Discover tasks matching a specific glob pattern."""
        for task_file in glob_module.glob(pattern, recursive=True):
            task_path = Path(task_file)
            try:
                with open(task_path) as f:
                    task_data = yaml.safe_load(f)
            except Exception:
                logger.warning(f"Invalid task file; skipping: {task_path}")
                continue

            if not isinstance(task_data, dict):
                logger.warning(f"Invalid task file; skipping: {task_path}")
                continue

            task_id = task_data.get("task_id")
            if not task_id:
                logger.warning(f"Task file missing task_id; skipping: {task_path}")
                continue

            # First match wins for duplicate task_ids
            if task_id not in self._task_files:
                self._task_files[task_id] = task_path

    def get_task_ids(self) -> list[str]:
        """Get list of discovered task IDs"""
        self._discover_tasks()
        return list(self._task_files.keys())

    def get_task(self, task_id: str) -> TaskConfig:
        """Load task configuration from YAML file"""
        self._discover_tasks()

        if task_id in self._tasks:
            return self._tasks[task_id]

        if task_id not in self._task_files:
            raise ValueError(f"Task {task_id} not found")

        task_path = self._task_files[task_id]
        with open(task_path) as f:
            task_data = yaml.safe_load(f)

        task = TaskConfig(**task_data)
        self._tasks[task_id] = task
        return task

    def get_task_dir(self, task_id: str) -> Path:
        """Get directory containing task files"""
        self._discover_tasks()
        if task_id not in self._task_files:
            raise ValueError(f"Task {task_id} not found")
        return self._task_files[task_id].parent

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        """
        Create environment from task's initial_state config.

        Loads JSON DB, filesystem, etc. based on task configuration.
        """
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        # Load initial data
        data: dict[str, Any] = {}
        if task.initial_state.json_db:
            json_db = task.initial_state.json_db
            if isinstance(json_db, str):
                json_db_path = task_dir / json_db
                if json_db_path.exists():
                    with open(json_db_path) as f:
                        data = json.load(f)
            elif isinstance(json_db, dict):
                data = json_db

        # Load wiki/system prompt
        wiki = ""
        if task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
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
        """Get system prompt from task's system_prompt file"""
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        if task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                return system_prompt_path.read_text()

        return ""

    def get_grading_config(self, task_id: str) -> GradingConfig:
        """Load grading configuration from task's grading file"""
        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)

        grading_path = task_dir / task.grading
        if grading_path.exists():
            with open(grading_path) as f:
                grading_data = yaml.safe_load(f)
            return GradingConfig(**grading_data)

        raise ValueError(f"Grading config not found: {grading_path}")

    def reset_environment(self, env: AdapterEnvironment) -> None:
        """Reset environment to initial state by reloading data"""
        # For native tasks, environment state is managed by MCP server
        # Reset is handled by orchestrator
        pass

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        """
        Compute golden hash for state comparison.

        For native tasks, uses golden_actions from grading.yaml if present.
        Returns None if no hash-based grading configured.
        """
        grading = self.get_grading_config(task_id)

        if not grading.state_checks or not grading.state_checks.hash:
            return None

        hash_config = grading.state_checks.hash
        if not hash_config.get("enabled", False):
            return None

        golden_actions = hash_config.get("golden_actions", [])
        if not golden_actions:
            # Pre-computed hash
            return hash_config.get("expected_state_hash")

        # Execute golden actions to compute hash dynamically
        # This requires MCP server access - delegated to grading engine
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
            RuntimeError: If required files cannot be loaded
        """
        from datetime import datetime, timezone

        from tolokaforge.runner.models import (
            AdapterType,
            EnvAssertion,
            GoldenAction,
            InitializationAction,
            InvocationStyle,
            RequiredAction,
            SearchConfig,
            StateChecksConfig,
            TaskDescription,
            ToolSchema,
            ToolSource,
            TranscriptRulesConfig,
        )
        from tolokaforge.runner.models import (
            GradingConfig as RunnerGradingConfig,
        )
        from tolokaforge.runner.models import (
            InitialStateConfig as RunnerInitialStateConfig,
        )
        from tolokaforge.runner.models import (
            UserSimulatorConfig as RunnerUserSimulatorConfig,
        )

        logger.info("Building TaskDescription", task_id=task_id, adapter_type="native")

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

        # Build agent tools from MCP server
        agent_tools: list[ToolSchema] = []
        user_tools: list[ToolSchema] = []  # always empty: user.enabled is [] in all native tasks

        # Get MCP server path for agent tools
        mcp_server_ref: str | None = None
        if task.tools and task.tools.agent:
            mcp_server_ref = task.tools.agent.get("mcp_server")
            if mcp_server_ref:
                mcp_server_path = task_dir / mcp_server_ref
                if not mcp_server_path.exists():
                    raise RuntimeError(f"MCP server script not found: {mcp_server_path}")

            # Load rich schemas from fixtures/tools.json or via live MCP query.
            # This populates real descriptions and parameter schemas (including
            # required fields) so the LLM receives accurate tool definitions.
            rich_schemas: dict[str, dict] = {}
            if mcp_server_ref:
                rich_schemas = self._load_rich_tool_schemas(task_dir, task_dir / mcp_server_ref)
            else:
                # No MCP server — pull parameter schemas from builtin tool
                # implementations so the LLM receives proper descriptions.
                enabled_tools_list = task.tools.agent.get("enabled", [])
                rich_schemas = _builtin_tool_schemas(enabled_tools_list)

            # Build tool schemas for enabled agent tools
            enabled_tools = task.tools.agent.get("enabled", [])
            for tool_name in enabled_tools:
                rich = rich_schemas.get(tool_name, {})
                # Only wire up MCP_SERVER source when the task provides an mcp_server
                # script. Builtin tools (read_file, write_file, bash, …) have no
                # script — the runner resolves them via its builtin registry when
                # source is None.
                source = (
                    ToolSource(
                        toolset=task.category or "native",
                        module_path="mcp_server",
                        class_name=tool_name,
                        invocation_style=InvocationStyle.MCP_SERVER,
                        # Relative path — Runner resolves it against the extracted artifacts dir.
                        mcp_server_script=mcp_server_ref,
                    )
                    if mcp_server_ref
                    else None
                )
                tool_schema = ToolSchema(
                    name=tool_name,
                    description=rich.get("description", f"Agent tool: {tool_name}"),
                    parameters=rich.get("parameters", {"type": "object", "properties": {}}),
                    category="compute",
                    timeout_s=30.0,
                    source=source,
                )
                agent_tools.append(tool_schema)

        # Build initial state from json_db
        initial_tables: dict[str, list[dict[str, Any]]] = {}
        if task.initial_state and task.initial_state.json_db:
            json_db = task.initial_state.json_db
            if isinstance(json_db, str):
                json_db_path = task_dir / json_db
                if json_db_path.exists():
                    with open(json_db_path) as f:
                        data = json.load(f)
                    # Convert data to table format
                    for collection_name, collection_data in data.items():
                        if isinstance(collection_data, list):
                            # List of records
                            records = collection_data
                        elif isinstance(collection_data, dict):
                            # Check if this is a single record or a dict of records
                            # A single record has primitive values (str, int, bool, etc.)
                            # A dict of records has dict values
                            values = list(collection_data.values())
                            if values and all(isinstance(v, dict) for v in values):
                                # Dict of records keyed by ID
                                records = values
                            else:
                                # Single record - wrap in list
                                records = [collection_data]
                        else:
                            records = [collection_data]
                        initial_tables[collection_name] = records
                else:
                    raise RuntimeError(f"JSON DB file not found: {json_db_path}")
            elif isinstance(json_db, dict):
                for collection_name, collection_data in json_db.items():
                    if isinstance(collection_data, list):
                        # List of records
                        records = collection_data
                    elif isinstance(collection_data, dict):
                        # Check if this is a single record or a dict of records
                        values = list(collection_data.values())
                        if values and all(isinstance(v, dict) for v in values):
                            # Dict of records keyed by ID
                            records = values
                        else:
                            # Single record - wrap in list
                            records = [collection_data]
                    else:
                        records = [collection_data]
                    initial_tables[collection_name] = records

        # Build initialization actions
        initialization_actions: list[InitializationAction] = []
        if task.initial_state and task.initial_state.initialization_actions:
            for action in task.initial_state.initialization_actions:
                # action is already an InitializationAction Pydantic model from core.models
                init_action = InitializationAction(
                    env_type=action.env_type,
                    tool_name=action.tool_name,
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

        # Build grading config
        state_checks = None
        transcript_rules = None

        if grading_data:
            # Build state checks
            state_checks_data = grading_data.get("state_checks", {})
            if state_checks_data:
                # Extract golden actions
                golden_actions: list[GoldenAction] = []
                hash_config = state_checks_data.get("hash", {})
                if hash_config and hash_config.get("enabled", False):
                    for action in hash_config.get("golden_actions", []):
                        golden_actions.append(
                            GoldenAction(
                                tool_name=action.get("name", ""),
                                arguments=action.get("kwargs", {}),
                            )
                        )

                # Extract env assertions
                env_assertions: list[EnvAssertion] = []
                for assertion in state_checks_data.get("env_assertions", []):
                    env_assertions.append(
                        EnvAssertion(
                            env_type=assertion.get("env_type", "user"),
                            func_name=assertion.get("tool_name", ""),
                            arguments=assertion.get("arguments", {}),
                            assert_value=assertion.get("assert_value", True),
                            message=assertion.get("message"),
                        )
                    )

                state_checks = StateChecksConfig(
                    hash_enabled=bool(hash_config and hash_config.get("enabled", False)),
                    expected_hash=hash_config.get("expected_state_hash") if hash_config else None,
                    golden_actions=golden_actions,
                    jsonpath_checks=state_checks_data.get("jsonpaths", []),
                    env_assertions=env_assertions,
                )

            # Build transcript rules
            transcript_data = grading_data.get("transcript_rules", {})
            if transcript_data:
                required_actions: list[RequiredAction] = []
                for action in transcript_data.get("required_actions", []):
                    required_actions.append(
                        RequiredAction(
                            action_id=action.get("action_id", ""),
                            requestor=action.get("requestor", "user"),
                            tool_name=action.get("name", ""),
                            arguments=action.get("arguments", {}),
                            compare_args=action.get("compare_args"),
                        )
                    )

                transcript_rules = TranscriptRulesConfig(
                    must_contain=transcript_data.get("must_contain", []),
                    disallow_regex=transcript_data.get("disallow_regex", []),
                    max_turns=transcript_data.get("max_turns"),
                    required_actions=required_actions,
                    communicate_info=transcript_data.get("communicate_info", []),
                )

        # Build combined grading config
        combine_data = grading_data.get("combine", {}) if grading_data else {}
        grading_config = RunnerGradingConfig(
            combine_method=combine_data.get("method", "weighted"),
            weights=combine_data.get("weights", {"state_checks": 1.0}),
            pass_threshold=combine_data.get("pass_threshold", 1.0),
            state_checks=state_checks,
            transcript_rules=transcript_rules,
        )

        # Build user simulator config
        user_simulator = RunnerUserSimulatorConfig(
            mode=task.user_simulator.mode if task.user_simulator else "llm",
            persona=task.user_simulator.persona if task.user_simulator else "cooperative",
            backstory=(
                task.user_simulator.backstory
                if task.user_simulator and task.user_simulator.backstory
                else ""
            ),
        )

        # Build initial state config
        initial_state = RunnerInitialStateConfig(
            tables=initial_tables,
            schemas=[],
            unstable_fields=[],
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
        tool_artifacts = self._bundle_task_artifacts(task_dir) if mcp_server_ref else {}

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
            search=SearchConfig(enabled=False),
            grading=grading_config,
            source_files=source_files,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "mcp_server_ref": mcp_server_ref,
            },
            tool_artifacts=tool_artifacts,
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

    def _fetch_mcp_tool_schemas(self, mcp_server_path: Path) -> dict[str, dict]:
        """Fetch rich tool schemas from an MCP server via tools/list.

        Starts the server as a subprocess, performs the MCP handshake, calls
        ``tools/list``, and converts the response to the same format used by
        ``fixtures/tools.json``:

        .. code-block:: json

            [{"name": "...", "description": "...", "parameters": { ... }}]

        The ``inputSchema`` field returned by MCP becomes ``parameters`` in the
        stored/returned format, matching what ``FrozenMcpCoreAdapter`` uses.

        Args:
            mcp_server_path: Absolute path to ``mcp_server.py``.

        Returns:
            Dict mapping tool_name → ``{"name", "description", "parameters"}``.
            Returns empty dict on any error so callers can fall back gracefully.
        """
        try:
            from tolokaforge.runner.tool_factory import MCPServerProcess

            server = MCPServerProcess(script_path=str(mcp_server_path))
            try:
                server.start()
                result = server.send_request("tools/list", {})
                tools_list = result.get("tools", [])
                schemas: dict[str, dict] = {}
                for tool in tools_list:
                    name = tool.get("name", "")
                    if not name:
                        continue
                    schemas[name] = {
                        "name": name,
                        "description": tool.get("description", f"Tool: {name}"),
                        "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                    }
                logger.info(
                    "Fetched MCP tool schemas",
                    count=len(schemas),
                    server=str(mcp_server_path),
                )
                return schemas
            finally:
                server.stop()
        except Exception as exc:
            logger.warning(
                "Could not fetch MCP tool schemas; falling back to empty schemas",
                server=str(mcp_server_path),
                error=str(exc),
            )
            return {}

    def _load_rich_tool_schemas(self, task_dir: Path, mcp_server_path: Path) -> dict[str, dict]:
        """Load rich tool schemas, preferring a static ``fixtures/tools.json``.

        Resolution order:

        1. ``task_dir/fixtures/tools.json`` — pre-generated static file (fastest,
           avoids subprocess overhead, matches ``FrozenMcpCoreAdapter`` pattern).
        2. Live MCP query via ``tools/list`` — used when the static file does not
           exist yet; result is written back to ``fixtures/tools.json`` so that
           subsequent runs are fast.

        Args:
            task_dir: Task directory (where ``fixtures/`` lives).
            mcp_server_path: Absolute path to ``mcp_server.py``.

        Returns:
            Dict mapping tool_name → schema dict.
        """
        import json

        fixtures_dir = task_dir / "fixtures"
        tools_json_path = fixtures_dir / "tools.json"

        # 1. Static file — fast path
        if tools_json_path.exists():
            try:
                with open(tools_json_path) as f:
                    tools_list = json.load(f)
                schemas = {t["name"]: t for t in tools_list if isinstance(t, dict) and "name" in t}
                logger.info(
                    "Loaded tool schemas from fixtures/tools.json",
                    count=len(schemas),
                    path=str(tools_json_path),
                )
                return schemas
            except Exception as exc:
                logger.warning(
                    "Failed to read fixtures/tools.json; falling back to MCP query",
                    path=str(tools_json_path),
                    error=str(exc),
                )

        # 2. Live MCP query — generate and cache
        schemas = self._fetch_mcp_tool_schemas(mcp_server_path)
        if schemas:
            try:
                fixtures_dir.mkdir(parents=True, exist_ok=True)
                tools_list_out = list(schemas.values())
                with open(tools_json_path, "w") as f:
                    json.dump(tools_list_out, f, indent=2)
                logger.info(
                    "Generated and cached fixtures/tools.json",
                    path=str(tools_json_path),
                    count=len(schemas),
                )
            except Exception as exc:
                logger.warning(
                    "Could not write fixtures/tools.json cache",
                    path=str(tools_json_path),
                    error=str(exc),
                )

        return schemas

    def _bundle_task_artifacts(self, task_dir: Path) -> dict[str, str]:
        """Bundle task directory files as base64-encoded artifacts.

        Reads Python sources, JSON/YAML data files, and Markdown files from
        *task_dir* and encodes them so the Docker Runner can extract them into
        a temporary directory and launch ``mcp_server.py`` as a subprocess
        without requiring host filesystem access.

        The keys are relative paths (e.g. ``"mcp_server.py"``,
        ``"tools/orders.py"``) and the values are base64-encoded file contents.
        The Runner reconstructs the same layout in a temp directory and passes
        the resolved absolute path to :class:`MCPServerToolWrapper`.

        Returns:
            dict mapping relative path → base64-encoded content.
        """
        import base64

        artifacts: dict[str, str] = {}

        for pattern in ["*.py", "**/*.py"]:
            for file_path in task_dir.glob(pattern):
                if file_path.is_file():
                    rel_path = file_path.relative_to(task_dir).as_posix()
                    if rel_path not in artifacts:
                        try:
                            artifacts[rel_path] = base64.b64encode(file_path.read_bytes()).decode(
                                "ascii"
                            )
                        except Exception as e:
                            logger.warning("Could not bundle artifact", path=rel_path, error=str(e))

        for pattern in ["*.json", "*.yaml", "*.yml", "*.md", "*.txt"]:
            for file_path in task_dir.glob(pattern):
                if file_path.is_file():
                    rel_path = file_path.relative_to(task_dir).as_posix()
                    if rel_path not in artifacts:
                        try:
                            artifacts[rel_path] = base64.b64encode(file_path.read_bytes()).decode(
                                "ascii"
                            )
                        except Exception as e:
                            logger.warning("Could not bundle artifact", path=rel_path, error=str(e))

        logger.info("Bundled task artifacts", count=len(artifacts), task_dir=str(task_dir))
        return artifacts
