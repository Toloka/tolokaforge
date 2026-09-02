"""
Tool Factory for Runner

This module provides tool reconstruction from ToolSource definitions.
It creates callable wrappers for four invocation styles:

1. tau_sync - Tau environment tools (synchronous invoke())
2. mcp_async - TlkMcpCore MCP tools (async run_with_validation())
3. mcp_server - Native MCP server tools (subprocess JSON-RPC)
4. rag_search - RAG service search tools (HTTP API)

Each wrapper produces a callable with the same interface:
    async def execute(arguments: dict[str, Any]) -> str
and reports what its substrate said about the call beside that text through:
    async def execute_call(arguments: dict[str, Any]) -> ToolCallOutcome

Usage:
    factory = ToolFactory(db_client, trial_id)
    tools = factory.reconstruct_tools(tool_schemas)
    result = await tools["book_reservation"]({"user_id": "123", "flight": "AA100"})
"""

import asyncio
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.runner.compose_naming import compose_container_name
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.db_proxy import DBServiceProxy, SyncDBServiceProxy
from tolokaforge.runner.id_resolution import TableKey, compute_diff_ops, table_key
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
from tolokaforge.tools.persistent_shell import (
    BashSession,
    CommandResult,
    DockerComposeBashSession,
    LocalBashSession,
)
from tolokaforge.tools.registry import TOOL_FAILURE_WITHOUT_MESSAGE, raised_tool_failure_text
from tolokaforge.tools.str_replace_editor import (
    DockerComposeEditor,
    EditorBackend,
    EditorError,
    LocalFilesystemEditor,
)

logger = logging.getLogger(__name__)


def _resolve_compose_container_name(trial_id: str, service: str, project_prefix: str) -> str:
    """Container name for *service* in the per-trial compose stack.

    Thin re-export of :func:`tolokaforge.runner.compose_naming.compose_container_name`
    that keeps the ``bash_session`` and ``str_replace_editor`` wrappers' local
    resolver-name stable; the shared implementation is what the compose lifecycle
    consumer on the host uses to name the project directory, so an exec targets
    the same container the stack brought up.
    """
    return compose_container_name(trial_id, service, project_prefix)


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
    Raising this from a wrapper's execute() lets the runner service record
    EXECUTION_STATUS_ERROR so tool_success_rate, failure_attribution, and
    error_count reflect reality.

    ``str`` is the tool's own message alone, because the runner records it as
    the failed call's result text and the in-process substrate records the same
    text verbatim from ``ToolResult.error``. ``tool_name`` stays an attribute
    and the runner's log names the tool independently.
    """

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(message)


# =============================================================================
# Tool Wrapper Base Class
# =============================================================================


@dataclass(frozen=True)
class ToolLifecycleContext:
    """Per-trial context passed to a tool's ``start()``.

    Carries only what a lifecycle tool may need to provision its per-trial
    resources, so the runner can drive lifecycle generically without knowing any
    specific tool or adapter.
    """

    trial_id: str
    artifacts_dir: str | None = None
    work_dir: str | None = None


@dataclass(frozen=True)
class ToolCallOutcome:
    """What a tool call answered, and whether the substrate declared it a failure."""

    output: str
    declared_failure: bool


BACKSTOP_GRACE_S = 5.0
"""Slack between a tool's own per-call budget and the runner's backstop.

It covers what a tool spends finishing *after* its own budget elapses, and the
two engines spend it differently:

* local shell — measured 0.7 s worst case: SIGINT, then SIGKILL 0.2 s later,
  then a 0.3 s drain, on top of a 0.2 s poll interval. 5 s clears it ~7x.
* compose shell — SIGKILL, then ``proc.wait(5)``, then a reopened ``docker
  exec`` whose readiness and ``cd`` round trips are bounded at
  ``_OPEN_TIMEOUT_S`` each. Warm, a local exec reopens in milliseconds and 5 s
  clears it comfortably; at those bounds it would not.

That second worst case is accepted rather than covered, because covering it
would mean holding a wedged trial ~35 s past its budget on every engine. When it
happens the backstop fires mid-reopen and the call reports a plain timeout —
``DockerComposeExecToolWrapper`` rebuilds no session of the runner's, so the
degradation is a lost reopen, not a poisoned pipe.

