"""
Tool Factory for Runner

This module provides tool reconstruction from ToolSource definitions.
It creates callable wrappers for four invocation styles:

1. tau_sync - Tau environment tools (synchronous invoke())
2. mcp_async - TlkMcpCore MCP tools (async run_with_validation())
3. mcp_server - Native MCP server tools (subprocess JSON-RPC)
4. rag_search - RAG service search tools (HTTP API)

Each wrapper produces a callable with the same interface:
    async def execute(tool_name: str, arguments: Dict) -> str

Usage:
    factory = ToolFactory(db_client, trial_id)
    tools = factory.reconstruct_tools(tool_schemas)
    result = await tools["book_reservation"]({"user_id": "123", "flight": "AA100"})
"""

import asyncio
import importlib
import json
import logging
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.db_proxy import DBServiceProxy, SyncDBServiceProxy
from tolokaforge.runner.models import (
    InvocationStyle,
)
from tolokaforge.runner.models import (
    ToolSchema as ToolSchemaModel,
)
from tolokaforge.runner.models import (
    ToolSource as ToolSourceModel,
)
from tolokaforge.runner.rag_client import (
    RAGServiceClient,
    RAGServiceError,
    SearchResponse,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Custom Exceptions
# =============================================================================


class ToolReconstructionError(Exception):
    """Error during tool reconstruction - fail fast."""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"Failed to reconstruct tool '{tool_name}': {message}")


class ToolImportError(ToolReconstructionError):
    """Tool module or class could not be imported."""

    pass


class ToolConfigurationError(ToolReconstructionError):
    """Tool configuration is invalid."""

    pass


class ToolExecutionError(Exception):
    """Error during tool execution at runtime (e.g., validation failures).

    Distinct from ToolReconstructionError which is for setup-time failures.
    """

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"{tool_name}: {message}")


# =============================================================================
# Tool Wrapper Base Class
# =============================================================================


class ToolWrapper(ABC):
    """
    Base class for tool wrappers.

    All wrappers must implement the execute() method with the same interface.
    """

    def __init__(self, tool_schema: ToolSchemaModel):
        self.tool_schema = tool_schema
        self.name = tool_schema.name
        self.timeout_s = tool_schema.timeout_s

    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> str:
        """
        Execute the tool with given arguments.

        Args:
            arguments: Tool arguments as a dictionary

        Returns:
            Tool output as a string (JSON serialized if structured)
        """
        pass

    async def __call__(self, arguments: dict[str, Any]) -> str:
        """Allow calling the wrapper directly."""
        return await self.execute(arguments)

    def cleanup(self) -> None:  # noqa: B027
        """Clean up any resources (override in subclasses if needed)."""
        pass


# =============================================================================
# Tau Sync Tool Wrapper
# =============================================================================


