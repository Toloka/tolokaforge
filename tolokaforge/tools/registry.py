"""MCP-based tool registry and executor"""

import copy
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import Any

from jsonschema import validate
from pydantic import BaseModel


def convert_nullable_to_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Convert OpenAPI-style 'nullable' keyword to JSON Schema format.

    OpenAPI 3.0 uses {"type": "string", "nullable": True}
    JSON Schema uses {"type": ["string", "null"]} or {"anyOf": [..., {"type": "null"}]}

    This function recursively converts the schema for proper validation.
    """
    if not isinstance(schema, dict):
        return schema

    # Make a copy to avoid mutating the original
    schema = copy.deepcopy(schema)

    # Process nullable field
    if schema.pop("nullable", False):
        original_type = schema.get("type")
        if original_type:
            # Convert single type to array with null
            if isinstance(original_type, str):
                schema["type"] = [original_type, "null"]
            elif isinstance(original_type, list) and "null" not in original_type:
                schema["type"] = original_type + ["null"]
        else:
            # No type specified but nullable - add null to anyOf/oneOf if present
            # Otherwise just allow null through other means
            pass

    # Recursively process nested schemas
    if "properties" in schema:
        schema["properties"] = {
            k: convert_nullable_to_json_schema(v) for k, v in schema["properties"].items()
        }

    if "items" in schema:
        schema["items"] = convert_nullable_to_json_schema(schema["items"])

    if "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
        schema["additionalProperties"] = convert_nullable_to_json_schema(
            schema["additionalProperties"]
        )

    if "anyOf" in schema:
        schema["anyOf"] = [convert_nullable_to_json_schema(s) for s in schema["anyOf"]]

    if "oneOf" in schema:
        schema["oneOf"] = [convert_nullable_to_json_schema(s) for s in schema["oneOf"]]

    if "allOf" in schema:
        schema["allOf"] = [convert_nullable_to_json_schema(s) for s in schema["allOf"]]

    return schema


def sanitize_property_name(name: str) -> str:
    """
    Sanitize property name to match OpenRouter/OpenAI API requirements.

    OpenRouter requires property names to match pattern: ^[a-zA-Z0-9_.-]+$

    Transformations:
    - Replace $ prefix: $filter → filter
    - Replace @ prefix: @odata.id → odata_id
    - Replace spaces: "Document Library" → "Document_Library"
    - Replace any char not matching [a-zA-Z0-9_.-] with _
    """
    # Strip leading $ and @ characters
    sanitized = name.lstrip("$@")
    # Replace any invalid characters with underscore
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", sanitized)
    return sanitized


def sanitize_schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively sanitize property names in a JSON schema to match API requirements.

    This ensures all property names in the schema conform to the pattern ^[a-zA-Z0-9_.-]+$
    which is required by OpenRouter and other LLM API providers.
    """
    if not isinstance(schema, dict):
        return schema

    result = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            # Sanitize property names
            sanitized_props = {}
            for prop_name, prop_value in value.items():
                sanitized_name = sanitize_property_name(prop_name)
                sanitized_props[sanitized_name] = sanitize_schema_properties(prop_value)
            result[key] = sanitized_props
        elif key == "required" and isinstance(value, list):
            # Sanitize required field names
            result[key] = [sanitize_property_name(n) for n in value]
        elif isinstance(value, dict):
            result[key] = sanitize_schema_properties(value)
        elif isinstance(value, list):
            result[key] = [
                sanitize_schema_properties(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            result[key] = value
    return result


def sanitize_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize a complete tool schema (OpenAI function format) for LLM API compatibility.

    This is the main entry point for sanitizing tool schemas before sending to the LLM.
    It handles the full OpenAI function calling schema structure:
    {
        "type": "function",
        "function": {
            "name": "...",
            "description": "...",
            "parameters": { ... }
        }
    }
    """
    if not isinstance(schema, dict):
        return schema

    result = copy.deepcopy(schema)

    # Sanitize the parameters section if present
    if "function" in result and isinstance(result["function"], dict):
        func = result["function"]
        if "parameters" in func:
            func["parameters"] = sanitize_schema_properties(func["parameters"])
    elif "parameters" in result:
        # Handle flat schema format (name, description, parameters at top level)
        result["parameters"] = sanitize_schema_properties(result["parameters"])

    return result


class ToolCategory(str, Enum):
    """Tool category for reporting"""

    READ = "read"
    WRITE = "write"
    COMPUTE = "compute"


class ToolPolicy(BaseModel):
    """Policy configuration for a tool"""

    rate_limit: int | None = None  # Max calls per trial
    timeout_s: float = 30.0
    cost_weight: float = 1.0
    visibility: list[str] = ["agent"]  # "agent", "user", or both
    category: ToolCategory = ToolCategory.COMPUTE


class ToolResult(BaseModel):
    """Result from tool execution"""

    success: bool
    output: str
    error: str | None = None
    duration_s: float = 0.0
    metadata: dict[str, Any] = {}
    content_blocks: list[dict[str, Any]] | None = None  # Multimodal content (screenshots)


class Tool(ABC):
    """Base class for all tools"""

    def __init__(self, name: str, description: str, policy: ToolPolicy | None = None):
        self.name = name
        self.description = description
        self.policy = policy or ToolPolicy()

    @abstractmethod
    def get_schema(self) -> dict[str, Any]:
        """Return OpenAI function calling schema"""
        pass

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments"""
        pass


class ToolRegistry:
    """Registry for managing tools"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._call_counts: dict[str, int] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool"""
        self._tools[tool.name] = tool
        self._call_counts[tool.name] = 0

    def get_tool(self, name: str) -> Tool | None:
        """Get tool by name"""
        return self._tools.get(name)

    def get_schemas(
        self, tool_names: list[str] | None = None, sanitize: bool = True
    ) -> list[dict[str, Any]]:
        """Get OpenAI function calling schemas for specified tools.

        Args:
            tool_names: List of tool names to get schemas for. If None, returns all tools.
            sanitize: If True, sanitize property names to match LLM API requirements
                     (pattern: ^[a-zA-Z0-9_.-]+$). Default is True.

        Returns:
            List of tool schemas in OpenAI function calling format.
        """
        if tool_names is None:
            tool_names = list(self._tools.keys())

        schemas = []
        for name in tool_names:
            tool = self._tools.get(name)
            if tool:
                schema = tool.get_schema()
                if sanitize:
                    schema = sanitize_tool_schema(schema)
                schemas.append(schema)
        return schemas

    def reset_counts(self) -> None:
        """Reset call counts (per trial)"""
        self._call_counts = dict.fromkeys(self._tools.keys(), 0)


class ToolExecutor:
    """Executor for running tools with validation and logging"""

    def __init__(self, registry: ToolRegistry, env_client: Any | None = None):
        self.registry = registry
        self.env_client = env_client
        self.tool_logs: list[dict[str, Any]] = []

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """
        Execute a tool with validation

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            ToolResult with output and metadata
        """
        start_time = time.time()

        # Get tool
        tool = self.registry.get_tool(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_name}' not found",
                duration_s=time.time() - start_time,
            )

        # Validate arguments against schema
        try:
            schema = tool.get_schema()
            parameters = schema.get("function", {}).get("parameters")

            # Handle Nova/AWS Bedrock argument wrapping
            # Nova may send {'input': {...}} or {'input': '{"json": "string"}'}
            # or {'parameters': {...}} or extra keys
            unwrapped = arguments
            if isinstance(arguments, dict):
                # If wrapped in 'input', unwrap it
                if "input" in arguments and len(arguments) == 1:
                    inner = arguments["input"]
                    if isinstance(inner, dict):
                        unwrapped = inner
                    elif isinstance(inner, str):
                        # Nova sometimes sends JSON as a string - try to parse it
                        try:
                            parsed = json.loads(inner)
                            if isinstance(parsed, dict):
                                unwrapped = parsed
                        except json.JSONDecodeError:
                            # Not valid JSON, leave as-is for validation to catch
                            pass
                # If wrapped in 'parameters', unwrap it
                elif "parameters" in arguments and len(arguments) == 1:
                    inner = arguments["parameters"]
                    if isinstance(inner, dict):
                        unwrapped = inner
                    elif isinstance(inner, str):
                        # Try to parse JSON string
                        try:
                            parsed = json.loads(inner)
                            if isinstance(parsed, dict):
                                unwrapped = parsed
                        except json.JSONDecodeError:
                            pass

            # If tool has no parameters schema, allow empty arguments
            if parameters is None:
                # Tool expects no arguments - verify we got none
                if unwrapped and unwrapped != {}:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Tool '{tool_name}' takes no arguments but received: {unwrapped}",
                        duration_s=time.time() - start_time,
                    )
                arguments = {}  # Ensure empty dict for consistency
            else:
                arguments = unwrapped
                # Convert OpenAPI-style nullable to JSON Schema format before validation
                converted_parameters = convert_nullable_to_json_schema(parameters)
                validate(instance=arguments, schema=converted_parameters)
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid arguments: {e}",
                duration_s=time.time() - start_time,
            )

        # Check rate limit
        if tool.policy.rate_limit:
            count = self.registry._call_counts.get(tool_name, 0)
            if count >= tool.policy.rate_limit:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Rate limit exceeded for tool '{tool_name}'",
                    duration_s=time.time() - start_time,
                )

        # Execute tool with timeout
        try:
            result = tool.execute(**arguments)
            self.registry._call_counts[tool_name] = self.registry._call_counts.get(tool_name, 0) + 1
        except Exception as e:
            result = ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {e}",
                duration_s=time.time() - start_time,
            )

        # Log tool call
        self.tool_logs.append(
            {
                "tool": tool_name,
                "arguments": self._redact_sensitive(arguments),
                "success": result.success,
                "duration_s": result.duration_s,
                "error": result.error,
                "timestamp": time.time(),
            }
        )

        return result

    def _redact_sensitive(self, data: dict[str, Any]) -> dict[str, Any]:
        """Redact sensitive information from logs"""
        # Simple redaction - can be expanded
        redacted = {}
        sensitive_keys = ["password", "token", "secret", "api_key"]
        for key, value in data.items():
            if any(sk in key.lower() for sk in sensitive_keys):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = value
        return redacted

    def get_logs(self) -> list[dict[str, Any]]:
        """Get tool execution logs"""
        return self.tool_logs

    def reset(self) -> None:
        """Reset executor state"""
        self.tool_logs = []
        self.registry.reset_counts()


# Global registry instance
_global_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """Get global tool registry"""
    return _global_registry


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    executor_func: Callable[..., ToolResult],
    policy: ToolPolicy | None = None,
) -> None:
    """
    Decorator-style registration for tools

    Args:
        name: Tool name
        description: Tool description
        parameters: JSON Schema parameters
        executor_func: Function to execute the tool
        policy: Tool policy configuration
    """

    class DynamicTool(Tool):
        def __init__(self):
            super().__init__(name, description, policy)
            self.parameters = parameters
            self.executor_func = executor_func

        def get_schema(self) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": self.parameters,
                },
            }

        def execute(self, **kwargs: Any) -> ToolResult:
            return self.executor_func(**kwargs)

    tool = DynamicTool()
    _global_registry.register(tool)