Resizing this downward needs both engines re-measured, not just the local one."""


class ToolWrapper(ABC):
    """
    Base class for tool wrappers.

    All wrappers must implement the execute() method with the same interface.
    Beside it, execute_call() is concrete: a wrapper over a substrate that declares
    a failed call out of band overrides that method rather than adding a third one.
    """

    # Whether the runner manages this tool's per-trial lifecycle via start()/stop()
    # in RegisterTrial/ResetTrial. Default False: most tools have no per-trial
    # resources to provision. Lifecycle tools (e.g. a compose-backed sandbox)
    # override this to True. This keeps the runner adapter-agnostic — it drives
    # lifecycle off this capability, never off the adapter type.
    has_lifecycle: bool = False

    # Consulted only for a ``has_lifecycle`` tool the runner's backstop fires on:
    # does closing and reopening this tool clear state an abandoned worker could
    # otherwise serve to the next call? True for a tool holding a pipe or session
    # of its own. False for a tool whose start() only resolves configuration —
    # rebuilding that clears nothing, so the call must not tell the agent its
    # session was reset. Defaults True so a new lifecycle tool is rebuilt rather
    # than silently left holding a poisoned pipe.
    rebuild_clears_backstopped_state: bool = True

    def __init__(self, tool_schema: ToolSchemaModel):
        self.tool_schema = tool_schema
        self.name = tool_schema.name
        self.timeout_s = tool_schema.timeout_s

    @property
    def own_budget_s(self) -> float | None:
        """The per-call budget this wrapper applies to its own work, if any.

        Override in a wrapper that hands a number to its own timeout mechanism
        — an ``httpx`` ``timeout=``, a ``subprocess`` ``timeout=``, a session's
        per-command budget. A wrapper that bounds nothing leaves this ``None``
        and is banded by its declared ``timeout_s`` alone.

        This is the one place a self-bounding wrapper has to override:
        :attr:`effective_timeout_s` derives the backstop from it.
        """
        return None

    @property
    def effective_timeout_s(self) -> float:
        """The band the runner's backstop applies to a call on this tool.

        Strictly greater than :attr:`own_budget_s` whenever the wrapper names
        one. The backstop abandons the worker thread rather than killing it,
        while a tool's own timeout terminates the work and leaves its session
        usable — so the tool's control must be the one that fires, and equality
        between the two is a race.

        The declared ``timeout_s`` is kept as a floor, so a tool bounding
        itself well inside its declaration keeps the wider band rather than
        being tightened to a value nothing asked for.
        """
        own = self.own_budget_s
        if own is None:
            return self.timeout_s
        return max(self.timeout_s, own + BACKSTOP_GRACE_S)

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

    async def execute_call(self, arguments: dict[str, Any]) -> ToolCallOutcome:
        """Execute the tool and report what its substrate said about the call.

        A substrate with no out-of-band failure channel signals a failed call by
        raising, and the golden-replay loop records that raise, so the default
        reports ``declared_failure=False`` and lets any exception travel
        untouched. A wrapper over a substrate that answers a failed call with a
        flag beside the output — MCP's ``isError`` — overrides this method.
        """
        return ToolCallOutcome(output=await self.execute(arguments), declared_failure=False)

    async def __call__(self, arguments: dict[str, Any]) -> str:
        """Allow calling the wrapper directly."""
        return await self.execute(arguments)

    def start(self, ctx: "ToolLifecycleContext") -> None:  # noqa: B027
        """Provision per-trial resources (override in lifecycle tools)."""
        pass

    def stop(self) -> None:  # noqa: B027
        """Tear down resources provisioned by start() (override if needed)."""
        pass

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
        id_fields: Mapping[str, str | list[str]] | None = None,
    ):
        super().__init__(tool_schema)
        self.tool_class = tool_class
        self.db_proxy = db_proxy
        self._id_fields: dict[str, str | list[str]] = dict(id_fields or {})
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
        """Detect and sync state changes to DB Service.

        Keys are resolved per table from ``state_checks.id_fields`` (default
        ``"id"``); a record missing its resolved key raises fail-loud rather
        than collapsing all keyless records to a single ``None`` bucket.
        """
        for table_name in set(before) | set(after):
            before_records = before.get(table_name, [])
            after_records = after.get(table_name, [])
            if before_records == after_records:
                continue
            operations = compute_diff_ops(
                before_records, after_records, table_name, self._id_fields
            )
            if operations:
                await self.db_proxy._async_proxy.db_client.mutate(
                    trial_id=self.db_proxy.trial_id, table_name=table_name, operations=operations
                )


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
            # Drain stderr so the actual subprocess crash reason is visible.
            # Without this the only signal is the empty-stdout symptom and the
            # real cause (import error, lifespan crash, …) is lost in the pipe.
            stderr_tail = ""
            if self.process.stderr is not None:
                try:
                    stderr_tail = self.process.stderr.read() or ""
                except Exception:
                    pass
            exit_code = self.process.poll()
            raise RuntimeError(
                f"MCP server closed connection (script={self.script_path}, "
                f"exit_code={exit_code}, stderr_tail={stderr_tail[-2000:]!r})"
            )

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
        """Return the tool call's output text; ``execute_call`` carries the flag too."""
        return (await self.execute_call(arguments)).output

    async def execute_call(self, arguments: dict[str, Any]) -> ToolCallOutcome:
        """Send the call to the server subprocess over JSON-RPC.

        MCP answers a call the tool never completed with ``isError: true`` beside
        the error prose, so this is where the protocol's own failure verdict is
        read — the prose alone cannot be told apart from a normal result.
        """
        start_time = time.perf_counter()
        logger.debug(
            f"MCPServerToolWrapper.execute_call() ENTRY: tool={self.name}, arguments={arguments}"
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
                f"MCPServerToolWrapper.execute_call() EXIT: tool={self.name}, "
                f"success=True, state_changed={state_changed}, latency_ms={latency_ms:.2f}"
            )
            # MCP specifies CallToolResult.isError as optional, absent meaning
            # the call did not fail — so the default is the protocol's, not a
            # fallback covering a response shape we failed to handle.
            return ToolCallOutcome(
                output=output, declared_failure=bool(result.get("isError", False))
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                f"MCPServerToolWrapper.execute_call() EXIT: tool={self.name}, "
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

        # base_path = /work so the file tools target the same directory as
        # BashTool's workdir and the runner's filesystem-provisioning code
        # (see service.py RegisterTrial). Without this, read_file looks at
        # /env/fs/agent-visible/X but the runner wrote to /work/X.
        WORK_DIR = "/work"
        if tool_schema.name == "read_file":
            self._tool = ReadFileTool(base_path=WORK_DIR)
        elif tool_schema.name == "write_file":
            self._tool = WriteFileTool(base_path=WORK_DIR)
        elif tool_schema.name == "list_dir":
            self._tool = ListDirTool(base_path=WORK_DIR)
        else:
            raise ToolConfigurationError(
                tool_schema.name,
                f"BuiltinFileToolWrapper does not support tool '{tool_schema.name}'",
            )

    async def execute(self, arguments: dict[str, Any]) -> str:
        result = self._tool.execute(**arguments)
        if result.success:
            return result.output or ""
        # Raise so the runner service records EXECUTION_STATUS_ERROR,
        # preserving correct tool_success_rate and failure attribution.
        # The runner's exception handler sends the error message back to
        # the LLM, so the agent can still self-correct.
        raise ToolExecutionError(self.name, result.error or TOOL_FAILURE_WITHOUT_MESSAGE)


# =============================================================================
# Builtin Generic Tool Wrapper
# =============================================================================


class BuiltinGenericToolWrapper(ToolWrapper):
    """Wrapper for builtin tools loaded by name from the unified registry.

    Handles tools like browser, bash, calculator, http_request, mobile, etc.
    Instantiates the tool class via ``tolokaforge.tools.builtin.registry``
    and delegates ``execute()`` to it. Per-task init kwargs come from
    ``ToolSchema.tool_config``.
    """

    def __init__(self, tool_schema: ToolSchemaModel):
        super().__init__(tool_schema)
        import inspect

        from tolokaforge.tools.builtin import registry as builtin_registry

        if not builtin_registry.is_builtin(tool_schema.name):
            raise ToolConfigurationError(
                tool_schema.name,
                f"No builtin factory for tool '{tool_schema.name}'",
            )
        try:
            cls = builtin_registry.get_class(tool_schema.name)
        except Exception as exc:
            raise ToolConfigurationError(
                tool_schema.name,
                f"Failed to import builtin tool '{tool_schema.name}': {exc}",
            ) from exc

        # Splat ``tool_schema.tool_config`` into the tool's __init__.
        # Unknown keys raise rather than getting silently dropped (a
        # filter would mask YAML typos as runtime quirks). The error
        # enumerates the kwargs the tool actually accepts so the
        # caller can spot the typo at trial registration.
        tool_config = tool_schema.tool_config or {}
        valid_kwargs = {p for p in inspect.signature(cls.__init__).parameters if p != "self"}
        unknown = set(tool_config) - valid_kwargs
        if unknown:
            raise ToolConfigurationError(
                tool_schema.name,
                f"Unknown tool_config keys for '{tool_schema.name}': "
                f"{sorted(unknown)}; accepted: {sorted(valid_kwargs)}",
            )
        try:
            self._tool = cls(**tool_config)
        except Exception as exc:
            raise ToolConfigurationError(
                tool_schema.name,
                f"Failed to instantiate builtin tool '{tool_schema.name}': {exc}",
            ) from exc

    @property
    def own_budget_s(self) -> float:
        """The budget the wrapped builtin applies to its own I/O.

        Read from the tool, not from the schema: the schema carries one pinned
        value for every builtin of every native pack (#1147), and several
        builtins bound themselves above it — ``build_check`` at 300 s,
        ``browser`` and ``mobile`` at 60 s, against a pinned 30 s. Banding on
        the schema alone cuts a healthy call short and records it a timeout.
        """
        return self._tool.policy.timeout_s

    async def execute(self, arguments: dict[str, Any]) -> str:
        result = self._tool.execute(**arguments)
        if result.success:
            return result.output or ""
        # Raise so the runner service records EXECUTION_STATUS_ERROR,
        # preserving correct tool_success_rate and failure attribution.
        # The runner's exception handler sends the error message back to
        # the LLM, so the agent can still self-correct.
        raise ToolExecutionError(self.name, result.error or TOOL_FAILURE_WITHOUT_MESSAGE)


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

    @property
    def own_budget_s(self) -> float:
        """The declared budget, which this wrapper hands to the RAG request.

        The same shape the in-process ``search_kb`` uses
        (:mod:`tolokaforge.tools.builtin.rag_search` passes its declared budget
        to ``httpx``), so the two substrates bound the same call the same way
        rather than one inheriting whatever the shared client was built with.
        """
        return self.timeout_s

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
                timeout=self.own_budget_s,
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


_COMPOSE_EXEC_DEFAULT_TIMEOUT_S = 120.0


class DockerComposeExecToolWrapper(ToolWrapper):
    """Execute a command inside an already-running compose service via ``docker exec``.

    The wrapper never brings a compose stack up or down: :class:`~tolokaforge.
    runtime.per_trial_runtime.PerTrialRuntimeBackend` owns the trial's compose
    project lifecycle. ``start()`` records the trial id (its only reason to
    live) and resolves the target container via
    :func:`~tolokaforge.runner.compose_naming.compose_container_name`, the same
    resolver the host-side materialiser uses to name the project — so the argv
    ``docker exec -i <container> bash -c <command>`` targets the container the
    per-trial runtime brought up.
    """

    # Runner-managed per-trial lifecycle: start() is how the wrapper learns
    # ctx.trial_id so it can resolve the container name. No subprocess runs
    # in start() — the compose stack is brought up by the per-trial runtime.
    has_lifecycle = True

    # Which is also why rebuilding this wrapper clears nothing: it holds a
    # container *name*, not a session, and the exec that outlived the backstop
    # is inside a container the per-trial runtime owns.
    rebuild_clears_backstopped_state = False

    def __init__(
        self,
        tool_schema: ToolSchemaModel,
        service: str,
        compose_project_prefix: str,
    ):
        super().__init__(tool_schema)
        self._service = service
        self._project_prefix = compose_project_prefix
        self._trial_id: str | None = None
        self._container: str | None = None

    def start(self, ctx: "ToolLifecycleContext") -> None:
        self._trial_id = ctx.trial_id
        self._container = compose_container_name(ctx.trial_id, self._service, self._project_prefix)

    @property
    def own_budget_s(self) -> float:
        return self.timeout_s or _COMPOSE_EXEC_DEFAULT_TIMEOUT_S

    async def execute(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command", "")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._exec_sync, command, self.own_budget_s)

    def _exec_sync(self, command: str, timeout: float) -> str:
        if self._container is None:
            raise ToolExecutionError(
                self.name,
                "docker_compose_exec tool executed before start() — container name unresolved",
            )
        proc = subprocess.run(
            ["docker", "exec", "-i", self._container, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]\n{proc.stderr}"
        return output

    def _exec_sync_with_rc(self, command: str, timeout: float) -> tuple[int, str]:
        """Run ``command`` and return ``(returncode, stdout+stderr_merged)``.

        Sibling of :meth:`_exec_sync`. The two-arg-tuple return exposes the
        returncode to callers that need to render it (e.g. the substrate's
        test-suite RPC that ships the exit code on the wire) without gating on
        it. rc=0 → merged output is just stdout; rc≠0 → stderr is appended
        after stdout (no ``[exit code: N]`` annotation — the returncode
        itself rides in the tuple).
        """
        if self._container is None:
            raise ToolExecutionError(
                self.name,
                "docker_compose_exec tool executed before start() — container name unresolved",
            )
        proc = subprocess.run(
            ["docker", "exec", "-i", self._container, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        merged = proc.stdout + (proc.stderr if proc.returncode != 0 else "")
        return proc.returncode, merged


# =============================================================================
# Persistent Shell Tool Wrapper
# =============================================================================


class PersistentShellToolWrapper(ToolWrapper):
    """Runner-side executor for the session-lifetime ``bash_session`` tool.

    Holds one bash session for the trial: ``start()`` opens it (seeding the
    working directory from :class:`ToolLifecycleContext`), ``execute()`` runs
    commands or restarts the shell, and ``stop()`` tears it down. State (cwd,
    environment, functions) persists across ``execute()`` calls.
    """

    has_lifecycle = True

    def __init__(self, tool_schema: ToolSchemaModel):
        super().__init__(tool_schema)
        tool_config = tool_schema.tool_config or {}
        # Resolve from tool_config, not self.timeout_s: the native adapter pins
        # every builtin's ToolSchema.timeout_s to 30.0, so self.timeout_s can
        # never carry the ADR-locked 120s default or a task's configured value.
        self._timeout_s = float(tool_config.get("timeout_s", 120.0))
        # Provider is a config axis: a `service` key selects the compose backend
        # (docker exec into a running service container); its absence selects the
        # local subprocess backend. The wire schema is identical either way.
        self._service: str | None = tool_config.get("service")
        self._project_prefix: str | None = tool_config.get("compose_project_prefix")
        # Optional ``docker exec --user <user>`` for the compose backend. Task
        # packs whose grader container runs as root (so the in-container
        # grader can read a hidden test oracle) use this to drop privileges
        # on agent-facing exec sessions. Local backend ignores it. Default
        # ``None`` inherits the container's default user — preserves prior
        # behaviour for every current pack.
        self._user: str | None = tool_config.get("user")
        if self._service is not None and not self._project_prefix:
            raise ToolConfigurationError(
                self.name,
                "bash_session with a 'service' requires 'compose_project_prefix' to "
                "resolve the running container name (the per-trial compose project "
                "prefix used to bring the stack up)",
            )
        self._trial_id: str | None = None
        self._session: BashSession | None = None
        self._cwd: str | None = None

    @property
    def own_budget_s(self) -> float:
        return self._timeout_s

    def start(self, ctx: "ToolLifecycleContext") -> None:
        self._trial_id = ctx.trial_id
        self._cwd = ctx.work_dir if ctx.work_dir and os.path.isdir(ctx.work_dir) else None
        session = self._new_session()
        session.open(self._cwd)
        self._session = session

    def stop(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def cleanup(self) -> None:
        self.stop()

    async def execute(self, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise ToolExecutionError(self.name, "bash session not started")
        if arguments.get("restart"):
            return await self._restart()
        command = arguments.get("command")
        if command is None:
            raise ToolExecutionError(
                self.name, "bash_session requires either 'command' or 'restart'"
            )
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._session.run, command, self._timeout_s)
        return self._format_result(result)

    def _new_session(self) -> BashSession:
        """Construct (but do not open) the backend session from config."""
        if self._service is None:
            return LocalBashSession()
        assert self._trial_id is not None and self._project_prefix is not None
        container = self._resolve_container_name(
            self._trial_id, self._service, self._project_prefix
        )
        return DockerComposeBashSession(container, user=self._user)

    @staticmethod
    def _resolve_container_name(trial_id: str, service: str, project_prefix: str) -> str:
        return _resolve_compose_container_name(trial_id, service, project_prefix)

    async def _restart(self) -> str:
        assert self._session is not None
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._session.close)
        session = self._new_session()
        await loop.run_in_executor(None, session.open, self._cwd)
        self._session = session
        return "Shell session restarted; working directory and environment reset."

    def _format_result(self, result: CommandResult) -> str:
        if result.timed_out:
            suffix = f"\n[timed out after {self._timeout_s:g}s; command terminated]"
            return result.output + suffix
        if result.exit_code not in (0, None):
            return f"{result.output}\n[exit code: {result.exit_code}]"
        return result.output


# =============================================================================
# Str-Replace Editor Tool Wrapper
# =============================================================================


class StrReplaceEditorToolWrapper(ToolWrapper):
    """Runner-side executor for the ``str_replace_editor`` tool.

    Stateless (no per-trial session), so ``has_lifecycle`` stays False. The
    working root defaults to ``/work`` (same directory the file tools and the
    shell's workdir target) and is overridable per task via
    ``tool_config.working_root``. A ``service`` key in ``tool_config`` selects
    the compose backend; its absence selects the local filesystem engine. The
    wire schema is identical either way.
    """

    has_lifecycle = False

    # Default working root when tool_config omits ``working_root``. Must equal
    # service.py's AGENT_WORK_DIR: this wrapper is has_lifecycle=False, so it
    # never sees ToolLifecycleContext.work_dir and cannot learn the root at
    # start() the way the lifecycle-managed shell does.
    _DEFAULT_WORKING_ROOT = "/work"

    def __init__(self, tool_schema: ToolSchemaModel, trial_id: str):
        super().__init__(tool_schema)
        tool_config = tool_schema.tool_config or {}
        self._service: str | None = tool_config.get("service")
        self._project_prefix: str | None = tool_config.get("compose_project_prefix")
        if self._service is not None and not self._project_prefix:
            raise ToolConfigurationError(
                self.name,
                "str_replace_editor with a 'service' requires 'compose_project_prefix' to "
                "resolve the running container name (the per-trial compose project "
                "prefix used to bring the stack up)",
            )
        self._working_root: str = tool_config.get("working_root", self._DEFAULT_WORKING_ROOT)
        # Optional ``docker exec --user <user>`` for the compose backend —
        # symmetric with ``PersistentShellToolWrapper._user``. Local backend
        # ignores it. Default ``None`` inherits the container's default
        # user and preserves prior behaviour.
        self._user: str | None = tool_config.get("user")
        self._trial_id = trial_id
        self._backend = self._new_backend()

    def _new_backend(self) -> EditorBackend:
        if self._service is None:
            return LocalFilesystemEditor(self._working_root)
        assert self._project_prefix is not None  # validated in __init__
        container = self._resolve_container_name(
            self._trial_id, self._service, self._project_prefix
        )
        return DockerComposeEditor(container, base_path=self._working_root, user=self._user)

    @staticmethod
    def _resolve_container_name(trial_id: str, service: str, project_prefix: str) -> str:
        return _resolve_compose_container_name(trial_id, service, project_prefix)

    async def execute(self, arguments: dict[str, Any]) -> str:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._dispatch, arguments)
        except EditorError as exc:
            raise ToolExecutionError(self.name, raised_tool_failure_text(exc)) from exc

    def _dispatch(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        path = arguments.get("path")
        if not path:
            raise EditorError("'path' is required")
        if command == "view":
            return self._backend.view(path, arguments.get("view_range"))
        if command == "create":
            file_text = arguments.get("file_text")
            if file_text is None:
                raise EditorError("'file_text' is required for the 'create' command")
            return self._backend.create(path, file_text)
        if command == "str_replace":
            old_str = arguments.get("old_str")
            new_str = arguments.get("new_str")
            if old_str is None or new_str is None:
                raise EditorError(
                    "'old_str' and 'new_str' are required for the 'str_replace' command"
                )
            return self._backend.str_replace(path, old_str, new_str)
        if command == "insert":
            insert_line = arguments.get("insert_line")
            insert_text = arguments.get("insert_text")
            if insert_line is None or insert_text is None:
                raise EditorError(
                    "'insert_line' and 'insert_text' are required for the 'insert' command"
                )
            return self._backend.insert(path, insert_line, insert_text)
        raise EditorError(
            f"unknown command: {command!r}; expected one of view/create/str_replace/insert"
        )


# =============================================================================
# Tool Factory
# =============================================================================


def _hint_source_configuration_missing(tool_name: str) -> str:
    """Actionable hint for a source-less non-builtin tool at runner emit time.

    The runner subset ships as a separate wheel and cannot import
    ``tolokaforge.adapters`` (runner-subset partition, canonically enforced
    in :mod:`tests.canonical.test_runner_subset_partition`), so the hint
    lists the two config keys and the fallback path without enumerating the
    adapter registry. The emit-time raise on :class:`NativeAdapter`
    (:mod:`tolokaforge.adapters.native`) carries the fuller hint with the
    registry enumeration and the detected-shape clause.
    """
    return (
        f"Tool '{tool_name}' has no source configuration and is not in the "
        "builtin registry. Set evaluation.harness_adapter.type in the run "
        "config to an adapter that emits source metadata for this tool, or "
        "declare the tool under tools.<actor>.mcp_server (or a per-tool "
        "source) in task.yaml."
    )


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
        id_fields: Mapping[str, str | list[str]] | None = None,
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
                               Used for declared-key matching and validation during
                               model registration.
            id_fields: Optional per-table primary-key overrides (table_name -> key
                       field or ordered component list), from grading config
                       state_checks.id_fields. Drives declared-key matching during
                       model registration and is forwarded to the DB proxy and to
                       TauSyncToolWrapper diff-sync so key resolution is
                       data-driven; a table absent resolves to ``"id"``.
        """
        self.db_client = db_client
        self.trial_id = trial_id
        self.rag_client = rag_client
        self.db_table_names = db_table_names or []
        self._initial_state_data = initial_state_data or {}
        self.id_fields: dict[str, str | list[str]] = dict(id_fields or {})
        self._claimed_tables: set[str] = set()

        # Create DB proxies for tools
        # Pass db_table_names so the proxy can resolve table names for unregistered models
        self._async_proxy = DBServiceProxy(
            db_client, trial_id, db_table_names=self.db_table_names, id_fields=self.id_fields
        )
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
        # Handle built-in tools (no source) — dispatch by name through
        # the unified registry. Eliminates the previous drift between
        # hardcoded name tuples and a separate factory dict.
        if schema.source is None:
            from tolokaforge.tools.builtin import registry as builtin_registry

            if not builtin_registry.is_builtin(schema.name):
                raise ToolConfigurationError(
                    schema.name, _hint_source_configuration_missing(schema.name)
                )
            dispatch = builtin_registry.get_dispatch(schema.name)
            if dispatch is builtin_registry.Dispatch.RAG:
                return self._create_rag_search_wrapper(schema)
            if dispatch is builtin_registry.Dispatch.FILES:
                return BuiltinFileToolWrapper(schema)
            if dispatch is builtin_registry.Dispatch.PERSISTENT_SHELL:
                return PersistentShellToolWrapper(schema)
            if dispatch is builtin_registry.Dispatch.EDITOR:
                return StrReplaceEditorToolWrapper(schema, trial_id=self.trial_id)
            return BuiltinGenericToolWrapper(schema)

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

        Import path: ``{source.toolset}.{source.module_path}`` — the adapter
        supplies the fully-qualified package; the runner does no prefixing.
        """
        module_path = f"{source.toolset}.{source.module_path}"
        try:
            module = importlib.import_module(module_path)
            tool_class = getattr(module, source.class_name)

            return TauSyncToolWrapper(
                tool_schema=schema,
                tool_class=tool_class,
                db_proxy=self._sync_proxy,
                id_fields=self.id_fields,
            )
        except ImportError as e:
            raise ToolImportError(schema.name, f"Cannot import module '{module_path}': {e}")
        except AttributeError as e:
            raise ToolImportError(
                schema.name,
                f"Class '{source.class_name}' not found in module '{module_path}': {e}",
            )

    def _create_mcp_async_wrapper(
        self, schema: ToolSchemaModel, source: ToolSourceModel
    ) -> MCPAsyncToolWrapper:
        """
        Create an MCP async tool wrapper.

        FAIL FAST: Raises ToolImportError if module/class cannot be imported.

        Import path: ``{source.toolset}.{source.module_path}`` — the adapter
        supplies the fully-qualified package; the runner does no prefixing.

        Note: MCP tools call db methods synchronously inside their async run()
        method, so we pass SyncDBServiceProxy instead of DBServiceProxy.

        Also registers model classes from the toolset with namespaced table names
        so that db.create(Ticket(...)) maps to 'zendesk_tickets' table.
        """
        module_path = f"{source.toolset}.{source.module_path}"
        try:
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
            raise ToolImportError(schema.name, f"Cannot import module '{module_path}': {e}")
        except AttributeError as e:
            raise ToolImportError(
                schema.name,
                f"Class '{source.class_name}' not found in module '{module_path}': {e}",
            )

    def _match_table_by_declared_key(
        self, model_cls: type[BaseModel], claimed_tables: set[str]
    ) -> str | None:
        """
        Find the seeded table whose declared primary key ``model_cls`` carries.

        A seeded table is a candidate when every component of its key — resolved
        from ``state_checks.id_fields``, defaulting to ``"id"`` — is both a field
        of the model and present in the table's first record. Candidates are
        tried with explicitly declared tables ahead of ``"id"``-defaulted ones,
        so a model carrying both a declared table's key and an incidental ``id``
        field claims the declared table; within each rank ``db_table_names``
        order decides. The first candidate whose first record validates against
        the model wins — validation is what discriminates two tables sharing a
        key shape.

        Args:
            model_cls: The model class to match
            claimed_tables: Table names already claimed by an earlier model

        Returns:
            The matching table name, or None if no candidate validates
        """
        declared: list[tuple[str, TableKey, dict[str, Any]]] = []
        defaulted: list[tuple[str, TableKey, dict[str, Any]]] = []
        for table in self.db_table_names:
            if table in claimed_tables:
                continue
            records = self._initial_state_data.get(table, [])
            if not records:
                continue
            key = table_key(table, self.id_fields)
            if any(f not in model_cls.model_fields or f not in records[0] for f in key.fields):
                continue
            rank = declared if self.id_fields.get(table) else defaulted
            rank.append((table, key, records[0]))

        for table, key, record in declared + defaulted:
            try:
                model_cls.model_validate(record)
            except Exception:
                logger.debug(
                    f"Declared key matched but validation failed: {model_cls.__name__} vs '{table}'"
                )
                continue
            logger.info(
                f"Matched {model_cls.__name__} to '{table}' via declared key "
                f"{list(key.fields)} + validation"
            )
            return table
        return None

    def _report_unregistered(self, model_cls: type[BaseModel], claimed_tables: set[str]) -> None:
        """
        Report what becomes of a model the registration pass could not match.

        The DB proxy's class-name fallback resolves many of them on first use,
        which is a working case rather than a failure: that outcome is reported
        at info, naming the table the proxy will reach, and only a model nothing
        will resolve draws a warning — with the declaration that would fix it.

        Args:
            model_cls: The model class no strategy matched
            claimed_tables: Table names already claimed by an earlier model
        """
        lazy_table = self._async_proxy.match_table_by_name(model_cls)
        if lazy_table is not None:
            logger.info(
                f"Model {model_cls.__name__} is not registered eagerly; the DB proxy "
                f"resolves it by class name to table '{lazy_table}' on first use"
            )
            return

        unclaimed = {
            t: list(table_key(t, self.id_fields).fields)
            for t in self.db_table_names
            if t not in claimed_tables
        }
        logger.warning(
            f"Cannot match model {model_cls.__name__} "
            f"(fields={sorted(model_cls.model_fields)}) to any table, and no table name "
            f"matches its class name. Skipping registration. "
            f"Unclaimed tables and their declared keys: {unclaimed}. "
            f"Declare the intended table's key under state_checks.id_fields to register it."
        )

    def _register_toolset_models(self, toolset: str) -> None:
        """
        Register Pydantic model classes from a toolset with actual DB table names.

        This is necessary because MCP tools use Pydantic models like Ticket,
        but the DB service stores them in tables like 'zendesk_tickets'.

        Uses a 4-strategy matching approach to find the correct table for each model:

        1. table_name ClassVar: If model has a table_name attribute, use it directly
           or find a table ending with that name.

        2. Declared-key matching (universal): Match the model against the primary
           key each seeded table declares under ``state_checks.id_fields``
           (default ``"id"``), single or composite — see
           :meth:`_match_table_by_declared_key`. Works for ANY domain without
           code changes, since the declaration travels with the task.

        3. Suffix matching (only for empty tables): Falls back to suffix matching
           only for tables with no records in initial_state.

        4. Report and skip: a model matching nothing is left unregistered — at
           info where the DB proxy's class-name fallback will resolve it on
           first use, at warning where nothing will.

        Args:
            toolset: The toolset path (e.g., 'consulting.zendesk', 'external_retail_toolset.oms')
        """
        # Only register once per toolset
        if hasattr(self, "_registered_toolsets"):
            if toolset in self._registered_toolsets:
                return
        else:
            self._registered_toolsets: set = set()

        try:
            # Try to import the models module from the toolset
            models_module_path = f"{toolset}.models"
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

                # Strategy 2: declared-key matching (universal)
                if matched_table is None:
                    matched_table = self._match_table_by_declared_key(model_cls, claimed_tables)

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

                # Strategy 4: report where the model lands, and skip (don't crash on
                # missing optional tables)
                if matched_table is None:
                    self._report_unregistered(model_cls, claimed_tables)
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

        except ModuleNotFoundError:
            # No models module — that's OK, some toolsets may not have one.
            # Note: ``ModuleNotFoundError`` is a subclass of ``ImportError`` raised
            # only when the module itself cannot be located. A genuine import error
            # *inside* an existing models module (e.g. a missing transitive dep)
            # raises plain ``ImportError`` and will propagate — surfacing the bug
            # instead of being silently logged at debug level.
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
        """Create a Docker Compose exec wrapper.

        FAIL FAST: Raises ToolConfigurationError if ``service`` or
        ``compose_project_prefix`` are missing from ``source.extra`` — the two
        fields the wrapper needs to resolve the container the per-trial runtime
        brought up.
        """
        extra = source.extra
        service = extra.get("service")
        compose_project_prefix = extra.get("compose_project_prefix")
        if not service or not compose_project_prefix:
            raise ToolConfigurationError(
                schema.name,
                "DOCKER_COMPOSE_EXEC requires 'service' and 'compose_project_prefix' "
                "in source.extra",
            )
        return DockerComposeExecToolWrapper(
            tool_schema=schema,
            service=service,
            compose_project_prefix=compose_project_prefix,
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