class TauSyncToolWrapper(ToolWrapper):
    """
    Wrapper for Tau environment tools.

    Tau tools have a static invoke() method that takes data dict and kwargs.
    The wrapper:
    1. Fetches current state from DB Service
    2. Calls tool.invoke(data, **kwargs)
    3. Detects state changes and pushes mutations back to DB Service

    Note: Tau tools modify state in-place, so we need to diff before/after.
    """

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        tool_class: type,
        db_proxy: SyncDBServiceProxy,
    ):
        super().__init__(tool_schema)
        self.tool_class = tool_class
        self.db_proxy = db_proxy
        self._tool_instance = None

    async def execute(self, arguments: dict[str, Any]) -> str:
        """
        Execute Tau tool synchronously.

        Tau tools expect:
        - data: dict containing the current state
        - **kwargs: tool-specific arguments
        """
        start_time = time.perf_counter()
        logger.debug(f"TauSyncToolWrapper.execute() ENTRY: tool={self.name}, arguments={arguments}")
        state_changed = False
        try:
            # Get current state from DB Service
            state_before = self.db_proxy.to_state_dict()

            # Tau tools expect a 'data' dict with the state
            # The tool modifies this dict in-place
            data = state_before.copy()

            # Call the tool's invoke method
            # Tau tools have: Tool.invoke(data, **kwargs)
            result = self.tool_class.invoke(data, **arguments)

            # Detect state changes by comparing before/after
            # This is a simplified approach - real implementation would
            # need to track which tables/records changed
            state_after = data
            state_changed = state_before != state_after

            # Push mutations back to DB Service
            await self._sync_state_changes(state_before, state_after)

            # Return result as string
            if isinstance(result, str):
                output = result
            elif result is None:
                output = "Success"
            else:
                output = json.dumps(result, default=str)

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"TauSyncToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            return output

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"TauSyncToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=False, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            logger.error(f"Tau tool {self.name} execution failed: {e}")
            raise

    async def _sync_state_changes(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        """
        Detect and sync state changes to DB Service.

        This compares the state before and after tool execution
        and pushes any changes to the DB Service.
        """
        # For each table, detect changes
        all_tables = set(before.keys()) | set(after.keys())

        for table_name in all_tables:
            before_records = before.get(table_name, [])
            after_records = after.get(table_name, [])

            # Skip if no changes
            if before_records == after_records:
                continue

            # Build operations for changes
            operations = []

            # Index records by ID for comparison
            before_by_id = {self._get_record_id(r): r for r in before_records}
            after_by_id = {self._get_record_id(r): r for r in after_records}

            # Find inserts (in after but not in before)
            for record_id, record in after_by_id.items():
                if record_id not in before_by_id:
                    operations.append({"op": "insert", "record": record})

            # Find updates (in both but different)
            for record_id, after_record in after_by_id.items():
                if record_id in before_by_id:
                    before_record = before_by_id[record_id]
                    if before_record != after_record:
                        operations.append({"op": "upsert", "record": after_record, "key": "id"})

            # Find deletes (in before but not in after)
            for record_id in before_by_id:
                if record_id not in after_by_id:
                    operations.append({"op": "delete", "filter": {"id": record_id}})

            # Apply operations to DB Service
            if operations:
                await self.db_proxy._async_proxy.db_client.mutate(
                    trial_id=self.db_proxy.trial_id, table_name=table_name, operations=operations
                )

    def _get_record_id(self, record: dict[str, Any]) -> Any:
        """Get the ID of a record."""
        return record.get("id") or record.get("_id")


# =============================================================================
# MCP Async Tool Wrapper
# =============================================================================


# OData filter parameter names that may contain double-quoted string literals.
_ODATA_FILTER_KEYS = frozenset({"filter", "$filter"})


def _normalize_odata_filter_quotes(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize double-quoted string literals to single-quoted in OData filter arguments.

    OData spec requires single quotes for string literals, but LLMs frequently
    generate double quotes (e.g. ``email eq "foo@bar.com"`` instead of
    ``email eq 'foo@bar.com'``).  This pre-processes the arguments dict so the
    OData lexer can parse the filter correctly.

    Only string values for keys in :data:`_ODATA_FILTER_KEYS` are modified;
    all other arguments are passed through unchanged.
    """
    for key in _ODATA_FILTER_KEYS:
        if key in arguments and isinstance(arguments[key], str):
            arguments[key] = re.sub(r'"([^"]*)"', r"'\1'", arguments[key])
    return arguments


class MCPAsyncToolWrapper(ToolWrapper):
    """
    Wrapper for TlkMcpCore MCP tools.

    MCP tools have an async run_with_validation(db, kwargs) method.
    The wrapper provides a SyncDBServiceProxy that looks like InMemoryDatabase
    but talks to the DB Service.

    IMPORTANT: MCP tools call db methods (get_all, create, update, delete)
    SYNCHRONOUSLY inside their async run() method. Therefore, we must pass
    a SyncDBServiceProxy, not the async DBServiceProxy.
    """

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        tool_class: type,
        db_proxy: SyncDBServiceProxy,
    ):
        super().__init__(tool_schema)
        self.tool_class = tool_class
        self.db_proxy = db_proxy
        self._tool_instance = None

    def _get_tool_instance(self):
        """Get or create the tool instance."""
        if self._tool_instance is None:
            self._tool_instance = self.tool_class()
        return self._tool_instance

    async def execute(self, arguments: dict[str, Any]) -> str:
        """
        Execute MCP async tool.

        MCP tools expect:
        - db: InMemoryDatabase-like object with SYNC methods
        - arguments: dict of tool arguments

        Note: MCP tools call db.get_all(), db.create(), etc. synchronously
        inside their async run() method. The SyncDBServiceProxy handles
        this by running async HTTP calls in a thread pool when called
        from an async context.
        """
        start_time = time.perf_counter()
        logger.debug(
            f"MCPAsyncToolWrapper.execute() ENTRY: tool={self.name}, arguments={arguments}"
        )
        state_changed = False
        try:
            tool = self._get_tool_instance()

            # Normalize OData filter quotes (double → single) before validation.
            # LLMs often generate email eq "x" instead of email eq 'x'.
            arguments = _normalize_odata_filter_quotes(arguments)

            # Call run_with_validation which handles input validation
            # and returns a JSON-serializable dict
            # Note: db_proxy is SyncDBServiceProxy - MCP tools call its
            # methods synchronously inside their async run() method
            result = await tool.run_with_validation(self.db_proxy, arguments)

            # MCP tools may modify state - check proxy for mutations
            # Note: state_changed detection is best-effort for MCP tools
            state_changed = getattr(self.db_proxy, "_mutations_applied", False)

            # Return result as JSON string
            if isinstance(result, str):
                output = result
            elif isinstance(result, dict):
                output = json.dumps(result, default=str)
            else:
                output = str(result)

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"MCPAsyncToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            return output

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"MCPAsyncToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=False, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            logger.error(f"MCP async tool {self.name} execution failed: {e}")
            raise


# =============================================================================
# MCP Server Tool Wrapper
# =============================================================================


class MCPServerProcess(BaseModel):
    """Manages an MCP server subprocess."""

    script_path: str
    process: Any | None = None  # subprocess.Popen - can't type properly
    request_id: int = 0

    model_config = {"arbitrary_types_allowed": True}

    def start(self) -> None:
        """Start the MCP server subprocess and perform MCP protocol handshake.

        MCP requires an initialize / notifications/initialized exchange before
        any tool calls can be made.  Skipping the handshake causes the server
        to reject every subsequent request with JSON-RPC error -32602.
        """
        if self.process is not None:
            return

        self.process = subprocess.Popen(
            [sys.executable, self.script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # MCP initialization handshake
        self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tolokaforge-runner", "version": "1.0"},
            },
        )
        # 'initialized' is a notification — no id, no response expected
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.process.stdin.write(json.dumps(notification) + "\n")
        self.process.stdin.flush()

        logger.info(f"Started MCP server: {self.script_path}")

    def stop(self) -> None:
        """Stop the MCP server subprocess."""
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            logger.info(f"Stopped MCP server: {self.script_path}")

    def send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Send a JSON-RPC request to the MCP server.

        Args:
            method: JSON-RPC method name
            params: Method parameters

        Returns:
            JSON-RPC response result
        """
        if self.process is None:
            raise RuntimeError("MCP server not started")

        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }

        # Send request
        request_line = json.dumps(request) + "\n"
        self.process.stdin.write(request_line)
        self.process.stdin.flush()

        # Read response
        response_line = self.process.stdout.readline()
        if not response_line:
            raise RuntimeError("MCP server closed connection")

        response = json.loads(response_line)

        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"MCP error {error.get('code')}: {error.get('message')}")

        return response.get("result", {})

    def get_state(self) -> dict[str, Any]:
        """Get current _STATE from the MCP subprocess via the internal tool.

        Calls the ``_tolokaforge_get_state_`` tool registered by
        ``DomainToolRegistry._register_internal_tools``.

        Returns:
            Current state dict (table_name -> list[record]).
        """
        result = self.send_request(
            "tools/call",
            {"name": "_tolokaforge_get_state_", "arguments": {}},
        )
        content = result.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "{}")
            return json.loads(text)
        return {}

    def reset_state(self, initial_state: dict[str, Any]) -> None:
        """Replace the MCP subprocess's _STATE with ``initial_state``.

        Calls the ``_tolokaforge_set_state_`` tool registered by
        ``DomainToolRegistry._register_internal_tools``.

        Args:
            initial_state: State dict to restore (table_name -> list[record]).
        """
        self.send_request(
            "tools/call",
            {
                "name": "_tolokaforge_set_state_",
                "arguments": {"state_json": json.dumps(initial_state)},
            },
        )


