"""Unit tests for tools registry, especially nullable schema conversion."""

import pytest
from jsonschema import validate

from tolokaforge.tools.registry import (
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    convert_nullable_to_json_schema,
)

pytestmark = pytest.mark.unit


class TestConvertNullableToJsonSchema:
    """Tests for convert_nullable_to_json_schema function."""

    def test_simple_nullable_string(self):
        """Test conversion of simple nullable string field."""
        schema = {"type": "string", "nullable": True}
        converted = convert_nullable_to_json_schema(schema)

        assert converted["type"] == ["string", "null"]
        assert "nullable" not in converted

    def test_nested_nullable_in_properties(self):
        """Test nullable fields inside object properties."""
        schema = {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "gift_card_id": {"type": "string", "nullable": True},
                "credit_card_id": {"type": "string", "nullable": True},
            },
            "required": ["user_id"],
        }
        converted = convert_nullable_to_json_schema(schema)

        assert converted["properties"]["user_id"]["type"] == "string"
        assert converted["properties"]["gift_card_id"]["type"] == ["string", "null"]
        assert converted["properties"]["credit_card_id"]["type"] == ["string", "null"]

    def test_validation_with_none_value(self):
        """Test that None values validate correctly after conversion."""
        schema = {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "gift_card_id": {"type": "string", "nullable": True},
            },
            "required": ["user_id"],
        }
        converted = convert_nullable_to_json_schema(schema)

        # Should pass with None value
        validate({"user_id": "user123", "gift_card_id": None}, converted)

        # Should pass with string value
        validate({"user_id": "user123", "gift_card_id": "gc123"}, converted)

        # Should pass without optional field
        validate({"user_id": "user123"}, converted)

    def test_nullable_object_type(self):
        """Test nullable object type conversion."""
        schema = {
            "type": "object",
            "properties": {
                "delivery_address": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "city_id": {"type": "string"},
                        "address": {"type": "string"},
                    },
                },
            },
        }
        converted = convert_nullable_to_json_schema(schema)

        assert converted["properties"]["delivery_address"]["type"] == ["object", "null"]

        # Validate with None
        validate({"delivery_address": None}, converted)

        # Validate with object
        validate({"delivery_address": {"city_id": "1", "address": "123 Main St"}}, converted)

    def test_type_already_array(self):
        """Test handling when type is already an array."""
        schema = {"type": ["string", "integer"], "nullable": True}
        converted = convert_nullable_to_json_schema(schema)

        assert converted["type"] == ["string", "integer", "null"]

    def test_type_already_includes_null(self):
        """Test that null is not added twice if already present."""
        schema = {"type": ["string", "null"], "nullable": True}
        converted = convert_nullable_to_json_schema(schema)

        # Should not duplicate null
        assert converted["type"] == ["string", "null"]


class TestToolExecutorValidation:
    """Tests for ToolExecutor validation with nullable fields."""

    def test_executor_validates_nullable_fields(self):
        """Test that ToolExecutor properly validates nullable fields."""

        class MockTool(Tool):
            def get_schema(self):
                return {
                    "type": "function",
                    "function": {
                        "name": "test_tool",
                        "description": "Test tool",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "required_field": {"type": "string"},
                                "nullable_field": {"type": "string", "nullable": True},
                            },
                            "required": ["required_field"],
                        },
                    },
                }

            def execute(self, required_field, nullable_field=None):
                return ToolResult(
                    success=True,
                    output=f"required={required_field}, nullable={nullable_field}",
                )

        registry = ToolRegistry()
        tool = MockTool("test_tool", "Test tool")
        registry.register(tool)

        executor = ToolExecutor(registry)

        # Should succeed with None value
        result = executor.execute(
            "test_tool",
            {
                "required_field": "value",
                "nullable_field": None,
            },
        )
        assert result.success, f"Expected success but got error: {result.error}"

        # Should succeed with string value
        result = executor.execute(
            "test_tool",
            {
                "required_field": "value",
                "nullable_field": "optional_value",
            },
        )
        assert result.success, f"Expected success but got error: {result.error}"

        # Should succeed without the optional field
        result = executor.execute(
            "test_tool",
            {
                "required_field": "value",
            },
        )
        assert result.success, f"Expected success but got error: {result.error}"
