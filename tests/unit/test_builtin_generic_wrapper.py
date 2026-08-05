"""Tests for BuiltinGenericToolWrapper — the runner-side dispatcher for
builtin tools that aren't filesystem tools or RAG search.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _make_schema(name: str) -> MagicMock:
    schema = MagicMock()
    schema.name = name
    schema.timeout_s = 30.0
    return schema


class TestBuiltinGenericToolWrapper:
    """Verify the wrapper instantiates known builtin tools and propagates failures."""

    def test_unknown_tool_name_raises_configuration_error(self):
        from tolokaforge.runner.tool_factory import (
            BuiltinGenericToolWrapper,
            ToolConfigurationError,
        )

        with pytest.raises(ToolConfigurationError):
            BuiltinGenericToolWrapper(_make_schema("not_a_real_tool"))

    def test_factory_registry_covers_expected_builtins(self):
        from tolokaforge.tools.builtin import registry

        # The unified registry must list every runtime-builtin tool that
        # tasks declare without a `source` block. Routes to GENERIC.
        expected = {
            "bash",
            "calculator",
            "browser",
            "http_request",
            "build_check",
            "mobile",
            "db_query",
            "db_update",
        }
        assert expected.issubset(registry.list_for_dispatch(registry.Dispatch.GENERIC))

    def test_calculator_wrapper_constructs_real_tool(self):
        # Calculator is the cheapest tool to instantiate (no IO, no globals).
        from tolokaforge.runner.tool_factory import BuiltinGenericToolWrapper

        wrapper = BuiltinGenericToolWrapper(_make_schema("calculator"))
        assert wrapper._tool.__class__.__name__ == "CalculatorTool"

    def test_build_check_wrapper_splats_tool_config_into_init(self):
        """``build_check`` is the first builtin whose ``__init__`` has a
        required kwarg (``service``). Locks the wrapper's tool_config →
        ``__init__`` splat: the endpoint URL threads through from the
        task-authored config to the live tool instance."""
        from tolokaforge.runner.models import ToolSchema
        from tolokaforge.runner.tool_factory import BuiltinGenericToolWrapper

        schema = ToolSchema(
            name="build_check",
            description="x",
            parameters={"type": "object"},
            tool_config={"service": "mb-grade", "port": 9001, "path": "/probe"},
        )
        wrapper = BuiltinGenericToolWrapper(schema)
        assert wrapper._tool.__class__.__name__ == "BuildCheckTool"
        assert wrapper._tool.endpoint_url == "http://mb-grade:9001/probe"

    def test_build_check_wrapper_rejects_unknown_tool_config_key(self):
        """A typo in ``tool_config`` fails loud at trial registration
        rather than getting silently dropped."""
        from tolokaforge.runner.models import ToolSchema
        from tolokaforge.runner.tool_factory import (
            BuiltinGenericToolWrapper,
            ToolConfigurationError,
        )

        schema = ToolSchema(
            name="build_check",
            description="x",
            parameters={"type": "object"},
            tool_config={"service": "peer", "prot": 8001},  # ``prot`` is a typo
        )
        with pytest.raises(ToolConfigurationError, match="prot"):
            BuiltinGenericToolWrapper(schema)

    @pytest.mark.asyncio
    async def test_execute_raises_on_tool_failure(self):
        """Mirrors the BuiltinFileToolWrapper contract: failure => raise."""
        from tolokaforge.runner.tool_factory import (
            BuiltinGenericToolWrapper,
            ToolExecutionError,
        )
        from tolokaforge.tools.registry import ToolResult

        wrapper = BuiltinGenericToolWrapper(_make_schema("calculator"))
        failing_tool = MagicMock()
        failing_tool.execute.return_value = ToolResult(
            success=False, output="", error="Division by zero"
        )
        wrapper._tool = failing_tool

        with pytest.raises(ToolExecutionError) as exc_info:
            await wrapper.execute({"expression": "1/0"})

        assert exc_info.value.tool_name == "calculator"
        assert "Division by zero" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_execute_returns_output_on_success(self):
        from tolokaforge.runner.tool_factory import BuiltinGenericToolWrapper
        from tolokaforge.tools.registry import ToolResult

        wrapper = BuiltinGenericToolWrapper(_make_schema("calculator"))
        success_tool = MagicMock()
        success_tool.execute.return_value = ToolResult(success=True, output="42")
        wrapper._tool = success_tool

        result = await wrapper.execute({"expression": "6*7"})
        assert result == "42"


class TestToolFactoryDispatch:
    """The ToolFactory dispatch routes source-less tools to the right wrapper."""

    def test_dispatch_routes_list_dir_to_file_wrapper(self):
        from tolokaforge.runner.models import ToolSchema
        from tolokaforge.runner.tool_factory import (
            BuiltinFileToolWrapper,
            ToolFactory,
        )

        schema = ToolSchema(name="list_dir", description="x", parameters={"type": "object"})
        factory = ToolFactory(MagicMock(), "trial-1")
        wrapper = factory._create_wrapper(schema)
        assert isinstance(wrapper, BuiltinFileToolWrapper)

    def test_dispatch_routes_calculator_to_generic_wrapper(self):
        from tolokaforge.runner.models import ToolSchema
        from tolokaforge.runner.tool_factory import (
            BuiltinGenericToolWrapper,
            ToolFactory,
        )

        schema = ToolSchema(name="calculator", description="x", parameters={"type": "object"})
        factory = ToolFactory(MagicMock(), "trial-1")
        wrapper = factory._create_wrapper(schema)
        assert isinstance(wrapper, BuiltinGenericToolWrapper)