class MCPServerToolWrapper(ToolWrapper):
    """
    Wrapper for Native MCP server tools.

    MCP server tools run as a subprocess and communicate via stdio JSON-RPC.
    The wrapper manages the server lifecycle and translates tool calls to
    JSON-RPC requests.
    """

    # Shared server processes (one per script)
    _servers: dict[str, MCPServerProcess] = {}

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        server_script: str,
        db_client: DBServiceClient,
        trial_id: str,
    ):
        super().__init__(tool_schema)
        self.server_script = server_script
        self.db_client = db_client
        self.trial_id = trial_id

    def _get_server(self) -> MCPServerProcess:
        """Get or create the MCP server process."""
        if self.server_script not in self._servers:
            server = MCPServerProcess(script_path=self.server_script)
            server.start()
            self._servers[self.server_script] = server
        return self._servers[self.server_script]

    async def execute(self, arguments: dict[str, Any]) -> str:
        """
        Execute MCP server tool via JSON-RPC.

        The tool call is sent to the MCP server subprocess.
        """
        start_time = time.perf_counter()
        logger.debug(
            f"MCPServerToolWrapper.execute() ENTRY: tool={self.name}, arguments={arguments}"
        )
        state_changed = False
        try:
            server = self._get_server()

            # MCP tool call format
            params = {
                "name": self.name,
                "arguments": arguments,
            }

            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: server.send_request("tools/call", params)
            )

            # MCP server tools may modify state - assume true if successful
            state_changed = True

            # Extract content from MCP response
            content = result.get("content", [])
            if content and isinstance(content, list):
                # MCP returns content as list of {type, text} objects
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                output = "\n".join(texts)
            else:
                output = json.dumps(result, default=str)

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"MCPServerToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            return output

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"MCPServerToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=False, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            logger.error(f"MCP server tool {self.name} execution failed: {e}")
            raise

    def get_state(self) -> dict[str, Any]:
        """Return current state from the MCP server subprocess."""
        return self._get_server().get_state()

    def reset_state(self, initial_state: dict[str, Any]) -> None:
        """Reset the MCP server subprocess state to ``initial_state``."""
        self._get_server().reset_state(initial_state)

    def cleanup(self) -> None:
        """Stop the MCP server if this is the last tool using it."""
        # Note: In practice, we'd track usage count and only stop
        # when no tools are using the server
        pass

    @classmethod
    def cleanup_all_servers(cls) -> None:
        """Stop all MCP server processes."""
        for server in cls._servers.values():
            server.stop()
        cls._servers.clear()


# =============================================================================
# Builtin File Tool Wrapper
# =============================================================================


class BuiltinFileToolWrapper(ToolWrapper):
    """Wrapper for builtin filesystem tools (read_file, write_file, list_dir).

    Used when a task declares these tools in ``enabled`` but provides no
    custom ``mcp_server`` script.  The runner container ships the builtin
    implementations directly — no subprocess needed.
    """

    def __init__(self, tool_schema: ToolSchemaModel):
        super().__init__(tool_schema)
        from tolokaforge.tools.builtin.files import ListDirTool, ReadFileTool, WriteFileTool

        if tool_schema.name == "read_file":
            self._tool = ReadFileTool()
        elif tool_schema.name == "write_file":
            self._tool = WriteFileTool()
        elif tool_schema.name == "list_dir":
            self._tool = ListDirTool()
        else:
            raise ToolConfigurationError(
                tool_schema.name,
                f"BuiltinFileToolWrapper does not support tool '{tool_schema.name}'",
            )

    async def execute(self, arguments: dict[str, Any]) -> str:
        result = self._tool.execute(**arguments)
        if result.success:
            return result.output or ""
        return f"Error: {result.error}"


# =============================================================================
# Builtin Generic Tool Wrapper
# =============================================================================


# Lazy factory registry for builtin tools that are NOT file tools and NOT search_kb.
# Each entry maps tool_name → (module_path, class_name).
_BUILTIN_TOOL_FACTORIES: dict[str, tuple[str, str]] = {
    "bash": ("tolokaforge.tools.builtin.bash", "BashTool"),
    "calculator": ("tolokaforge.tools.builtin.calculator", "CalculatorTool"),
    "browser": ("tolokaforge.tools.builtin.browser", "BrowserTool"),
    "http_request": ("tolokaforge.tools.builtin.http_request", "HTTPRequestTool"),
    "mobile": ("tolokaforge.tools.builtin.mobile", "MobileTool"),
    "db_query": ("tolokaforge.tools.builtin.db_json", "DBQueryTool"),
    "db_update": ("tolokaforge.tools.builtin.db_json", "DBUpdateTool"),
}


