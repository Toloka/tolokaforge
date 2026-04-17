"""Unit tests for DomainToolRegistry.tool() decorator (tools_interface.py lines 223-292)
and the bootstrap helpers setup_task_server / create_server.

Covers:
- _ToolClass.invoke: plain pass-through, Pydantic model coercion, list coercion,
  mixed list (model + already-coerced), ToolError serialisation.
- _mcp_fn: delegates to original function with state_getter(); serialises ToolError.
- Decorator side-effects: TOOLS entry, class naming, MCP registration (exact count),
  signature stripping of 'data', annotation forwarding, docstring propagation.
- setup_task_server: sys.path ordering, pkg_root insertion, sys.modules alias.
- create_server: return types, TOOLS identity, state file loading, missing state file.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import BaseModel, Field

from tolokaforge.core.tools_interface import (
    DomainToolRegistry,
    ToolError,
    create_server,
    setup_task_server,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_registry() -> tuple[DomainToolRegistry, MagicMock]:
    """Return a registry wired to a mock FastMCP instance."""
    mock_mcp = MagicMock()
    # mcp.tool(description=...) returns a decorator — make it a no-op
    mock_mcp.tool.return_value = lambda fn: fn
    state: dict[str, Any] = {"products": [{"id": "P1", "name": "Widget"}]}
    registry = DomainToolRegistry(mock_mcp, lambda: state)
    return registry, mock_mcp, state


class ItemModel(BaseModel):
    product_id: Annotated[str, Field(description="Product ID")]
    quantity: Annotated[int, Field(ge=1)]


# ---------------------------------------------------------------------------
# _ToolClass.invoke — core grading path
# ---------------------------------------------------------------------------


class TestInvokePlainKwargs:
    """invoke() with no Pydantic parameters — values passed through unchanged."""

    def setup_method(self):
        self.registry, self.mock_mcp, self.state = _make_registry()

        @self.registry.tool("Echo name and count.")
        def echo(data: dict, name: str, count: int) -> dict:
            return {"name": name, "count": count, "table_count": len(data)}

    def test_returns_correct_result(self):
        result = self.registry.TOOLS["echo"].invoke(data=self.state, name="Alice", count=3)
        assert result == {"name": "Alice", "count": 3, "table_count": 1}

    def test_data_injected_from_argument(self):
        empty_data: dict = {}

        @self.registry.tool("Return data size.")
        def data_size(data: dict) -> int:
            return len(data)

        assert self.registry.TOOLS["data_size"].invoke(data=empty_data) == 0
        assert self.registry.TOOLS["data_size"].invoke(data=self.state) == 1


class TestInvokeModelCoercion:
    """invoke() auto-coerces dict → Pydantic model."""

    def setup_method(self):
        self.registry, self.mock_mcp, self.state = _make_registry()

        @self.registry.tool("Accept a single item.")
        def accept_item(data: dict, item: ItemModel) -> dict:
            return {"id": item.product_id, "qty": item.quantity}

    def test_dict_is_coerced_to_model(self):
        result = self.registry.TOOLS["accept_item"].invoke(
            data=self.state, item={"product_id": "P1", "quantity": 2}
        )
        assert result == {"id": "P1", "qty": 2}

    def test_already_model_passes_through(self):
        item = ItemModel(product_id="P2", quantity=5)
        result = self.registry.TOOLS["accept_item"].invoke(data=self.state, item=item)
        assert result == {"id": "P2", "qty": 5}

    def test_annotated_model_param_is_coerced(self):
        @self.registry.tool("Annotated model param.")
        def accept_annotated(
            data: dict,
            item: Annotated[ItemModel, Field(description="An item")],
        ) -> str:
            return item.product_id

        result = self.registry.TOOLS["accept_annotated"].invoke(
            data=self.state, item={"product_id": "PA", "quantity": 1}
        )
        assert result == "PA"


class TestInvokeListCoercion:
    """invoke() auto-coerces list[dict] → list[SomeModel]."""

    def setup_method(self):
        self.registry, self.mock_mcp, self.state = _make_registry()

        @self.registry.tool("Accept a list of items.")
        def accept_items(data: dict, items: list[ItemModel]) -> list[str]:
            return [i.product_id for i in items]

    def test_list_of_dicts_coerced(self):
        result = self.registry.TOOLS["accept_items"].invoke(
            data=self.state,
            items=[{"product_id": "A", "quantity": 1}, {"product_id": "B", "quantity": 2}],
        )
        assert result == ["A", "B"]

    def test_list_already_models_passes_through(self):
        items = [ItemModel(product_id="X", quantity=1), ItemModel(product_id="Y", quantity=3)]
        result = self.registry.TOOLS["accept_items"].invoke(data=self.state, items=items)
        assert result == ["X", "Y"]

    def test_mixed_list_coerces_only_dicts(self):
        already = ItemModel(product_id="KEPT", quantity=1)
        result = self.registry.TOOLS["accept_items"].invoke(
            data=self.state,
            items=[{"product_id": "NEW", "quantity": 2}, already],
        )
        assert result == ["NEW", "KEPT"]

    def test_empty_list_passes_through(self):
        result = self.registry.TOOLS["accept_items"].invoke(data=self.state, items=[])
        assert result == []

    def test_list_of_plain_scalars_not_coerced(self):
        """list[str] has no model_validate; values must remain unchanged."""

        @self.registry.tool("String list.")
        def tag_list(data: dict, tags: list[str]) -> list[str]:
            return tags

        result = self.registry.TOOLS["tag_list"].invoke(data=self.state, tags=["a", "b"])
        assert result == ["a", "b"]


class TestInvokeToolError:
    """invoke() catches ToolError and serialises it to a dict."""

    def setup_method(self):
        self.registry, self.mock_mcp, self.state = _make_registry()

    def test_tool_error_without_details(self):
        @self.registry.tool("May fail.")
        def always_fail(data: dict) -> dict:
            raise ToolError("Something went wrong")

        result = self.registry.TOOLS["always_fail"].invoke(data=self.state)
        assert result == {"error": "Something went wrong"}

    def test_tool_error_with_details(self):
        @self.registry.tool("May fail with details.")
        def fail_with_details(data: dict) -> dict:
            raise ToolError("Bad input", details=["field x: required", "field y: invalid"])

        result = self.registry.TOOLS["fail_with_details"].invoke(data=self.state)
        assert result == {
            "error": "Bad input",
            "details": ["field x: required", "field y: invalid"],
        }

    def test_non_tool_error_propagates(self):
        @self.registry.tool("Raises unexpected error.")
        def raises_value_error(data: dict) -> dict:
            raise ValueError("unexpected")

        with pytest.raises(ValueError, match="unexpected"):
            self.registry.TOOLS["raises_value_error"].invoke(data=self.state)


# ---------------------------------------------------------------------------
# _mcp_fn — MCP runtime path
# ---------------------------------------------------------------------------


class TestMcpFn:
    """The _mcp_fn closure uses state_getter() as data and catches ToolError."""

    def setup_method(self):
        self.mock_mcp = MagicMock()
        self.captured_fn: Any = None

        def capture_decorator(description):
            def inner(fn):
                self.captured_fn = fn
                return fn

            return inner

        self.mock_mcp.tool.side_effect = capture_decorator
        self.state: dict[str, Any] = {"key": "val"}
        self.registry = DomainToolRegistry(self.mock_mcp, lambda: self.state)

    def _register(self, func):
        self.registry.tool("desc")(func)
        return self.captured_fn

    def test_mcp_fn_injects_current_state(self):
        @self.registry.tool("Use state.")
        def use_state(data: dict) -> str:
            return data["key"]

        mcp_fn = self.captured_fn
        assert mcp_fn() == "val"

    def test_mcp_fn_reflects_state_mutation(self):
        @self.registry.tool("Check state key.")
        def get_key(data: dict) -> str:
            return data.get("key", "missing")

        mcp_fn = self.captured_fn
        assert mcp_fn() == "val"
        self.state["key"] = "updated"
        assert mcp_fn() == "updated"

    def test_mcp_fn_tool_error_serialised(self):
        @self.registry.tool("Fail via MCP path.")
        def mcp_fail(data: dict) -> dict:
            raise ToolError("MCP error", details=["detail"])

        mcp_fn = self.captured_fn
        result = mcp_fn()
        assert result == {"error": "MCP error", "details": ["detail"]}

    def test_mcp_fn_passes_kwargs(self):
        @self.registry.tool("Pass kwargs.")
        def add(data: dict, x: int, y: int) -> int:
            return x + y

        mcp_fn = self.captured_fn
        assert mcp_fn(x=3, y=4) == 7


# ---------------------------------------------------------------------------
# Decorator side-effects
# ---------------------------------------------------------------------------


class TestDecoratorSideEffects:
    """Verify structural guarantees made by the decorator."""

    def setup_method(self):
        self.mock_mcp = MagicMock()
        self.mock_mcp.tool.return_value = lambda fn: fn
        self.registry = DomainToolRegistry(self.mock_mcp, lambda: {})

    def test_tool_registered_in_tools_dict(self):
        @self.registry.tool("Simple tool.")
        def my_tool(data: dict) -> str:
            return "ok"

        assert "my_tool" in self.registry.TOOLS

    def test_tool_class_name_matches_function(self):
        @self.registry.tool("Named tool.")
        def named_tool(data: dict) -> str:
            return "ok"

        cls = self.registry.TOOLS["named_tool"]
        assert cls.__name__ == "named_tool"
        assert cls.__qualname__ == "named_tool"

    def test_original_function_returned_by_decorator(self):
        def my_fn(data: dict) -> str:
            return "x"

        result = self.registry.tool("Return check.")(my_fn)
        assert result is my_fn

    def test_mcp_registration_called_once_per_tool(self):
        # __init__ already called mcp.tool twice (internal tools); record baseline
        count_before = self.mock_mcp.tool.call_count

        @self.registry.tool("One tool.")
        def one_tool(data: dict) -> str:
            return "x"

        assert self.mock_mcp.tool.call_count == count_before + 1
        assert self.mock_mcp.tool.call_args == call(description="One tool.")

    def test_data_stripped_from_mcp_signature(self):
        captured: list[Any] = []
        self.mock_mcp.tool.return_value = lambda fn: captured.append(fn) or fn

        @self.registry.tool("Sig test.")
        def sig_tool(data: dict, name: str, count: int) -> str:
            return name

        mcp_fn = captured[-1]
        params = list(inspect.signature(mcp_fn).parameters)
        assert "data" not in params
        assert "name" in params
        assert "count" in params

    def test_data_stripped_from_mcp_annotations(self):
        captured: list[Any] = []
        self.mock_mcp.tool.return_value = lambda fn: captured.append(fn) or fn

        @self.registry.tool("Anno test.")
        def anno_tool(data: dict, value: int) -> int:
            return value

        mcp_fn = captured[-1]
        assert "data" not in mcp_fn.__annotations__
        assert "value" in mcp_fn.__annotations__

    def test_docstring_propagated_to_mcp_fn(self):
        captured: list[Any] = []
        self.mock_mcp.tool.return_value = lambda fn: captured.append(fn) or fn

        @self.registry.tool("Doc test.")
        def doc_tool(data: dict) -> str:
            """My docstring."""
            return "x"

        mcp_fn = captured[-1]
        assert mcp_fn.__doc__ == "My docstring."

    def test_multiple_tools_registered_independently(self):
        @self.registry.tool("Tool A.")
        def tool_a(data: dict) -> str:
            return "a"

        @self.registry.tool("Tool B.")
        def tool_b(data: dict) -> str:
            return "b"

        assert "tool_a" in self.registry.TOOLS
        assert "tool_b" in self.registry.TOOLS
        assert self.registry.TOOLS["tool_a"] is not self.registry.TOOLS["tool_b"]


# ---------------------------------------------------------------------------
# setup_task_server
# ---------------------------------------------------------------------------


class TestSetupTaskServer:
    """setup_task_server configures sys.path and sys.modules, returns DomainToolRegistry."""

    def setup_method(self):
        self._original_path = sys.path[:]
        self._original_modules = dict(sys.modules)

    def teardown_method(self):
        sys.path[:] = self._original_path
        # Remove keys added during the test
        for key in list(sys.modules):
            if key not in self._original_modules:
                del sys.modules[key]

    def test_returns_domain_tool_registry_class(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        result = setup_task_server(str(fake_server))
        assert result is DomainToolRegistry

    def test_task_dir_placed_at_index_zero(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        setup_task_server(str(fake_server))
        assert sys.path[0] == str(tmp_path)

    def test_task_dir_moved_to_front_if_already_present(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        task_dir = str(tmp_path)
        # Pre-insert somewhere in the middle
        sys.path.append(task_dir)
        old_count = sys.path.count(task_dir)
        setup_task_server(str(fake_server))
        assert sys.path[0] == task_dir
        # No duplicates introduced
        assert sys.path.count(task_dir) == old_count

    def test_pkg_root_inserted_when_absent(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        pkg_root = str(Path(__file__).parent.parent.parent / "tolokaforge")
        if pkg_root in sys.path:
            sys.path.remove(pkg_root)
        setup_task_server(str(fake_server))
        assert pkg_root in sys.path

    def test_pkg_root_not_duplicated_when_already_present(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        pkg_root = str(Path(__file__).parent.parent.parent / "tolokaforge")
        if pkg_root not in sys.path:
            sys.path.append(pkg_root)
        count_before = sys.path.count(pkg_root)
        setup_task_server(str(fake_server))
        assert sys.path.count(pkg_root) == count_before

    def test_core_tools_interface_alias_registered(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        sys.modules.pop("core.tools_interface", None)
        setup_task_server(str(fake_server))
        import tolokaforge.core.tools_interface as real_mod

        assert sys.modules["core.tools_interface"] is real_mod

    def test_alias_not_overwritten_if_already_set(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        sentinel = MagicMock()
        sys.modules["core.tools_interface"] = sentinel
        setup_task_server(str(fake_server))
        assert sys.modules["core.tools_interface"] is sentinel


# ---------------------------------------------------------------------------
# create_server
# ---------------------------------------------------------------------------


class TestCreateServer:
    """create_server returns (FastMCP, DomainToolRegistry, TOOLS dict)."""

    def setup_method(self):
        self._original_path = sys.path[:]
        self._original_modules = dict(sys.modules)

    def teardown_method(self):
        sys.path[:] = self._original_path
        for key in list(sys.modules):
            if key not in self._original_modules:
                del sys.modules[key]

    def test_returns_three_tuple(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        result = create_server(str(fake_server), "test-server")
        assert len(result) == 3

    def test_second_element_is_registry(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        _, registry, _ = create_server(str(fake_server), "test-server")
        assert isinstance(registry, DomainToolRegistry)

    def test_third_element_is_registry_tools(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        _, registry, TOOLS = create_server(str(fake_server), "test-server")
        assert TOOLS is registry.TOOLS

    def test_state_empty_when_no_state_file(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()
        _, registry, _ = create_server(str(fake_server), "test-server")
        # State getter returns the live _STATE dict; before lifespan runs it's empty
        state = registry._state()
        assert state == {}

    def _create_server_capturing_lifespan(self, fake_server_path: str, server_name: str, **kwargs):
        """Call create_server while intercepting the lifespan passed to FastMCP.

        FastMCP internals are not touched: we patch the constructor to capture
        the ``lifespan=`` kwarg before it disappears inside the object, then
        delegate to the real FastMCP so the rest of create_server works normally.
        """
        from mcp.server.fastmcp import FastMCP as RealFastMCP

        captured: dict[str, Any] = {}
        original_init = RealFastMCP.__init__

        def patched_init(self_mcp, name, *, lifespan=None, **kw):
            captured["lifespan"] = lifespan
            original_init(self_mcp, name, lifespan=lifespan, **kw)

        with patch.object(RealFastMCP, "__init__", patched_init):
            mcp, registry, TOOLS = create_server(fake_server_path, server_name, **kwargs)

        return mcp, registry, TOOLS, captured["lifespan"]

    @pytest.mark.asyncio
    async def test_lifespan_loads_state_file(self, tmp_path):
        state_data = {"orders": [{"id": "O1"}], "customers": []}
        (tmp_path / "initial_state.json").write_text(json.dumps(state_data))
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()

        _, registry, _, lifespan = self._create_server_capturing_lifespan(
            str(fake_server), "test-server"
        )

        async with lifespan(None):
            state = registry._state()
            assert state["orders"] == [{"id": "O1"}]
            assert state["customers"] == []

    @pytest.mark.asyncio
    async def test_lifespan_falls_back_to_empty_when_file_missing(self, tmp_path):
        fake_server = tmp_path / "mcp_server.py"
        fake_server.touch()

        _, registry, _, lifespan = self._create_server_capturing_lifespan(
            str(fake_server), "test-server", state_file="no_such_file.json"
        )

        async with lifespan(None):
            assert registry._state() == {}
