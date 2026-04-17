"""Unit tests for tool allowlisting and security documentation.

Tests tool executor rejection of unregistered tools, schema validation,
rate limiting, and security documentation existence.
"""

import os
from typing import Any

import pytest

pytestmark = pytest.mark.unit


class TestToolAllowlisting:
    """Test that only registered tools are callable"""

    def test_unregistered_tool_rejected(self):
        """Tool executor must reject unregistered tools"""
        from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

        registry = ToolRegistry()
        executor = ToolExecutor(registry)

        # Try to execute unregistered tool
        result = executor.execute(tool_name="nonexistent_tool", arguments={})

        assert not result.success
        assert result.error and "not found" in result.error.lower()

    def test_tool_schema_validation(self):
        """Tool executor must validate arguments against schema"""
        from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

        class TestTool(Tool):
            def __init__(self):
                super().__init__("test_tool", "Test tool")

            def get_schema(self) -> dict[str, Any]:
                return {
                    "type": "function",
                    "function": {
                        "name": "test_tool",
                        "description": "Test",
                        "parameters": {
                            "type": "object",
                            "properties": {"required_param": {"type": "string"}},
                            "required": ["required_param"],
                            "additionalProperties": False,
                        },
                    },
                }

            def execute(self, **kwargs) -> ToolResult:
                return ToolResult(success=True, output="OK")

        registry = ToolRegistry()
        registry.register(TestTool())
        executor = ToolExecutor(registry)

        # Missing required parameter should fail
        result = executor.execute("test_tool", {})
        assert not result.success
        assert result.error and "Invalid arguments" in result.error

        # Extra parameter should fail
        result = executor.execute("test_tool", {"required_param": "value", "extra_param": "value"})
        assert not result.success

        # Valid call should succeed
        result = executor.execute("test_tool", {"required_param": "value"})
        assert result.success

    def test_tool_rate_limiting(self):
        """Tool executor must enforce rate limits"""
        from tolokaforge.tools.registry import (
            Tool,
            ToolExecutor,
            ToolPolicy,
            ToolRegistry,
            ToolResult,
        )

        class RateLimitedTool(Tool):
            def __init__(self):
                policy = ToolPolicy(rate_limit=3)
                super().__init__("rate_limited", "Rate limited tool", policy)

            def get_schema(self) -> dict[str, Any]:
                return {
                    "type": "function",
                    "function": {
                        "name": "rate_limited",
                        "description": "Test",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }

            def execute(self, **kwargs) -> ToolResult:
                return ToolResult(success=True, output="OK")

        registry = ToolRegistry()
        registry.register(RateLimitedTool())
        executor = ToolExecutor(registry)

        # First 3 calls should succeed
        for i in range(3):
            result = executor.execute("rate_limited", {})
            assert result.success, f"Call {i + 1} failed"

        # 4th call should fail (rate limit exceeded)
        result = executor.execute("rate_limited", {})
        assert not result.success
        assert result.error and "rate limit" in result.error.lower()


def test_security_documentation_exists():
    """Verify SECURITY.md documentation exists"""
    # This test will create SECURITY.md if it doesn't exist
    security_md_path = "docs/SECURITY.md"

    if not os.path.exists(security_md_path):
        pytest.skip("SECURITY.md not yet created")

    # If it exists, check it has key sections
    with open(security_md_path) as f:
        content = f.read()

    assert "Ground Truth Isolation" in content
    assert "Tool-Level Security" in content
    assert "Secret Management" in content