class BuiltinGenericToolWrapper(ToolWrapper):
    """Wrapper for builtin tools loaded by name from the tool registry.

    Handles tools like browser, bash, calculator, http_request, mobile, etc.
    Instantiates the tool class from ``_BUILTIN_TOOL_FACTORIES`` and
    delegates ``execute()`` to it.
    """

    def __init__(self, tool_schema: ToolSchemaModel):
        super().__init__(tool_schema)
        import importlib

        entry = _BUILTIN_TOOL_FACTORIES.get(tool_schema.name)
        if entry is None:
            raise ToolConfigurationError(
                tool_schema.name,
                f"No builtin factory for tool '{tool_schema.name}'",
            )
        module_path, class_name = entry
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            self._tool = cls()
        except Exception as exc:
            raise ToolConfigurationError(
                tool_schema.name,
                f"Failed to instantiate builtin tool '{tool_schema.name}': {exc}",
            ) from exc

    async def execute(self, arguments: dict[str, Any]) -> str:
        result = self._tool.execute(**arguments)
        if result.success:
            return result.output or ""
        # Raise so the runner service records EXECUTION_STATUS_ERROR,
        # preserving correct tool_success_rate and failure attribution.
        # The runner's exception handler sends the error message back to
        # the LLM, so the agent can still self-correct.
        raise ToolExecutionError(
            self.name,
            result.error or "Tool returned failure with no error message",
        )


# =============================================================================
# RAG Search Tool Wrapper
# =============================================================================


class RAGSearchToolWrapper(ToolWrapper):
    """
    Wrapper for RAG service search tools.

    This wrapper provides search_kb functionality by calling the RAG service
    HTTP API. It handles:
    - Query execution via RAG service
    - Result formatting for LLM consumption
    - Error handling with fail-fast behavior

    The RAG service must be initialized with documents before search works.
    """

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        rag_client: RAGServiceClient,
        trial_id: str,
    ):
        super().__init__(tool_schema)
        self.rag_client = rag_client
        self.trial_id = trial_id

    async def execute(self, arguments: dict[str, Any]) -> str:
        """
        Execute RAG search.

        Args:
            arguments: Dict with 'query' (required), 'top_k' (optional), 'alpha' (optional)

        Returns:
            JSON string with search results

        Raises:
            RAGServiceError: If search fails (fail fast)
        """
        start_time = time.perf_counter()
        logger.debug(
            f"RAGSearchToolWrapper.execute() ENTRY: tool={self.name}, arguments={arguments}"
        )
        # RAG search is read-only, never changes state
        state_changed = False

        query = arguments.get("query", "")
        if not query:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"RAGSearchToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            return json.dumps({"error": "Query is required", "results": []})

        top_k = arguments.get("top_k", arguments.get("limit", 5))
        alpha = arguments.get("alpha", 0.5)

        logger.debug(f"RAG search: trial={self.trial_id}, query={query[:50]}..., top_k={top_k}")

        try:
            response: SearchResponse = await self.rag_client.search(
                trial_id=self.trial_id,
                query=query,
                limit=top_k,
                alpha=alpha,
            )

            # Format results for LLM consumption
            if not response.results:
                output = json.dumps(
                    {
                        "message": "No relevant documents found.",
                        "results": [],
                        "query": query,
                    }
                )
            else:
                # Build formatted output
                results = []
                for result in response.results:
                    results.append(
                        {
                            "doc_id": result.doc_id,
                            "source": result.source,
                            "score": result.score,
                            "text": result.text,
                            "retrieval_method": result.retrieval_method,
                        }
                    )

                output = json.dumps(
                    {
                        "results": results,
                        "total": len(results),
                        "query": query,
                    }
                )

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"RAGSearchToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            return output

        except RAGServiceError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"RAGSearchToolWrapper.execute() EXIT: tool={self.name}, "
                f"success=False, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            # FAIL FAST: RAG errors should be visible
            logger.error(f"RAG search failed: {e}")
            raise

    def cleanup(self) -> None:
        """Clean up RAG client resources."""
        # RAG client cleanup is handled at factory level
        pass


# =============================================================================
# Reconstructed Tools Container
# =============================================================================


class ReconstructedTools(BaseModel):
    """Container for reconstructed tools."""

    agent_tools: dict[str, Any] = Field(default_factory=dict)  # ToolWrapper instances
    user_tools: dict[str, Any] = Field(default_factory=dict)  # ToolWrapper instances

    model_config = {"arbitrary_types_allowed": True}

    def get_tool(self, name: str, executor: str = "agent") -> ToolWrapper | None:
        """Get a tool by name and executor type."""
        if executor == "user":
            return self.user_tools.get(name)
        return self.agent_tools.get(name)

    def cleanup(self) -> None:
        """Clean up all tool resources."""
        for tool in self.agent_tools.values():
            if hasattr(tool, "cleanup"):
                tool.cleanup()
        for tool in self.user_tools.values():
            if hasattr(tool, "cleanup"):
                tool.cleanup()
        MCPServerToolWrapper.cleanup_all_servers()


# =============================================================================
# Search Tool Schema (for search_kb tool)
# =============================================================================


