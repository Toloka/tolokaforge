"""Adapter for running converted/frozen tlk_mcp_core task packs.

Loads tools from bundled ``_domain/`` directory instead of live mcp-tools-library.
Handles database creation, tool wrapping, and stable hash grading.

The frozen adapter reads the output produced by
``tolokaforge adapter convert --name tlk_mcp_core`` — a self-contained directory
that includes:

* ``_domain/mcp_core/``           — InMemoryDatabase + validation libraries
* ``_domain/tools/mcp_tools_library/`` — frozen tool implementations
* ``_domain/tool_registry.json``  — mapping tool_name → source module
* ``{task_id}/initial_state.json`` — merged DB state for each task
* ``{task_id}/fixtures/golden_actions.json`` — golden path for grading

.. important::

   ``mcp_core`` is **not** imported at module level.  It becomes available
   only after :py:meth:`_setup_sys_path` injects the ``_domain/`` tree into
   :data:`sys.path`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    GradingCombineConfig,
    GradingConfig,
    StateChecksConfig,
    TaskConfig,
    Trajectory,
)

if TYPE_CHECKING:
    from tolokaforge.tools.registry import Tool

logger = get_logger(__name__)


class FrozenMcpCoreAdapter(BaseAdapter):
    """Adapter for frozen/converted tlk_mcp_core tasks.

    Reads task configuration + ``adapter_settings`` to find:

    - ``_domain/mcp_core/``                — the InMemoryDatabase + validation
    - ``_domain/tools/mcp_tools_library/`` — frozen tool implementations
    - ``_domain/tool_registry.json``       — mapping *tool_name* → source module
    - ``initial_state.json``               — merged DB state for this task
    - ``fixtures/golden_actions.json``     — golden path for grading
    """

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self._task_configs: dict[str, TaskConfig] = {}
        self._task_files: dict[str, Path] = {}
        self._db_instances: dict[str, Any] = {}
        self._tool_instances: dict[str, Any] = {}
        self._tool_registry: dict[str, dict] = {}
        self._domain_dir: Path | None = None
        self._sys_paths_added = False

    # ------------------------------------------------------------------
    # Domain / sys.path helpers
    # ------------------------------------------------------------------

    def _resolve_domain_dir(self, task_dir: Path, adapter_settings: dict) -> Path:
        """Resolve ``_domain/`` directory from *adapter_settings*."""
        domain_dir = adapter_settings.get("domain_dir", "../_domain")
        resolved = (task_dir / domain_dir).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Domain directory not found: {resolved}")
        return resolved

    def _setup_sys_path(self, domain_dir: Path) -> None:
        """Add mcp_core and tools to :data:`sys.path`.

        The ``_domain/`` directory contains ``mcp_core/`` as a Python package
        (with ``__init__.py``), so we add ``_domain/`` itself to ``sys.path``
        so that ``import mcp_core`` resolves correctly.  Tool packages live
        under ``_domain/tools/mcp_tools_library/``, so ``_domain/tools/`` is
        added for ``import mcp_tools_library``.
        """
        if self._sys_paths_added:
            return

        # domain_dir contains mcp_core/ as a package → add domain_dir itself
        mcp_core_parent = domain_dir
        tools_src = domain_dir / "tools"

        for path in [mcp_core_parent, tools_src]:
            path_str = str(path)
            if path.exists() and path_str not in sys.path:
                sys.path.insert(0, path_str)
                logger.debug("Added to sys.path", path=path_str)

        self._sys_paths_added = True

    # ------------------------------------------------------------------
    # Tool loading
    # ------------------------------------------------------------------

    def _load_tool_registry(self, domain_dir: Path) -> dict[str, dict]:
        """Load ``tool_registry.json`` from ``_domain/``."""
        registry_path = domain_dir / "tool_registry.json"
        if not registry_path.exists():
            raise FileNotFoundError(f"tool_registry.json not found at {registry_path}")

        with open(registry_path) as f:
            return json.load(f)

    def _load_tools(self, domain_dir: Path) -> dict[str, Any]:
        """Import and instantiate tool classes from frozen modules."""
        self._setup_sys_path(domain_dir)
        registry = self._load_tool_registry(domain_dir)

        tool_instances: dict[str, Any] = {}
        for tool_name, source in registry.items():
            try:
                toolset = source["toolset"]
                module_path = source["module_path"]
                class_name = source["class_name"]

                full_module = f"mcp_tools_library.{toolset}.{module_path}"
                module = importlib.import_module(full_module)
                tool_cls = getattr(module, class_name)
                tool_instances[tool_name] = tool_cls()
                logger.debug("Loaded frozen tool", name=tool_name, module=full_module)
            except Exception as e:
                logger.error("Failed to load frozen tool", name=tool_name, error=str(e))
                raise

        return tool_instances

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _create_database(self, task_dir: Path, domain_dir: Path) -> Any:
        """Create :class:`InMemoryDatabase` from ``initial_state.json``."""
        self._setup_sys_path(domain_dir)

        from mcp_core.db.database import InMemoryDatabase  # noqa: WPS433 (late import)

        # Load initial state
        initial_state_path = task_dir / "initial_state.json"
        if not initial_state_path.exists():
            raise FileNotFoundError(f"initial_state.json not found at {initial_state_path}")

        with open(initial_state_path) as f:
            state_dict = json.load(f)

        # Read domain from domain_manifest.yaml
        domain = self._read_domain_from_manifest(domain_dir)

        # Build additional_sources for model discovery
        tools_src = domain_dir / "tools" / "mcp_tools_library"
        additional_sources = self._discover_model_sources(tools_src)

        # Create database
        db = InMemoryDatabase(domain=domain, additional_sources=additional_sources)

        # Populate with initial state.
        # Use upsert logic: try create first, fall back to update if the record
        # already exists (loaded by InMemoryDatabase from initial_data).
        for table_name, records in state_dict.items():
            model_cls = self._resolve_model_class(db, table_name, domain)
            if model_cls:
                for record in records:
                    try:
                        instance = model_cls(**record)
                        try:
                            db.create(instance)
                        except (ValueError, Exception) as create_err:
                            if "already exists" in str(create_err):
                                try:
                                    db.update(instance)
                                except Exception as update_err:
                                    logger.debug(
                                        "Failed to update existing record",
                                        table=table_name,
                                        error=str(update_err),
                                    )
                            else:
                                logger.debug(
                                    "Failed to create record",
                                    table=table_name,
                                    error=str(create_err),
                                )
                    except Exception as e:
                        logger.debug(
                            "Failed to instantiate record",
                            table=table_name,
                            error=str(e),
                        )
            else:
                logger.debug("Unknown table in initial_state", table=table_name)

        return db

    @staticmethod
    def _resolve_model_class(db: Any, table_name: str, domain: str) -> Any:
        """Try several matching strategies to find the Pydantic model for *table_name*."""
        # Direct match
        model_cls = db._stem_to_model_cls.get(table_name)
        if model_cls:
            return model_cls

        # Try removing "_models_" segment
        if "_models_" in table_name:
            alt_name = table_name.replace("_models_", "_")
            model_cls = db._stem_to_model_cls.get(alt_name)
            if model_cls:
                return model_cls

        # Try with domain prefix
        if domain:
            prefixed = f"{domain}_{table_name}"
            model_cls = db._stem_to_model_cls.get(prefixed)
            if model_cls:
                return model_cls

        # Suffix matching
        for stem in db._stem_to_model_cls:
            if stem.endswith(f"_{table_name}") or stem == table_name:
                return db._stem_to_model_cls[stem]

        # Reverse suffix matching: the data_patch / initial_state key
        # (possibly with ``_models_`` collapsed) may end with the DB stem.
        # E.g. ``external_retail_toolset_oms_models_orders`` → collapse →
        # ``external_retail_toolset_oms_orders`` which endswith ``_oms_orders``.
        candidates = [table_name]
        if "_models_" in table_name:
            candidates.append(table_name.replace("_models_", "_"))
        for candidate in candidates:
            for stem in db._stem_to_model_cls:
                if candidate.endswith(f"_{stem}") or candidate == stem:
                    return db._stem_to_model_cls[stem]

        return None

    def _read_domain_from_manifest(self, domain_dir: Path) -> str:
        """Read domain name from ``domain_manifest.yaml``."""
        manifest_path = domain_dir / "domain_manifest.yaml"
        domain = "default"
        if manifest_path.exists():
            import yaml

            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}
            domain = manifest.get("domain", "default")
        return domain

    def _discover_model_sources(self, tools_src: Path) -> dict[str, tuple]:
        """Discover model sources from frozen tools directory."""
        additional_sources: dict[str, tuple] = {}
        if not tools_src.exists():
            return additional_sources

        def scan(path: Path, depth: int = 0) -> None:
            if depth > 5 or not path.is_dir():
                return
            for item in path.iterdir():
                if not item.is_dir() or item.name.startswith(("_", ".")):
                    continue
                models_dir = item / "models"
                models_file = item / "models.py"
                initial_data_dir = item / "initial_data"

                has_models = (models_file.exists() and models_file.is_file()) or (
                    models_dir.exists() and models_dir.is_dir()
                )
                if has_models:
                    rel_path = item.relative_to(tools_src)
                    namespace = "_".join(rel_path.parts)
                    module_prefix = "mcp_tools_library." + ".".join(rel_path.parts) + ".models"
                    if initial_data_dir.exists():
                        additional_sources[namespace] = (
                            str(initial_data_dir),
                            module_prefix,
                        )
                scan(item, depth + 1)

        scan(tools_src)
        return additional_sources

    # =====================================================================
    # BaseAdapter interface — task discovery
    # =====================================================================

    def _discover_tasks(self) -> None:
        """Discover tasks matching glob pattern (same as NativeAdapter)."""
        if self._task_files:
            return  # Already discovered

        import glob as glob_module

        import yaml

        tasks_glob = self.params.get("tasks_glob", "")

        if not tasks_glob:
            return

        pattern = str(self.base_dir / tasks_glob)
        for task_file in glob_module.glob(pattern, recursive=True):
            task_path = Path(task_file)
            try:
                with open(task_path) as f:
                    task_data = yaml.safe_load(f)
            except Exception:
                logger.warning("Invalid task file; skipping", path=str(task_path))
                continue

            if not isinstance(task_data, dict):
                continue

            task_id = task_data.get("task_id")
            if not task_id:
                continue

            if task_id not in self._task_files:
                self._task_files[task_id] = task_path

        logger.info("Discovered frozen tasks", count=len(self._task_files))

    def get_task_ids(self) -> list[str]:
        """Return task IDs from discovery or pre-loaded configs."""
        self._discover_tasks()
        return (
            list(self._task_files.keys()) if self._task_files else list(self._task_configs.keys())
        )

    def get_task(self, task_id: str) -> TaskConfig:
        """Return task config, loading from file if not pre-loaded."""
        if task_id in self._task_configs:
            return self._task_configs[task_id]

        self._discover_tasks()
        if self._task_files and task_id in self._task_files:
            import yaml

            with open(self._task_files[task_id]) as f:
                task_data = yaml.safe_load(f)
            task_config = TaskConfig(**task_data)
            self._task_configs[task_id] = task_config
            return task_config

        raise ValueError(f"Task {task_id} not found")

    def get_task_dir(self, task_id: str) -> Path:
        """Return task directory from discovered file or fallback to ``base_dir / task_id``."""
        self._discover_tasks()
        if self._task_files and task_id in self._task_files:
            return self._task_files[task_id].parent
        return self.base_dir / task_id

    # =====================================================================
    # BaseAdapter interface — environment
    # =====================================================================

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        """Create environment with database and tools."""
        task_dir = self.get_task_dir(task_id)
        task_config = self.get_task(task_id)
        adapter_settings = task_config.adapter_settings or {}

        domain_dir = self._resolve_domain_dir(task_dir, adapter_settings)
        self._domain_dir = domain_dir

        # Load tools (cached across tasks sharing the same _domain/)
        if not self._tool_instances:
            self._tool_instances = self._load_tools(domain_dir)
            self._tool_registry = self._load_tool_registry(domain_dir)

        # Create database (per-task)
        db = self._create_database(task_dir, domain_dir)
        self._db_instances[task_id] = db

        # Initialize TypeSense for knowledge base search
        self._init_typesense(task_id, domain_dir)

        # Load system prompt
        system_prompt = self._load_system_prompt(task_dir, adapter_settings)

        db_state = db.to_state_dict() if hasattr(db, "to_state_dict") else {}

        return AdapterEnvironment(
            data=db_state,
            tools=list(self._tool_instances.values()),
            wiki=system_prompt,
            rules=[],
            task_dir=task_dir,
        )

    # ------------------------------------------------------------------
    # TypeSense initialization
    # ------------------------------------------------------------------

    def _init_typesense(self, task_id: str, domain_dir: Path) -> None:
        """Initialize TypeSense for knowledge base search.

        Loads documents from the frozen ``_domain/docindex/`` directory and
        indexes them in TypeSense for semantic search via search_policy tools.
        Gracefully degrades if TypeSense is not available, disabled, or if
        the docindex directory is absent.
        """
        try:
            from tolokaforge.core.search.typesense_provider import create_typesense_provider
        except ImportError:
            logger.warning("TypeSense provider not available — search_policy will return empty")
            return

        domain_name = self._read_domain_from_manifest(domain_dir)

        # Get TypeSense settings from adapter params (passed by orchestrator)
        ts_config = self.params.get("typesense") or {}
        enabled = ts_config.get("enabled", True)
        mode = ts_config.get("mode", "local")

        if not enabled or mode == "disabled":
            logger.info("TypeSense is disabled — search_policy will return empty results")
            return

        host = ts_config.get("host", "127.0.0.1")
        port = ts_config.get("port", 8108)
        api_key = ts_config.get("api_key")
        timeout = ts_config.get("timeout", 30.0)

        # In local mode, the orchestrator started TypeSense on localhost.
        # The config 'host' may be a Docker DNS name ("typesense") intended for
        # runners inside Docker, but the orchestrator runs on the host where
        # Docker DNS doesn't resolve. Always use 127.0.0.1 for host-side init.
        if mode == "local":
            host = "127.0.0.1"

        if port == "auto":
            port = 8108

        # Locate docindex directory inside the frozen _domain/
        docindex_path = domain_dir / "docindex"
        if not docindex_path.is_dir():
            logger.debug(
                "No docindex directory in _domain/ — TypeSense init skipped",
                domain_dir=str(domain_dir),
            )
            return

        provider = create_typesense_provider(
            enabled=enabled,
            host=host,
            port=int(port),
            api_key=api_key,
            timeout=timeout,
            use_stub=False,
        )

        try:
            success = provider.ensure_domain_initialized(
                domain=domain_name,
                docindex_path=docindex_path,
                timeout=timeout,
            )
            if success:
                logger.info(
                    f"TypeSense initialized for domain '{domain_name}'",
                    host=host,
                    port=port,
                )
            else:
                logger.warning(
                    f"TypeSense initialization returned False for domain '{domain_name}' "
                    "— search_policy will return empty results"
                )
        except TimeoutError:
            logger.warning(
                f"TypeSense initialization timed out for domain '{domain_name}' "
                f"after {timeout}s — search_policy will return empty results"
            )
        except RuntimeError as e:
            logger.warning(
                f"TypeSense initialization failed for domain '{domain_name}': {e} "
                "— search_policy will return empty results"
            )

    def get_tools(self, task_id: str) -> list[Any]:
        """Return raw tool instances."""
        return list(self._tool_instances.values())

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Tool]:
        """Wrap frozen mcp_core tools as :class:`Tool` instances for the registry."""

        db = self._db_instances.get(task_id)
        if not db:
            logger.error("No database instance for task", task_id=task_id)
            return []

        tools: list[Tool] = []
        for tool_name, tool_instance in self._tool_instances.items():
            try:
                wrapper = self._create_tool_wrapper(tool_name, tool_instance, db)
                tools.append(wrapper)
            except Exception as e:
                logger.error("Failed to wrap tool", tool=tool_name, error=str(e))

        return tools

    @staticmethod
    def _create_tool_wrapper(name: str, tool_instance: Any, db: Any) -> Tool:
        """Create a :class:`Tool` wrapper around an mcp_core tool instance."""
        from tolokaforge.tools.registry import Tool, ToolResult

        class FrozenToolWrapper(Tool):
            """Sync wrapper around an async mcp_core frozen tool."""

            def __init__(self, tool_name: str, tb_tool: Any, database: Any):
                super().__init__(tool_name, tb_tool.description)
                self.tb_tool = tb_tool
                self.database = database
                self._alias_map = self._build_alias_map()

            def _build_alias_map(self) -> dict[str, str]:
                """Build mapping from sanitised names back to original aliases."""
                alias_map: dict[str, str] = {}
                if hasattr(self.tb_tool, "request_model"):
                    model = self.tb_tool.request_model
                    for field_name, field_info in model.model_fields.items():
                        alias = None
                        if hasattr(field_info, "alias") and field_info.alias:
                            alias = field_info.alias
                        elif (
                            hasattr(field_info, "serialization_alias")
                            and field_info.serialization_alias
                        ):
                            alias = field_info.serialization_alias
                        if alias:
                            sanitised = _sanitize_property_name(alias)
                            alias_map[sanitised] = field_name
                return alias_map

            def get_schema(self) -> dict[str, Any]:
                input_schema = self.tb_tool.input_schema
                sanitised_schema = _sanitize_schema_properties(input_schema)
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": sanitised_schema,
                    },
                }

            def execute(self, **kwargs: Any) -> ToolResult:
                try:
                    mapped_kwargs: dict[str, Any] = {}
                    for key, value in kwargs.items():
                        if key in self._alias_map:
                            mapped_kwargs[self._alias_map[key]] = value
                        else:
                            mapped_kwargs[key] = value

                    # Normalize OData filter double-quotes → single-quotes.
                    # LLMs often generate email eq "x" instead of email eq 'x'.
                    for fkey in ("filter", "$filter"):
                        if fkey in mapped_kwargs and isinstance(mapped_kwargs[fkey], str):
                            mapped_kwargs[fkey] = re.sub(r'"([^"]*)"', r"'\1'", mapped_kwargs[fkey])

                    result = asyncio.run(
                        self.tb_tool.run_with_validation(self.database, mapped_kwargs)
                    )
                    return ToolResult(
                        success=True,
                        output=json.dumps(result, default=str),
                    )
                except Exception as e:
                    return ToolResult(success=False, output="", error=str(e))

        return FrozenToolWrapper(name, tool_instance, db)

    # =====================================================================
    # BaseAdapter interface — system prompt / grading
    # =====================================================================

    def get_system_prompt(self, task_id: str) -> str:
        """Return system prompt from ``_domain/``."""
        task_config = self.get_task(task_id)
        adapter_settings = task_config.adapter_settings or {}
        task_dir = self.get_task_dir(task_id)
        return self._load_system_prompt(task_dir, adapter_settings)

    def get_grading_config(self, task_id: str) -> GradingConfig:
        """Return stable-hash grading config."""
        return GradingConfig(
            combine=GradingCombineConfig(
                method="weighted",
                weights={"state_checks": 1.0},
                pass_threshold=1.0,
            ),
            state_checks=StateChecksConfig(
                hash={"enabled": True, "weight": 1.0},
            ),
        )

    def grade(
        self,
        task_id: str,
        trajectory: Trajectory,
        final_state: dict[str, Any],
        env: AdapterEnvironment,
    ) -> Grade:
        """Grade using stable hash comparison."""
        from tolokaforge.core.grading.fuzzy_compare import get_stable_state
        from tolokaforge.core.utils.diff import calculate_state_diff

        # Compute expected state by replaying golden actions
        expected_stable = self._compute_expected_state(task_id)
        if expected_stable is None:
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Failed to compute expected state",
            )

        expected_hash = compute_stable_hash(expected_stable)

        # Get actual state from tracked DB
        db = self._db_instances.get(task_id)
        if not db:
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="No database instance for grading",
            )

        actual_stable = get_stable_state(db)
        actual_hash = compute_stable_hash(actual_stable)

        if actual_hash == expected_hash:
            return Grade(
                binary_pass=True,
                score=1.0,
                components=GradeComponents(state_checks=1.0),
                reasons=f"State: stable hash matches ({expected_hash[:16]}...)",
            )
        else:
            state_diff = calculate_state_diff(expected_stable, actual_stable)
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=(
                    f"State: stable hash mismatch "
                    f"(expected {expected_hash[:16]}..., got {actual_hash[:16]}...)"
                ),
                state_diff=state_diff,
            )

    def reset_environment(self, env: AdapterEnvironment) -> None:
        """No-op — ``create_environment`` is called per trial."""

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        """Compute expected stable hash by replaying golden actions on fresh state."""
        expected_stable = self._compute_expected_state(task_id)
        if expected_stable is None:
            return None
        return compute_stable_hash(expected_stable)

    def to_task_description(self, task_id: str) -> Any:
        """Convert frozen tlk_mcp_core task to a TaskDescription for Docker Runner.

        Extracts:
        - Tools from the frozen tool registry (``_domain/tool_registry.json``)
        - Initial state from ``initial_state.json``
        - Golden actions from ``fixtures/golden_actions.json``
        - Unstable fields from ``fixtures/unstable_fields.json``
        - Grading config (hash-based by default, or from ``grading.yaml``)
        - System prompt from adapter_settings

        Args:
            task_id: Task identifier

        Returns:
            TaskDescription ready for Docker Runner

        Raises:
            ValueError: If task_id not found
            RuntimeError: If required files cannot be loaded
        """
        from datetime import datetime, timezone

        import yaml as _yaml

        from tolokaforge.runner.models import (
            AdapterType,
            GoldenAction,
            InvocationStyle,
            SearchConfig,
            StateChecksConfig,
            TaskDescription,
            ToolSchema,
            ToolSource,
            TranscriptRulesConfig,
            UnstableFieldSpec,
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

        logger.info("Building TaskDescription", task_id=task_id, adapter_type="tlk_mcp_core")

        # Ensure tasks are discovered
        self._discover_tasks()

        task = self.get_task(task_id)
        task_dir = self.get_task_dir(task_id)
        adapter_settings = task.adapter_settings or {}
        domain_dir = self._resolve_domain_dir(task_dir, adapter_settings)

        # ---- System prompt ----
        system_prompt = self._load_system_prompt(task_dir, adapter_settings)

        # ---- Tool schemas from frozen registry + fixtures/tools.json ----
        tool_registry = self._load_tool_registry(domain_dir)

        # Load rich tool schemas from fixtures/tools.json (has descriptions + parameters)
        tools_json_path = task_dir / "fixtures" / "tools.json"
        rich_schemas: dict[str, dict[str, Any]] = {}
        if tools_json_path.exists():
            with open(tools_json_path) as f:
                tools_list = json.load(f)
            for tool_def in tools_list:
                if isinstance(tool_def, dict) and "name" in tool_def:
                    rich_schemas[tool_def["name"]] = tool_def

        agent_tools: list[ToolSchema] = []
        for tool_name, source in tool_registry.items():
            toolset = source.get("toolset", "")
            module_path = source.get("module_path", "")
            class_name = source.get("class_name", tool_name)
            inv_style_raw = source.get("invocation_style", "mcp_async")
            try:
                inv_style = InvocationStyle(inv_style_raw)
            except ValueError:
                inv_style = InvocationStyle.MCP_ASYNC

            # Use rich schema from fixtures/tools.json if available
            rich = rich_schemas.get(tool_name, {})
            description = rich.get("description", f"Frozen tool: {tool_name}")
            parameters = rich.get("parameters", {"type": "object", "properties": {}})

            tool_schema = ToolSchema(
                name=tool_name,
                description=description,
                parameters=parameters,
                category="compute",
                timeout_s=30.0,
                source=ToolSource(
                    toolset=toolset,
                    module_path=module_path,
                    class_name=class_name,
                    invocation_style=inv_style,
                ),
            )
            agent_tools.append(tool_schema)

        # ---- Initial state from initial_state.json ----
        initial_tables: dict[str, list[dict[str, Any]]] = {}
        initial_state_path = task_dir / "initial_state.json"
        if initial_state_path.exists():
            with open(initial_state_path) as f:
                state_dict = json.load(f)
            for collection_name, collection_data in state_dict.items():
                if isinstance(collection_data, list):
                    records = collection_data
                elif isinstance(collection_data, dict):
                    values = list(collection_data.values())
                    if values and all(isinstance(v, dict) for v in values):
                        records = values
                    else:
                        records = [collection_data]
                else:
                    records = [collection_data]
                initial_tables[collection_name] = records

        # ---- Unstable fields from fixtures/unstable_fields.json ----
        unstable_fields: list[UnstableFieldSpec] = []
        unstable_fields_path = task_dir / "fixtures" / "unstable_fields.json"
        if unstable_fields_path.exists():
            with open(unstable_fields_path) as f:
                uf_data = json.load(f)
            if isinstance(uf_data, list):
                for entry in uf_data:
                    if isinstance(entry, dict) and "table_name" in entry and "field_name" in entry:
                        unstable_fields.append(
                            UnstableFieldSpec(
                                table_name=entry["table_name"],
                                field_name=entry["field_name"],
                                reason=entry.get("reason", "auto_id"),
                            )
                        )

        # ---- Golden actions from fixtures/golden_actions.json ----
        golden_actions: list[GoldenAction] = []
        golden_path = task_dir / "fixtures" / "golden_actions.json"
        if golden_path.exists():
            with open(golden_path) as f:
                ga_data = json.load(f)
            for action in ga_data:
                golden_actions.append(
                    GoldenAction(
                        tool_name=action.get("tool_name", ""),
                        arguments=action.get("arguments", {}),
                    )
                )

        # ---- Grading config ----
        state_checks: StateChecksConfig | None = None
        transcript_rules: TranscriptRulesConfig | None = None

        # Try loading grading.yaml if referenced in task config
        grading_data: dict[str, Any] | None = None
        if task.grading:
            grading_path = task_dir / task.grading
            if grading_path.exists():
                with open(grading_path) as f:
                    grading_data = _yaml.safe_load(f)

        if grading_data:
            sc_data = grading_data.get("state_checks", {})
            if sc_data:
                hash_cfg = sc_data.get("hash", {})
                state_checks = StateChecksConfig(
                    hash_enabled=bool(hash_cfg and hash_cfg.get("enabled", False)),
                    expected_hash=hash_cfg.get("expected_state_hash") if hash_cfg else None,
                    golden_actions=golden_actions,
                    jsonpath_checks=sc_data.get("jsonpaths", []),
                )
            tr_data = grading_data.get("transcript_rules", {})
            if tr_data:
                transcript_rules = TranscriptRulesConfig(
                    must_contain=tr_data.get("must_contain", []),
                    disallow_regex=tr_data.get("disallow_regex", []),
                    max_turns=tr_data.get("max_turns"),
                    communicate_info=tr_data.get("communicate_info", []),
                )
        else:
            # Default: hash-based grading (the frozen adapter standard)
            state_checks = StateChecksConfig(
                hash_enabled=True,
                golden_actions=golden_actions,
            )

        combine_data = grading_data.get("combine", {}) if grading_data else {}
        grading_config = RunnerGradingConfig(
            combine_method=combine_data.get("method", "weighted"),
            weights=combine_data.get("weights", {"state_checks": 1.0}),
            pass_threshold=combine_data.get("pass_threshold", 1.0),
            state_checks=state_checks,
            transcript_rules=transcript_rules,
        )

        # ---- User simulator ----
        user_simulator = RunnerUserSimulatorConfig(
            mode=task.user_simulator.mode if task.user_simulator else "llm",
            persona=task.user_simulator.persona if task.user_simulator else "cooperative",
            backstory=(
                task.user_simulator.backstory
                if task.user_simulator and task.user_simulator.backstory
                else ""
            ),
        )

        # ---- Initial state config ----
        initial_state = RunnerInitialStateConfig(
            tables=initial_tables,
            schemas=[],
            unstable_fields=unstable_fields,
        )

        # ---- Search config ----
        domain_name = self._read_domain_from_manifest(domain_dir)
        docindex_path = domain_dir / "docindex"
        ts_config = self.params.get("typesense") or {}
        search_config = SearchConfig(
            enabled=docindex_path.is_dir(),
            domain_name=domain_name if docindex_path.is_dir() else None,
            documents_path=str(docindex_path) if docindex_path.is_dir() else None,
            host=ts_config.get("host") if docindex_path.is_dir() else None,
            port=(
                int(ts_config["port"]) if docindex_path.is_dir() and ts_config.get("port") else None
            ),
            api_key=ts_config.get("api_key") if docindex_path.is_dir() else None,
        )

        # ---- Source files for debugging ----
        source_files: dict[str, str] = {}
        if task_id in self._task_files:
            source_files["task"] = str(self._task_files[task_id])
        if task.grading:
            source_files["grading"] = str(task_dir / task.grading)
        if initial_state_path.exists():
            source_files["initial_state"] = str(initial_state_path)
        if golden_path.exists():
            source_files["golden_actions"] = str(golden_path)

        # ---- Bundle domain artifacts for Docker execution ----
        tool_artifacts = self._bundle_domain_artifacts(domain_dir)

        # ---- Construct TaskDescription ----
        task_description = TaskDescription(
            task_id=task_id,
            name=task.name or task_id,
            category=task.category or "tlk_mcp_core",
            description=task.description or "",
            adapter_type=AdapterType.TLK_MCP_CORE,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
            user_tools=[],
            initial_state=initial_state,
            initialization_actions=[],
            user_simulator=user_simulator,
            search=search_config,
            grading=grading_config,
            source_files=source_files,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "domain_dir": str(domain_dir),
                "domain_name": domain_name,
            },
            tool_artifacts=tool_artifacts,
        )

        logger.info(
            "Built TaskDescription",
            task_id=task_id,
            agent_tools_count=len(agent_tools),
            tables_count=len(initial_tables),
            golden_actions_count=len(golden_actions),
            unstable_fields_count=len(unstable_fields),
        )

        return task_description

    # ------------------------------------------------------------------
    # Domain artifact bundling (for Docker execution)
    # ------------------------------------------------------------------

    def _bundle_domain_artifacts(self, domain_dir: Path) -> dict[str, str]:
        """Bundle all Python files from _domain/ as base64-encoded artifacts.

        These artifacts are included in TaskDescription.tool_artifacts so the
        Docker Runner can extract them without needing host filesystem access.
        """
        import base64

        artifacts: dict[str, str] = {}
        for file_path in domain_dir.rglob("*.py"):
            rel_path = file_path.relative_to(domain_dir).as_posix()
            try:
                content = file_path.read_bytes()
                artifacts[rel_path] = base64.b64encode(content).decode("ascii")
            except Exception as e:
                logger.warning("Could not bundle artifact", path=rel_path, error=str(e))

        # Also bundle data files needed by tools (e.g., tool_registry.json)
        for pattern in ["*.json", "*.txt", "*.yaml", "*.yml", "*.md"]:
            for file_path in domain_dir.rglob(pattern):
                rel_path = file_path.relative_to(domain_dir).as_posix()
                if rel_path not in artifacts:
                    try:
                        content = file_path.read_bytes()
                        artifacts[rel_path] = base64.b64encode(content).decode("ascii")
                    except Exception as e:
                        logger.warning("Could not bundle artifact", path=rel_path, error=str(e))

        logger.info("Bundled artifacts", count=len(artifacts), domain_dir=str(domain_dir))
        return artifacts

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_system_prompt(self, task_dir: Path, adapter_settings: dict) -> str:
        """Load system prompt from the path specified in *adapter_settings*."""
        sp_path = adapter_settings.get("system_prompt")
        if sp_path:
            sp_file = (task_dir / sp_path).resolve()
            if sp_file.exists():
                return sp_file.read_text()
        return ""

    def _compute_expected_state(self, task_id: str) -> dict[str, Any] | None:
        """Replay golden actions on a fresh DB to compute expected state."""
        from tolokaforge.core.grading.fuzzy_compare import get_stable_state

        task_dir = self.get_task_dir(task_id)
        task_config = self.get_task(task_id)
        adapter_settings = task_config.adapter_settings or {}
        domain_dir = self._resolve_domain_dir(task_dir, adapter_settings)

        # Load golden actions
        golden_path = task_dir / "fixtures" / "golden_actions.json"
        if not golden_path.exists():
            logger.error("golden_actions.json not found", task_id=task_id)
            return None

        with open(golden_path) as f:
            golden_actions = json.load(f)

        # Create fresh DB
        db = self._create_database(task_dir, domain_dir)

        # Execute golden actions
        for action in golden_actions:
            tool_name = action.get("tool_name", "")
            arguments = action.get("arguments", {})

            tool_instance = self._tool_instances.get(tool_name)
            if tool_instance:
                try:
                    asyncio.run(tool_instance.run_with_validation(db, arguments))
                except Exception as e:
                    logger.debug("Golden action failed", tool=tool_name, error=str(e))
            else:
                # Try without namespace prefix
                for name, inst in self._tool_instances.items():
                    if name.endswith(f"_{tool_name}") or name == tool_name:
                        try:
                            asyncio.run(inst.run_with_validation(db, arguments))
                        except Exception as e:
                            logger.debug(
                                "Golden action failed",
                                tool=tool_name,
                                error=str(e),
                            )
                        break
                else:
                    logger.warning("Tool not found for golden action", tool=tool_name)

        return get_stable_state(db)


# =====================================================================
# Schema sanitisation helpers (shared with TlkMcpCoreAdapter)
# =====================================================================


def _sanitize_property_name(name: str) -> str:
    """Sanitise a property name to match LLM API requirements."""
    sanitised = name.lstrip("$")
    sanitised = re.sub(r"[^a-zA-Z0-9_.-]", "_", sanitised)
    return sanitised


def _sanitize_schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitise property names in a JSON Schema dict."""
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            sanitised_props = {}
            for prop_name, prop_value in value.items():
                sanitised_name = _sanitize_property_name(prop_name)
                sanitised_props[sanitised_name] = _sanitize_schema_properties(prop_value)
            result[key] = sanitised_props
        elif key == "required" and isinstance(value, list):
            result[key] = [_sanitize_property_name(n) for n in value]
        elif isinstance(value, dict):
            result[key] = _sanitize_schema_properties(value)
        elif isinstance(value, list):
            result[key] = [
                _sanitize_schema_properties(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            result[key] = value
    return result
