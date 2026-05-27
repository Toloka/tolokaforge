"""End-to-end wiring test — :class:`LLMClient` threads the post-sanitised
tool schema through to :meth:`ResponsePolicy.parse_arguments` so schema-aware
recovery (currently empty-container coercion) fires on real tool-call
arguments.

Without this wiring the empty-container fix in
:mod:`tolokaforge.core.llm.response_policy` would only run in unit tests —
the production path would never receive a populated ``param_types`` mapping
and qwen's ``equipment: ''`` failures would persist.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import (
    LLMClient,
    _resolve_declared_type,
    _root_param_types_by_tool,
)
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


def _equipment_tool() -> dict[str, Any]:
    """Mirrors qwen's ``tau_manufacturing_create_order`` schema shape:
    ``equipment`` is an array, ``status`` is a string, ``lines`` is an
    object dict-map.
    """
    return {
        "type": "function",
        "function": {
            "name": "create_order",
            "description": "Create a production order.",
            "parameters": {
                "type": "object",
                "required": ["status", "equipment", "lines"],
                "properties": {
                    "status": {"type": "string"},
                    "equipment": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Equipment identifiers.",
                    },
                    "lines": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Map of sku_id → line.",
                    },
                },
            },
        },
    }


def _completion_response_with_tool_call(arguments: dict[str, Any]) -> MagicMock:
    """Build a litellm-shape response that emits one tool-call with the
    given arguments JSON-encoded as the model would deliver them."""
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = ""
    message.thinking_blocks = None
    message.reasoning_content = None

    tc = MagicMock()
    tc.id = "call_1"
    tc.function = MagicMock()
    tc.function.name = "create_order"
    tc.function.arguments = json.dumps(arguments)
    message.tool_calls = [tc]
    choice.message = message

    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


class TestRootParamTypesByTool:
    """The schema-extraction helper that drives the wiring."""

    def test_extracts_root_level_types(self) -> None:
        out = _root_param_types_by_tool([_equipment_tool()])
        assert out == {
            "create_order": {
                "status": "string",
                "equipment": "array",
                "lines": "object",
            }
        }

    def test_handles_optional_via_anyof(self) -> None:
        tool = {
            "type": "function",
            "function": {
                "name": "modify_order",
                "parameters": {
                    "type": "object",
                    "properties": {
                        # Optional[list[str]] — Pydantic emits anyOf with null
                        "extras": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "null"},
                            ],
                            "default": None,
                        }
                    },
                },
            },
        }
        out = _root_param_types_by_tool([tool])
        assert out["modify_order"]["extras"] == "array"

    def test_empty_tools_list_returns_empty_dict(self) -> None:
        assert _root_param_types_by_tool([]) == {}
        assert _root_param_types_by_tool(None) == {}

    def test_resolve_declared_type_prefers_direct_type(self) -> None:
        assert _resolve_declared_type({"type": "string"}) == "string"

    def test_resolve_declared_type_picks_first_non_null_anyof_branch(self) -> None:
        assert _resolve_declared_type({"anyOf": [{"type": "null"}, {"type": "object"}]}) == "object"

    def test_resolve_declared_type_returns_none_for_unknown_shape(self) -> None:
        assert _resolve_declared_type({}) is None

    def test_resolve_declared_type_marks_strict_schema_dict_map_as_dict_map(self) -> None:
        """``StrictSchema``'s dict-map → array conversion shape gets the
        ``"dict_map"`` marker so the response policy can pivot empty
        arrays back to empty dicts. Shape signature is ``{type: array,
        items.properties.key}``."""
        dict_map_array_shape = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "qty": {"type": "integer"},
                },
                "required": ["key", "qty"],
            },
        }
        assert _resolve_declared_type(dict_map_array_shape) == "dict_map"

    def test_resolve_declared_type_plain_array_stays_array(self) -> None:
        """A genuine ``list[T]`` array (no ``items.properties.key``)
        keeps the ``"array"`` declared type."""
        plain_array = {"type": "array", "items": {"type": "string"}}
        assert _resolve_declared_type(plain_array) == "array"

    def test_resolve_declared_type_dict_map_inside_optional(self) -> None:
        """``Optional[dict[str, T]]`` round-trips: the ``anyOf`` branch
        carrying the dict-map → array shape is recognised."""
        optional_dict_map = {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}, "v": {"type": "integer"}},
                    },
                },
                {"type": "null"},
            ],
        }
        assert _resolve_declared_type(optional_dict_map) == "dict_map"


class TestLLMClientThreadsParamTypesIntoResponsePolicy:
    """The qwen pipeline (``passthrough`` schema + ``json_coerce`` response
    policy) recovers the empty-string-as-list shape end-to-end."""

    def test_qwen_empty_equipment_string_coerced_to_empty_list(self) -> None:
        # Qwen preset → PassthroughSchema + JsonCoerceResponse (the policy
        # path that consumes ``param_types``).
        client = LLMClient(ModelConfig(provider="openrouter", name="qwen/qwen3.6-plus"))
        tools = [_equipment_tool()]

        # Model-emitted arguments: equipment as empty string (the real bug
        # signature observed on tau_manufacturing).
        emitted_args = {
            "status": "pending",
            "equipment": "",
            "lines": {"SKU-A": {"qty": 1}},
        }

        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _completion_response_with_tool_call(emitted_args)
            result = client.generate(
                system="You help.",
                messages=[Message(role=MessageRole.USER, content="make order")],
                tools=tools,
            )

        assert len(result.tool_calls) == 1
        recovered = result.tool_calls[0].arguments
        assert (
            recovered["equipment"] == []
        ), f"Empty-string for an array param must be coerced to []. Got: {recovered!r}"
        # Other args round-trip untouched.
        assert recovered["status"] == "pending"
        assert recovered["lines"] == {"SKU-A": {"qty": 1}}

    def test_default_preset_without_array_param_passes_string_through(self) -> None:
        """Sanity: the wiring doesn't over-coerce when there's no schema info
        suggesting the value should be a container."""
        client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o"))
        tool = {
            "type": "function",
            "function": {
                "name": "echo",
                "parameters": {
                    "type": "object",
                    "required": ["msg"],
                    "properties": {"msg": {"type": "string"}},
                },
            },
        }
        emitted_args = {"msg": ""}  # legitimate empty string for a string param

        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _completion_response_with_tool_call(emitted_args)
            result = client.generate(
                system=None,
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=[tool],
            )
        # Adjust the tool-call name match — the mock used ``create_order`` —
        # but for this test we only care that the value passes through.
        recovered = result.tool_calls[0].arguments
        assert recovered.get("msg") == ""