def create_search_kb_schema() -> ToolSchemaModel:
    """Create the schema for the search_kb tool."""
    return ToolSchemaModel(
        name="search_kb",
        description="Search the knowledge base for relevant information. Use this to find policies, procedures, FAQs, and other documentation.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find relevant documents",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                    "default": 5,
                },
                "alpha": {
                    "type": "number",
                    "description": "Weight for hybrid search: 0.0=keyword only, 1.0=semantic only, 0.5=balanced (default: 0.5)",
                    "default": 0.5,
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        category="read",
        timeout_s=15.0,
        source=None,  # RAG tools don't have a source - they're built-in
    )


# =============================================================================
# Docker Compose Exec Tool Wrapper
# =============================================================================


class DockerComposeExecToolWrapper(ToolWrapper):
    """Execute commands inside a Docker Compose service via ``docker compose exec``.

    Manages compose lifecycle: pull/build → up → exec → down.
    Uses host Docker daemon via mounted socket.
    """

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        compose_file: str,
        task_dir: str,
        service: str = "main",
        env_vars: dict[str, str] | None = None,
    ):
        super().__init__(tool_schema)
        self.compose_file = compose_file
        self.task_dir = task_dir
        self.service = service
        self.env_vars = env_vars or {}
        self.project_name: str | None = None
        self._started = False

    # -- helpers --------------------------------------------------------------

    def _compose_cmd(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            self.compose_file,
            "-p",
            self.project_name,
            *args,
        ]

    def _run(self, cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=self.task_dir,
            env={**__import__("os").environ, **self.env_vars},
        )

    # -- lifecycle ------------------------------------------------------------

    def start(self, project_name: str) -> None:
        """Build/pull images, start the compose stack, and copy tests in.

        Called once per trial from ``RegisterTrial``.
        """
        import os

        self.project_name = project_name
        # Override container_name and log paths for parallel trial isolation.
        # Log paths must exist on the Docker daemon's filesystem (DinD or host).
        self.env_vars["T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME"] = f"{project_name}_main"
        self.env_vars["T_BENCH_TASK_LOGS_PATH"] = f"/workspace/logs/{project_name}"
        self.env_vars["T_BENCH_TASK_AGENT_LOGS_PATH"] = f"/workspace/agent_logs/{project_name}"

        # Pre-create log dirs.  With DinD, /workspace is a shared volume
        # between Runner and DinD, so mkdir on Runner's side creates them
        # on the Docker daemon's filesystem too.
        os.makedirs(f"/workspace/logs/{project_name}", exist_ok=True)
        os.makedirs(f"/workspace/agent_logs/{project_name}", exist_ok=True)

        result = self._run(self._compose_cmd("up", "-d", "--wait"), timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose up failed (project={project_name}): {result.stderr}"
            )
        self._started = True

        # Copy tests/ and run-tests.sh into the container (Harbor does this too).
        task_dir = self.task_dir
        tests_src = os.path.join(task_dir, "tests")
        run_tests_src = os.path.join(task_dir, "run-tests.sh")
        env = {**os.environ, **self.env_vars}

        if os.path.isdir(tests_src):
            subprocess.run(
                self._compose_cmd("cp", f"{tests_src}/.", f"{self.service}:/tests/"),
                cwd=task_dir,
                env=env,
                timeout=30,
            )
        if os.path.isfile(run_tests_src):
            subprocess.run(
                self._compose_cmd("cp", run_tests_src, f"{self.service}:/tests/test.sh"),
                cwd=task_dir,
                env=env,
                timeout=30,
            )

        # Ensure /logs/verifier exists for reward output
        self._run(
            self._compose_cmd(
                "exec", "-T", self.service, "bash", "-c", "mkdir -p /logs/verifier /logs/agent"
            ),
            timeout=10,
        )

        logger.info("DockerComposeExec: stack started (project=%s)", project_name)

    def stop(self) -> None:
        """Tear down the compose stack.  Called on trial cleanup."""
        if self._started and self.project_name:
            self._run(
                self._compose_cmd("down", "-v", "--remove-orphans"),
                timeout=60,
            )
            self._started = False
            logger.info(
                "DockerComposeExec: stack stopped (project=%s)",
                self.project_name,
            )

    def cleanup(self) -> None:
        """Override base cleanup to tear down compose."""
        self.stop()

    # -- execute --------------------------------------------------------------

    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute a bash command in the main container."""
        command = arguments.get("command", "")
        timeout = self.timeout_s or 120.0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._exec_sync, command, timeout)

    def _exec_sync(self, command: str, timeout: float) -> str:
        proc = self._run(
            self._compose_cmd("exec", "-T", self.service, "bash", "-c", command),
            timeout=timeout,
        )
        output = proc.stdout
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]\n{proc.stderr}"
        return output


# =============================================================================
# Tool Factory
# =============================================================================


class ToolFactory:
    """
    Factory for reconstructing tools from ToolSource definitions.

    The factory creates appropriate wrappers based on invocation style:
    - tau_sync: TauSyncToolWrapper
    - mcp_async: MCPAsyncToolWrapper
    - mcp_server: MCPServerToolWrapper
    - rag_search: RAGSearchToolWrapper (for search_kb tool)

    FAIL FAST: If any tool cannot be reconstructed, raises ToolReconstructionError.
    """

    def __init__(
        self,
        db_client: DBServiceClient,
        trial_id: str,
        rag_client: RAGServiceClient | None = None,
        db_table_names: list[str] | None = None,
        initial_state_data: dict[str, list[dict]] | None = None,
    ):
        """
        Initialize the tool factory.

        Args:
            db_client: HTTP client for DB Service communication
            trial_id: Unique trial identifier
            rag_client: Optional RAG service client for search tools
            db_table_names: Optional list of actual table names from initial_state.
                           These are the source of truth for table name registration.
            initial_state_data: Optional dict mapping table names to their records.
                               Used for ID field matching during model registration.
        """
        self.db_client = db_client
        self.trial_id = trial_id
        self.rag_client = rag_client
        self.db_table_names = db_table_names or []
        self._initial_state_data = initial_state_data or {}
        self._claimed_tables: set[str] = set()

        # Create DB proxies for tools
        # Pass db_table_names so the proxy can resolve table names for unregistered models
        self._async_proxy = DBServiceProxy(db_client, trial_id, db_table_names=self.db_table_names)
        self._sync_proxy = SyncDBServiceProxy(self._async_proxy)

    def reconstruct_tools(
        self,
        agent_tools: list[dict[str, Any]],
        user_tools: list[dict[str, Any]] | None = None,
    ) -> ReconstructedTools:
        """
        Reconstruct tools from ToolSchema definitions.

        FAIL FAST: If any tool cannot be reconstructed, raises ToolReconstructionError.

        Args:
            agent_tools: List of agent tool schema dicts
            user_tools: Optional list of user tool schema dicts

        Returns:
            ReconstructedTools container with callable wrappers

        Raises:
            ToolReconstructionError: If any tool cannot be reconstructed
        """
        result = ReconstructedTools()

        # Reconstruct agent tools (FAIL FAST)
        for tool_dict in agent_tools:
            schema = ToolSchemaModel.model_validate(tool_dict)
            wrapper = self._create_wrapper(schema)
            if wrapper:
                result.agent_tools[schema.name] = wrapper
                logger.info(f"Reconstructed agent tool: {schema.name}")
            # If wrapper is None, _create_wrapper already raised

        # Reconstruct user tools (FAIL FAST)
        if user_tools:
            for tool_dict in user_tools:
                schema = ToolSchemaModel.model_validate(tool_dict)
                wrapper = self._create_wrapper(schema)
                if wrapper:
                    result.user_tools[schema.name] = wrapper
                    logger.info(f"Reconstructed user tool: {schema.name}")
                # If wrapper is None, _create_wrapper already raised

        return result

    def _create_wrapper(self, schema: ToolSchemaModel) -> ToolWrapper:
        """
        Create a tool wrapper based on invocation style.

        FAIL FAST: Raises ToolReconstructionError if tool cannot be created.

        Args:
            schema: Tool schema with source information

        Returns:
            ToolWrapper instance

        Raises:
            ToolConfigurationError: If tool has no source and is not a built-in
            ToolImportError: If tool module/class cannot be imported
        """
        # Handle built-in tools (no source)
        if schema.source is None:
            # Check if this is a known built-in tool
            if schema.name == "search_kb":
                return self._create_rag_search_wrapper(schema)
            if schema.name in ("read_file", "write_file", "list_dir"):
                return BuiltinFileToolWrapper(schema)
            if schema.name in _BUILTIN_TOOL_FACTORIES:
                return BuiltinGenericToolWrapper(schema)
            raise ToolConfigurationError(
                schema.name, "Tool has no source configuration, cannot reconstruct"
            )

        source = schema.source
        style = source.invocation_style

        if style == InvocationStyle.TAU_SYNC:
            return self._create_tau_sync_wrapper(schema, source)
        elif style == InvocationStyle.MCP_ASYNC:
            return self._create_mcp_async_wrapper(schema, source)
        elif style == InvocationStyle.MCP_SERVER:
            return self._create_mcp_server_wrapper(schema, source)
        elif style == InvocationStyle.DOCKER_COMPOSE_EXEC:
            return self._create_docker_compose_exec_wrapper(schema, source)
        else:
            raise ToolConfigurationError(schema.name, f"Unknown invocation style: {style}")

    def _create_tau_sync_wrapper(
        self, schema: ToolSchemaModel, source: ToolSourceModel
    ) -> TauSyncToolWrapper:
        """
        Create a Tau sync tool wrapper.

        FAIL FAST: Raises ToolImportError if module/class cannot be imported.

        Import path: {source.toolset}.{source.module_path}
        """
        try:
            module_path = f"{source.toolset}.{source.module_path}"
            module = importlib.import_module(module_path)
            tool_class = getattr(module, source.class_name)

            return TauSyncToolWrapper(
                tool_schema=schema,
                tool_class=tool_class,
                db_proxy=self._sync_proxy,
            )
        except ImportError as e:
            raise ToolImportError(
                schema.name, f"Cannot import module '{source.toolset}.{source.module_path}': {e}"
            )
        except AttributeError as e:
            raise ToolImportError(
                schema.name,
                f"Class '{source.class_name}' not found in module "
                f"'{source.toolset}.{source.module_path}': {e}",
            )

    def _create_mcp_async_wrapper(
        self, schema: ToolSchemaModel, source: ToolSourceModel
    ) -> MCPAsyncToolWrapper:
        """
        Create an MCP async tool wrapper.

        FAIL FAST: Raises ToolImportError if module/class cannot be imported.

        Import path: mcp_tools_library.{source.toolset}.{source.module_path}

        Note: MCP tools call db methods synchronously inside their async run()
        method, so we pass SyncDBServiceProxy instead of DBServiceProxy.

        Also registers model classes from the toolset with namespaced table names
        so that db.create(Ticket(...)) maps to 'zendesk_tickets' table.
        """
        try:
            module_path = f"mcp_tools_library.{source.toolset}.{source.module_path}"
            module = importlib.import_module(module_path)
            tool_class = getattr(module, source.class_name)

            # Register models from the toolset with namespaced table names
            # This ensures db.create(Ticket(...)) maps to 'zendesk_tickets'
            self._register_toolset_models(source.toolset)

            return MCPAsyncToolWrapper(
                tool_schema=schema,
                tool_class=tool_class,
                db_proxy=self._sync_proxy,  # MCP tools need sync proxy!
            )
        except ImportError as e:
            raise ToolImportError(
                schema.name,
                f"Cannot import module 'mcp_tools_library.{source.toolset}.{source.module_path}': {e}",
            )
        except AttributeError as e:
            raise ToolImportError(
                schema.name,
                f"Class '{source.class_name}' not found in module "
                f"'mcp_tools_library.{source.toolset}.{source.module_path}': {e}",
            )

    def _get_id_field_name(self, model_cls: type) -> str | None:
        """
        Extract the ID field name from a model's get_id() method.

        Parses the source code of get_id() to find 'return self.FIELD_NAME'
        and extracts FIELD_NAME.

        Examples:
            Review.get_id → self.review_id → "review_id"
            QualityReview.get_id → self.quality_review_id → "quality_review_id"
            Sku.get_id → self.sku_id → "sku_id"

        Args:
            model_cls: The model class with a get_id() method

        Returns:
            The ID field name, or None if extraction fails
        """
        import inspect

        try:
            source = inspect.getsource(model_cls.get_id)
            for line in source.split("\n"):
                line = line.strip()
                if line.startswith("return self."):
                    return line.replace("return self.", "").strip()
        except (TypeError, OSError):
            pass
        return None

    def _register_toolset_models(self, toolset: str) -> None:
        """
        Register Pydantic model classes from a toolset with actual DB table names.

        This is necessary because MCP tools use Pydantic models like Ticket,
        but the DB service stores them in tables like 'zendesk_tickets'.

        Uses a 4-strategy matching approach to find the correct table for each model:

        1. table_name ClassVar: If model has a table_name attribute, use it directly
           or find a table ending with that name.

        2. ID field matching (universal): Extract the ID field name from get_id(),
           then find the table whose records contain that field. This is the most
           reliable approach as it works for ANY domain without code changes.
           Examples:
             Review.get_id → self.review_id → table with review_id → review_api_reviews ✅
             QualityReview.get_id → self.quality_review_id → table with quality_review_id → content_api_quality_reviews ✅

        3. Suffix matching (only for empty tables): Falls back to suffix matching
           only for tables with no records in initial_state.

        4. FAIL LOUD: If no match found, raise RuntimeError to surface the issue.

        Args:
            toolset: The toolset path (e.g., 'consulting.zendesk', 'external_retail_toolset.oms')

        Raises:
            RuntimeError: If any model cannot be matched to a table
        """
        # Only register once per toolset
        if hasattr(self, "_registered_toolsets"):
            if toolset in self._registered_toolsets:
                return
        else:
            self._registered_toolsets: set = set()

        try:
            # Try to import the models module from the toolset
            models_module_path = f"mcp_tools_library.{toolset}.models"
            models_module = importlib.import_module(models_module_path)

            # Collect all model classes from the module
            model_classes: list[type] = []
            for attr_name in dir(models_module):
                attr = getattr(models_module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseModel)
                    and attr is not BaseModel
                    and hasattr(attr, "get_id")  # MCP models have get_id()
                ):
                    model_classes.append(attr)

            # Track claimed tables to prevent double-matching
            claimed_tables = self._claimed_tables
            initial_data = self._initial_state_data or {}

            # Register each model class with its matching table name
            for model_cls in model_classes:
                matched_table: str | None = None

                # Strategy 1: table_name ClassVar (handles tau_manufacturing)
                if hasattr(model_cls, "table_name"):
                    table_name_attr = model_cls.table_name
                    for t in self.db_table_names:
                        if t == table_name_attr or t.endswith(f"_{table_name_attr}"):
                            if t not in claimed_tables:
                                matched_table = t
                                logger.info(
                                    f"Matched {model_cls.__name__} to '{t}' via table_name ClassVar"
                                )
                                break

                # Strategy 2: ID field matching (universal)
                if matched_table is None:
                    id_field = self._get_id_field_name(model_cls)
                    if id_field:
                        # Collect all candidates with matching first key
                        candidates = []
                        for t in self.db_table_names:
                            if t in claimed_tables:
                                continue
                            records = initial_data.get(t, [])
                            if records and list(records[0].keys())[0] == id_field:
                                candidates.append((t, records))

                        # Always validate before claiming - prevents wrong model from claiming table
                        for t, records in candidates:
                            try:
                                model_cls.model_validate(records[0])
                                matched_table = t
                                logger.info(
                                    f"Matched {model_cls.__name__} to '{t}' via ID field '{id_field}' + validation"
                                )
                                break
                            except Exception:
                                logger.debug(
                                    f"ID field matched but validation failed: {model_cls.__name__} vs '{t}'"
                                )
                                continue

                # Strategy 3: Suffix matching ONLY for empty tables
                if matched_table is None:
                    name = model_cls.__name__
                    snake_name = "".join(
                        ["_" + c.lower() if c.isupper() else c for c in name]
                    ).lstrip("_")
                    plural_name = self._to_plural(snake_name)
                    snake_suffix_plural = f"_{plural_name}"
                    snake_suffix_singular = f"_{snake_name}"

                    for t in self.db_table_names:
                        if t in claimed_tables:
                            continue
                        # Only use suffix matching for empty tables
                        records = initial_data.get(t, [])
                        if records:
                            # Table has records - skip suffix matching, ID field should have matched
                            continue

                        # Check suffix matches
                        if (
                            t.endswith(snake_suffix_plural)
                            or t.endswith(snake_suffix_singular)
                            or t in (plural_name, snake_name)
                        ):
                            matched_table = t
                            logger.info(
                                f"Matched {model_cls.__name__} to '{t}' via suffix (empty table)"
                            )
                            break

                # Strategy 4: WARN and skip (don't crash on missing optional tables)
                if matched_table is None:
                    id_field = self._get_id_field_name(model_cls) or "unknown"
                    unclaimed = [t for t in self.db_table_names if t not in claimed_tables]
                    logger.warning(
                        f"Cannot match model {model_cls.__name__} (id_field={id_field}) "
                        f"to any table. Skipping registration. "
                        f"Available unclaimed: {unclaimed}"
                    )
                    continue  # Skip this model, don't register

                # Register the model with the matched table
                claimed_tables.add(matched_table)
                self._async_proxy.register_model(matched_table, model_cls)
                logger.info(
                    f"Registered model {model_cls.__name__} (module={model_cls.__module__}) "
                    f"-> table '{matched_table}'"
                )

            self._registered_toolsets.add(toolset)
            logger.info(f"Registered models for toolset '{toolset}'")

        except ImportError:
            # No models module - that's OK, some toolsets may not have models
            logger.debug(f"No models module found for toolset '{toolset}'")
            self._registered_toolsets.add(toolset)

    def _to_plural(self, singular: str) -> str:
        """
        Convert singular form to plural using English grammar rules.

        Handles various pluralization patterns:
        - Regular plurals: item -> items
        - -y endings: entry -> entries
        - -s/-x/-z/-ch/-sh endings: box -> boxes, class -> classes
        - -f/-fe endings: shelf -> shelves
        """
        # Words ending in consonant + y
        if singular.endswith("y") and len(singular) > 1 and singular[-2] not in "aeiou":
            return singular[:-1] + "ies"

        # Words ending in s, x, z, ch, sh
        if singular.endswith(("s", "x", "z")):
            return singular + "es"
        if singular.endswith(("ch", "sh")):
            return singular + "es"

        # Words ending in f or fe
        if singular.endswith("f"):
            return singular[:-1] + "ves"
        if singular.endswith("fe"):
            return singular[:-2] + "ves"

        # Words ending in o (some take -es, but most take -s)
        # For simplicity, just add -s
        if singular.endswith("o"):
            return singular + "s"

        # Standard -s ending
        return singular + "s"

    def _create_mcp_server_wrapper(
        self, schema: ToolSchemaModel, source: ToolSourceModel
    ) -> MCPServerToolWrapper:
        """
        Create an MCP server tool wrapper.

        FAIL FAST: Raises ToolConfigurationError if mcp_server_script is missing.

        Server script: {source.mcp_server_script}
        """
        if not source.mcp_server_script:
            raise ToolConfigurationError(
                schema.name, "MCP server tool missing 'mcp_server_script' in source"
            )

        return MCPServerToolWrapper(
            tool_schema=schema,
            server_script=source.mcp_server_script,
            db_client=self.db_client,
            trial_id=self.trial_id,
        )

    def _create_docker_compose_exec_wrapper(
        self, schema: ToolSchemaModel, source: ToolSourceModel
    ) -> DockerComposeExecToolWrapper:
        """Create a Docker Compose exec wrapper for terminal-bench tasks.

        FAIL FAST: Raises ToolConfigurationError if required extra fields are missing.
        """
        extra = source.extra
        compose_file = extra.get("compose_file")
        task_dir = extra.get("task_dir")
        if not compose_file or not task_dir:
            raise ToolConfigurationError(
                schema.name,
                "DOCKER_COMPOSE_EXEC requires 'compose_file' and 'task_dir' in source.extra",
            )
        return DockerComposeExecToolWrapper(
            tool_schema=schema,
            compose_file=compose_file,
            task_dir=task_dir,
            service=extra.get("service", "main"),
            env_vars=extra.get("env_vars", {}),
        )

    def _create_rag_search_wrapper(self, schema: ToolSchemaModel) -> RAGSearchToolWrapper:
        """
        Create a RAG search tool wrapper.

        FAIL FAST: Raises ToolConfigurationError if RAG client not available.

        Args:
            schema: Tool schema for search_kb

        Returns:
            RAGSearchToolWrapper instance
        """
        if self.rag_client is None:
            raise ToolConfigurationError(
                schema.name,
                "RAG client not configured. Set RAG_SERVICE_URL environment variable.",
            )

        return RAGSearchToolWrapper(
            tool_schema=schema,
            rag_client=self.rag_client,
            trial_id=self.trial_id,
        )


# =============================================================================
# Convenience function
# =============================================================================


def reconstruct_tools(
    tools: list[dict[str, Any]],
    db_client: DBServiceClient,
    trial_id: str,
    is_user_tools: bool = False,
    rag_client: RAGServiceClient | None = None,
) -> dict[str, ToolWrapper]:
    """
    Convenience function to reconstruct tools from schema dicts.

    FAIL FAST: Raises ToolReconstructionError if any tool cannot be reconstructed.

    Args:
        tools: List of tool schema dictionaries
        db_client: DB Service client
        trial_id: Trial identifier
        is_user_tools: Whether these are user-side tools
        rag_client: Optional RAG service client for search tools

    Returns:
        Dictionary mapping tool name to wrapper

    Raises:
        ToolReconstructionError: If any tool cannot be reconstructed
    """
    factory = ToolFactory(db_client, trial_id, rag_client)

    if is_user_tools:
        result = factory.reconstruct_tools([], tools)
        return result.user_tools
    else:
        result = factory.reconstruct_tools(tools, [])
        return result.agent_tools
