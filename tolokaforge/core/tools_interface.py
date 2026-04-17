"""Domain Tool Registry — decorator-based API for native MCP tool servers.

Analogous to ``checks_interface.py`` (used for grading) but for tool
registration. A single ``@registry.tool()`` decorator replaces two separate
registrations that are otherwise needed:

1. Manual ``TOOLS`` dict entry  → grading contract
   ``GradingEngine`` calls ``TOOLS["name"].invoke(data=data, **kwargs)``

2. Manual ``@mcp.tool()`` wrapper → runtime MCP schema
   FastMCP generates ``inputSchema`` from type hints

Pattern comparison
------------------
checks_interface.py (grading):

    @check
    def order_was_created():
        ...

tools_interface.py (MCP tools):

    @registry.tool("Create a new order for a customer.")
    def place_order(data: dict, customer_id: str, ...) -> dict:
        ...

Both use a module-level registry populated at decoration time.

Usage in mcp_server.py
----------------------
::

    from core.tools_interface import DomainToolRegistry, ToolError

    registry = DomainToolRegistry(mcp, lambda: _STATE)
    TOOLS = registry.TOOLS          # grading reads this

    @registry.tool("List all products currently in stock.")
    def list_products(data: dict) -> list[dict]:
        return [p for p in data["products"] if p["stock"] > 0]

    @registry.tool("Retrieve a customer record by ID.")
    def get_customer(
        data: dict,
        customer_id: Annotated[str, Field(description="Customer ID")],
    ) -> dict:
        customer = next((c for c in data["customers"] if c["id"] == customer_id), None)
        if not customer:
            raise ToolError(f"Customer '{customer_id}' not found")
        return customer

Notes
-----
- The decorated function **must** accept ``data: dict`` as its first parameter.
  ``data`` is injected at runtime (from ``_STATE``) and excluded from the
  MCP ``inputSchema``. In grading mode the caller passes ``data`` explicitly.
- Raise ``ToolError`` instead of returning ``{"error": "..."}`` for business
  logic errors. The registry catches it and serialises it to the standard
  ``{"error": "...", "details": [...]}`` response format.
- Nested Pydantic models in kwargs are auto-coerced: if the grading engine
  passes raw dicts for a parameter typed ``list[SomeModel]`` or ``SomeModel``,
  the registry validates them before calling the function.
- Import via ``importlib`` or direct sys.path manipulation is recommended in
  ``mcp_server.py`` subprocess to avoid triggering the heavy
  ``tolokaforge/__init__.py`` import chain (litellm, orchestrator, etc.).
"""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# ToolError — typed business-logic error
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Business logic error raised inside a tool function.

    Raise instead of returning ``{"error": "..."}`` for explicit, structured
    error signalling.  ``DomainToolRegistry`` catches ``ToolError`` and
    converts it to ``{"error": str(e), "details": [...]}`` automatically,
    so the response format stays consistent across all tools.

    Parameters
    ----------
    message:
        Human-readable error description.
    details:
        Optional list of additional detail strings (e.g. field-level
        validation messages).

    Examples
    --------
    ::

        raise ToolError(f"Customer '{customer_id}' not found")
        raise ToolError("Insufficient stock", details=["P-001: need 5, have 2"])
        raise ToolError.from_exc("Payment gateway error", exc)
    """

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details: list[str] = details or []
        super().__init__(message)

    @classmethod
    def from_exc(cls, message: str, exc: Exception) -> ToolError:
        """Wrap an unexpected exception into a ``ToolError``."""
        return cls(message, [str(exc)])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unwrap_annotated(hint: Any) -> Any:
    """Strip ``Annotated[X, ...]`` wrapper and return ``X``."""
    if get_origin(hint) is Annotated:
        return get_args(hint)[0]
    return hint


def _tool_error_to_dict(exc: ToolError) -> dict[str, Any]:
    result: dict[str, Any] = {"error": str(exc)}
    if exc.details:
        result["details"] = exc.details
    return result


# ---------------------------------------------------------------------------
# DomainToolRegistry
# ---------------------------------------------------------------------------


class DomainToolRegistry:
    """Unified registry: one decorator → TOOLS dict + FastMCP registration.

    Parameters
    ----------
    mcp:
        FastMCP server instance (``mcp.server.fastmcp.FastMCP``).
    state:
        Zero-argument callable that returns the current ``_STATE`` dict at
        call time (e.g. ``lambda: _STATE``). Using a callable — not the dict
        directly — ensures the wrapper always sees the latest state even after
        ``_load_state()`` reassigns the module-level variable.
    """

    def __init__(self, mcp: Any, state: Callable[[], dict]) -> None:
        self.TOOLS: dict[str, Any] = {}
        self._mcp = mcp
        self._state = state
        self._register_internal_tools()

    def _register_internal_tools(self) -> None:
        """Register internal state-management tools for grading use.

        These tools are NOT added to the TOOLS dict and are never listed in a
        task's ``enabled`` tool list, so the LLM never sees them.  They are
        callable only via direct JSON-RPC (e.g. from MCPServerProcess.get_state /
        reset_state used by the Runner's hash-based grader).
        """
        state_getter = self._state

        def _tolokaforge_get_state_() -> str:
            """Internal: serialise current _STATE to JSON."""
            return json.dumps(state_getter())

        def _tolokaforge_set_state_(state_json: str) -> str:
            """Internal: replace _STATE contents from a JSON string."""
            new_state = json.loads(state_json)
            current = state_getter()
            current.clear()
            current.update(new_state)
            return json.dumps({"status": "ok", "tables": list(current.keys())})

        self._mcp.tool(description="Internal: get state snapshot")(_tolokaforge_get_state_)
        self._mcp.tool(description="Internal: replace state")(_tolokaforge_set_state_)

    def tool(self, description: str) -> Callable:
        """Decorator factory — registers ``func`` in both TOOLS and FastMCP.

        The decorated function **must** have ``data: dict`` as its first
        positional parameter. All remaining parameters become the tool's
        input schema (FastMCP auto-generates JSON Schema from their type hints
        and ``Annotated[..., Field(...)]`` metadata).

        Parameters
        ----------
        description:
            Human-readable description forwarded to FastMCP and shown to the
            LLM agent as the tool description.

        Example
        -------
        ::

            class OrderItem(BaseModel):
                product_id: Annotated[str, Field(description="Product ID")]
                quantity:   Annotated[int, Field(ge=1)]

            @registry.tool("Create a new order.")
            def place_order(
                data: dict,
                customer_id: Annotated[str, Field(description="Customer ID")],
                items: Annotated[list[OrderItem], Field(description="Items")],
            ) -> dict:
                customer = next(...)
                if not customer:
                    raise ToolError(f"Customer '{customer_id}' not found")
                ...
        """

        def decorator(func: Callable) -> Callable:
            tool_name = func.__name__
            state_getter = self._state

            # ------------------------------------------------------------------
            # 1. Grading class
            #    GradingEngine calls: TOOLS["name"].invoke(data=data, **kwargs)
            #    Grading passes raw YAML dicts; MCP runtime passes typed objects
            #    (FastMCP coerces them). Bridge the gap by calling model_validate
            #    only when we actually see a dict where a model is expected.
            # ------------------------------------------------------------------
            _hints = get_type_hints(func, include_extras=True)

            class _ToolClass:
                @staticmethod
                def invoke(data: dict, **kwargs: Any) -> Any:
                    coerced: dict[str, Any] = {}
                    for name, value in kwargs.items():
                        hint = _unwrap_annotated(_hints.get(name))
                        if hint is not None:
                            if get_origin(hint) is list:
                                args = get_args(hint)
                                item_type = _unwrap_annotated(args[0]) if args else None
                                if item_type and hasattr(item_type, "model_validate"):
                                    if isinstance(value, list) and any(
                                        isinstance(v, dict) for v in value
                                    ):
                                        value = [
                                            (
                                                item_type.model_validate(v)
                                                if isinstance(v, dict)
                                                else v
                                            )
                                            for v in value
                                        ]
                            elif hasattr(hint, "model_validate") and isinstance(value, dict):
                                value = hint.model_validate(value)
                        coerced[name] = value
                    try:
                        return func(data, **coerced)
                    except ToolError as exc:
                        return _tool_error_to_dict(exc)

            _ToolClass.__name__ = tool_name
            _ToolClass.__qualname__ = tool_name
            self.TOOLS[tool_name] = _ToolClass
            sig = inspect.signature(func)
            new_params = [p for name, p in sig.parameters.items() if name != "data"]
            new_sig = sig.replace(parameters=new_params)
            new_annotations = {k: v for k, v in func.__annotations__.items() if k != "data"}

            def _mcp_fn(**kwargs: Any) -> Any:
                try:
                    return func(state_getter(), **kwargs)
                except ToolError as exc:
                    return _tool_error_to_dict(exc)

            _mcp_fn.__name__ = tool_name
            _mcp_fn.__qualname__ = tool_name
            _mcp_fn.__doc__ = func.__doc__
            _mcp_fn.__signature__ = new_sig
            _mcp_fn.__annotations__ = new_annotations

            self._mcp.tool(description=description)(_mcp_fn)

            return func

        return decorator


# ---------------------------------------------------------------------------
# Task-server bootstrap hook
# ---------------------------------------------------------------------------


def setup_task_server(caller_file: str) -> type[DomainToolRegistry]:
    """Bootstrap hook for task ``mcp_server.py`` scripts."""
    task_dir = str(Path(caller_file).parent)
    pkg_root = str(Path(__file__).parent.parent)

    # task_dir must be at index 0 so the task's own tools/ package takes
    # precedence over tolokaforge's built-in tools/ package (which lives
    # ("python mcp_server.py") it pre-populates sys.path[0] with the script
    # directory — so a simple "insert if absent" loop would put pkg_root
    # *before* task_dir.  We therefore always move task_dir to position 0.
    if task_dir in sys.path:
        sys.path.remove(task_dir)
    sys.path.insert(0, task_dir)

    if pkg_root not in sys.path:
        sys.path.insert(1, pkg_root)

    # Register alias so tool files can use `from core.tools_interface import ToolError`
    # regardless of whether tolokaforge was loaded as an installed package
    # (tolokaforge.core.tools_interface) or directly by path (core.tools_interface).
    sys.modules.setdefault("core.tools_interface", sys.modules[__name__])

    return DomainToolRegistry


def create_server(
    caller_file: str,
    server_name: str,
    state_file: str = "initial_state.json",
) -> tuple[Any, DomainToolRegistry, dict]:
    """Create a fully wired MCP server for a task directory.

    Combines ``setup_task_server`` + ``FastMCP`` + ``DomainToolRegistry`` into
    a single call so individual ``mcp_server.py`` files contain only what is
    unique to that task: the server name and the ``register_all`` import.

    Parameters
    ----------
    caller_file:
        Pass ``__file__`` from the calling ``mcp_server.py``.  Used both to
        locate the task directory (for ``state_file`` resolution) and to
        configure ``sys.path`` via ``setup_task_server``.
    server_name:
        Name passed to ``FastMCP`` (shown in tool listings).
    state_file:
        Path to the initial-state JSON file, relative to the task directory.
        Defaults to ``"initial_state.json"``.

    Returns
    -------
    tuple[FastMCP, DomainToolRegistry, dict]
        ``(mcp, registry, TOOLS)`` — assign all three at module level so
        grading can reach ``TOOLS`` and the entry point can call ``mcp.run()``.

    Typical usage in ``mcp_server.py``::

        from tolokaforge.core.tools_interface import create_server

        mcp, registry, TOOLS = create_server(__file__, "my-server")

        from tools import register_all  # noqa: E402
        register_all(registry)

        if __name__ == "__main__":
            mcp.run(transport="stdio")
    """
    setup_task_server(caller_file)

    task_dir = Path(caller_file).parent
    _STATE: dict[str, Any] = {}

    def _load_state() -> None:
        resolved = task_dir / state_file
        loaded: dict[str, Any]
        if resolved.exists():
            with open(resolved) as f:
                loaded = json.load(f)
        else:
            loaded = {}
        _STATE.clear()
        _STATE.update(loaded)

    @asynccontextmanager
    async def _lifespan(server: Any):  # type: ignore[override]
        """Load DB state when the server starts; skipped on grading import."""
        _load_state()
        yield

    mcp = FastMCP(server_name, lifespan=_lifespan)
    registry = DomainToolRegistry(mcp, lambda: _STATE)
    return mcp, registry, registry.TOOLS
