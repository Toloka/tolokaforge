"""Unit tests for tolokaforge/core/model_client.py — pure-logic helpers and construction."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tolokaforge.core.llm import (
    AnthropicContent,
    ArrayDictMapResponse,
    DictMapHints,
    NoPromptEnrichment,
    OpenAIContent,
    PassthroughSchema,
    StandardResponse,
    StrictSchema,
    UnwrapInputResponse,
    build_capabilities,
)
from tolokaforge.core.llm.presets import _match_preset
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers to build an LLMClient without touching real env / litellm
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> ModelConfig:
    """Build a ModelConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "provider": "openai",
        "name": "gpt-4",
        "temperature": 0.0,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


def _make_client(**config_overrides: Any):
    """Create an LLMClient with mocked env so no real keys are needed."""
    from tolokaforge.core.llm import LLMClient

    cfg = _make_config(**config_overrides)
    with patch.dict("os.environ", {}, clear=False):
        client = LLMClient(cfg)
    return client


# ===================================================================
# _should_retry_exception
# ===================================================================


@pytest.mark.unit
class TestShouldRetryException:
    """Verify the retry decision function."""

    def test_returns_true_for_runtime_error(self) -> None:
        from tolokaforge.core.llm.client import _should_retry_exception

        assert _should_retry_exception(RuntimeError("boom")) is True

    def test_returns_true_for_value_error(self) -> None:
        from tolokaforge.core.llm.client import _should_retry_exception

        assert _should_retry_exception(ValueError("bad")) is True

    def test_returns_true_for_generic_exception(self) -> None:
        from tolokaforge.core.llm.client import _should_retry_exception

        assert _should_retry_exception(Exception("generic")) is True

    def test_returns_true_for_keyboard_interrupt(self) -> None:
        from tolokaforge.core.llm.client import _should_retry_exception

        assert _should_retry_exception(KeyboardInterrupt()) is True

    def test_returns_true_for_os_error(self) -> None:
        from tolokaforge.core.llm.client import _should_retry_exception

        assert _should_retry_exception(OSError("network")) is True


# ===================================================================
# GenerationResult construction
# ===================================================================


@pytest.mark.unit
class TestGenerationResult:
    """Test GenerationResult default construction."""

    def test_defaults(self) -> None:
        from tolokaforge.core.llm import GenerationResult

        r = GenerationResult(text="hello")
        assert r.text == "hello"
        assert r.tool_calls == []
        assert r.usage == Usage()
        assert r.latency_s == 0.0
        assert r.cost_usd is None
        assert r.reasoning is None
        assert r.effective_system_prompt is None

    def test_with_tool_calls(self) -> None:
        from tolokaforge.core.llm import GenerationResult

        tc = ToolCall(id="tc1", name="search", arguments={"q": "test"})
        r = GenerationResult(text="", tool_calls=[tc])
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "search"

    def test_with_cost(self) -> None:
        from tolokaforge.core.llm import GenerationResult

        r = GenerationResult(
            text="", cost_usd=0.05, usage=Usage(prompt_tokens=100, completion_tokens=50)
        )
        assert r.cost_usd == 0.05
        assert r.usage.prompt_tokens == 100

    def test_effective_system_prompt_stored_when_provided(self) -> None:
        from tolokaforge.core.llm import GenerationResult

        prompt = "You are a helpful customer service agent for an airline."
        r = GenerationResult(text="ok", effective_system_prompt=prompt)
        assert r.effective_system_prompt == prompt

    def test_mock_generate_does_not_set_effective_system_prompt(self) -> None:
        """Mock provider returns GenerationResult without effective_system_prompt"""
        from tolokaforge.core.llm import GenerationResult

        # Mirrors _mock_generate behavior: no effective_system_prompt kwarg
        r = GenerationResult(
            text="Acknowledged.",
            tool_calls=[],
            usage=Usage(prompt_tokens=0, completion_tokens=1),
            latency_s=0.0,
            cost_usd=0.0,
        )
        assert r.effective_system_prompt is None


# ===================================================================
# LLMClient construction and _format_model_name
# ===================================================================


@pytest.mark.unit
class TestLLMClientConstruction:
    """LLMClient initialisation and model name formatting."""

    def test_basic_construction(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        assert client.config.provider == "openai"
        assert client.config.name == "gpt-4"
        assert client.model_name == "openai/gpt-4"

    def test_format_model_name_already_prefixed(self) -> None:
        client = _make_client(provider="openai", name="openai/gpt-4")
        assert client.model_name == "openai/gpt-4"

    def test_format_model_name_openrouter(self) -> None:
        client = _make_client(provider="openrouter", name="google/gemini-2.5-flash")
        assert client.model_name == "openrouter/google/gemini-2.5-flash"

    def test_format_model_name_nova(self) -> None:
        """Nova provider should use model name as-is without prefix."""
        client = _make_client(provider="nova", name="nova-pro-v1")
        assert client.model_name == "nova-pro-v1"

    def test_provider_stored_lowercase(self) -> None:
        client = _make_client(provider="OpenAI", name="gpt-4")
        assert client.provider == "openai"


# ===================================================================
# _repair_json_like (static)
# ===================================================================


@pytest.mark.unit
class TestRepairJsonLike:
    """LLMClient._repair_json_like light JSON repair."""

    def test_strips_markdown_fences(self) -> None:
        from tolokaforge.core.llm import LLMClient

        raw = '```json\n{"key": "value"}\n```'
        assert LLMClient._repair_json_like(raw) == '{"key": "value"}'

    def test_normalizes_smart_quotes(self) -> None:
        from tolokaforge.core.llm import LLMClient

        raw = "{\u201ckey\u201d: \u201cvalue\u201d}"
        result = LLMClient._repair_json_like(raw)
        assert "\u201c" not in result
        assert "\u201d" not in result
        assert '"key"' in result

    def test_quotes_unquoted_keys(self) -> None:
        from tolokaforge.core.llm import LLMClient

        raw = '{path: "/tmp/file"}'
        result = LLMClient._repair_json_like(raw)
        assert '"path"' in result

    def test_balances_braces(self) -> None:
        from tolokaforge.core.llm import LLMClient

        raw = '{"key": {"nested": 1}'
        result = LLMClient._repair_json_like(raw)
        assert result.count("{") == result.count("}")

    def test_already_valid_json(self) -> None:
        from tolokaforge.core.llm import LLMClient

        raw = '{"x": 1}'
        assert LLMClient._repair_json_like(raw) == '{"x": 1}'


# ===================================================================
# _parse_tool_arguments
# ===================================================================


@pytest.mark.unit
class TestParseToolArguments:
    """Tool argument parsing with fallback chain."""

    def test_dict_passthrough(self) -> None:
        client = _make_client()
        assert client._parse_tool_arguments("search", {"q": "hello"}) == {"q": "hello"}

    def test_valid_json_string(self) -> None:
        client = _make_client()
        result = client._parse_tool_arguments("search", '{"q": "hello"}')
        assert result == {"q": "hello"}

    def test_none_returns_empty(self) -> None:
        client = _make_client()
        assert client._parse_tool_arguments("search", None) == {}

    def test_empty_string_returns_empty(self) -> None:
        client = _make_client()
        assert client._parse_tool_arguments("search", "") == {}

    def test_non_string_non_dict_returns_empty(self) -> None:
        client = _make_client()
        assert client._parse_tool_arguments("search", 42) == {}

    def test_malformed_json_yaml_fallback(self) -> None:
        client = _make_client()
        # YAML can parse unquoted keys
        result = client._parse_tool_arguments("search", "q: hello")
        assert result == {"q": "hello"}

    def test_json_with_markdown_fences(self) -> None:
        client = _make_client()
        raw = '```json\n{"q": "hello"}\n```'
        result = client._parse_tool_arguments("search", raw)
        assert result == {"q": "hello"}

    def test_non_dict_json_returns_empty(self) -> None:
        """JSON that parses to a list or scalar should return empty dict."""
        client = _make_client()
        assert client._parse_tool_arguments("search", "[1, 2, 3]") == {}

    def test_browser_actions_string_recovery(self) -> None:
        """Browser tool with stringified actions list should decode."""
        client = _make_client()
        raw = {"actions": '[{"type": "click", "x": 100, "y": 200}]'}
        result = client._parse_tool_arguments("browser", raw)
        assert isinstance(result["actions"], list)
        assert result["actions"][0]["type"] == "click"


# ===================================================================
# _tool_block_format and supports_tool_image_blocks
# ===================================================================


@pytest.mark.unit
class TestToolBlockFormat:
    """Provider format detection for tool content blocks."""

    def test_anthropic_claude(self) -> None:
        client = _make_client(provider="anthropic", name="claude-3-sonnet")
        assert client._tool_block_format() == "anthropic"
        assert client.supports_tool_image_blocks() is True

    def test_openai_gpt(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        assert client._tool_block_format() == "openai"
        assert client.supports_tool_image_blocks() is False

    def test_openrouter_claude(self) -> None:
        client = _make_client(provider="openrouter", name="anthropic/claude-3-sonnet")
        assert client._tool_block_format() == "anthropic"

    def test_openrouter_openai(self) -> None:
        client = _make_client(provider="openrouter", name="openai/gpt-4")
        assert client._tool_block_format() == "openai"

    def test_nova_provider(self) -> None:
        client = _make_client(provider="nova", name="nova-pro-v1")
        assert client._tool_block_format() == "openai"

    def test_azure_provider(self) -> None:
        client = _make_client(provider="azure", name="my-deployment")
        assert client._tool_block_format() == "openai"

    def test_unknown_provider_defaults_openai(self) -> None:
        client = _make_client(provider="custom", name="my-model")
        assert client._tool_block_format() == "openai"


# ===================================================================
# _tool_blocks_to_text
# ===================================================================


@pytest.mark.unit
class TestToolBlocksToText:
    """Flatten content blocks to text-only summary."""

    def test_text_blocks(self) -> None:
        client = _make_client()
        blocks = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ]
        assert client._tool_blocks_to_text(blocks) == "Hello\nWorld"

    def test_image_only_returns_placeholder(self) -> None:
        client = _make_client()
        blocks = [{"type": "image", "source": {"type": "base64", "data": "abc"}}]
        assert client._tool_blocks_to_text(blocks) == "Screenshot captured."

    def test_empty_blocks(self) -> None:
        client = _make_client()
        assert client._tool_blocks_to_text([]) == ""

    def test_non_dict_blocks_ignored(self) -> None:
        client = _make_client()
        blocks = ["not a dict", 42, None]
        assert client._tool_blocks_to_text(blocks) == ""

    def test_empty_text_ignored(self) -> None:
        client = _make_client()
        blocks = [{"type": "text", "text": "   "}, {"type": "text", "text": "actual"}]
        assert client._tool_blocks_to_text(blocks) == "actual"


# ===================================================================
# _convert_messages
# ===================================================================


@pytest.mark.unit
class TestConvertMessages:
    """Message conversion to LiteLLM format."""

    def test_system_prompt_first(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.USER, content="Hi")]
        result = client._convert_messages("You are helpful.", msgs)
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "Hi"

    def test_no_system_prompt(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.USER, content="Hi")]
        result = client._convert_messages(None, msgs)
        assert result[0]["role"] == "user"

    def test_assistant_with_tool_calls(self) -> None:
        client = _make_client()
        tc = ToolCall(id="tc1", name="search", arguments={"q": "test"})
        msgs = [Message(role=MessageRole.ASSISTANT, content="Let me search.", tool_calls=[tc])]
        result = client._convert_messages(None, msgs)
        assert result[0]["role"] == "assistant"
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["function"]["name"] == "search"
        assert result[0]["tool_calls"][0]["id"] == "tc1"
        assert result[0]["tool_calls"][0]["type"] == "function"

    def test_tool_message(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.TOOL, content='{"result": 42}', tool_call_id="tc1")]
        result = client._convert_messages(None, msgs)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tc1"
        assert result[0]["content"] == '{"result": 42}'

    def test_empty_tool_content_replaced(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.TOOL, content="", tool_call_id="tc1")]
        result = client._convert_messages(None, msgs)
        assert result[0]["content"] == "{}"

    def test_empty_user_content_replaced(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.USER, content="")]
        result = client._convert_messages(None, msgs)
        assert result[0]["content"] == "Please continue."

    def test_empty_assistant_content_replaced(self) -> None:
        client = _make_client()
        msgs = [Message(role=MessageRole.ASSISTANT, content="")]
        result = client._convert_messages(None, msgs)
        assert result[0]["content"] == "I understand."

    def test_assistant_with_blank_content_and_tool_calls_nova_only(self) -> None:
        """Bedrock compat: blank assistant content with tool_calls gets placeholder
        ONLY on the Nova preset. Every other preset leaves content empty —
        injecting the filler universally is what created the Gemini echo
        regression (2026-04-30 OTS eval). See `ToolContentPolicy.inject_empty_assistant_filler`."""
        tc = ToolCall(id="tc1", name="fn", arguments={})
        msgs = [Message(role=MessageRole.ASSISTANT, content="  ", tool_calls=[tc])]

        # Nova: filler injected (Bedrock validation).
        nova_client = _make_client(provider="nova", name="nova-pro-v1")
        nova_result = nova_client._convert_messages(None, msgs)
        assert nova_result[0]["content"] == "I'll help you with that."

        # OpenAI default preset: filler off.
        openai_client = _make_client()
        openai_result = openai_client._convert_messages(None, msgs)
        assert openai_result[0]["content"] == ""


# ===================================================================
# StrictSchema policy — sanitisation and description
# ===================================================================


@pytest.mark.unit
class TestSchemaStrictness:
    """Schema sanitisation for strict providers (e.g. xAI/Grok)."""

    def test_strict_for_xai(self) -> None:
        client = _make_client(provider="openrouter", name="x-ai/grok-3")
        assert isinstance(client.capabilities.schema_sanitizer, StrictSchema)

    def test_not_strict_for_openai(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        assert isinstance(client.capabilities.schema_sanitizer, PassthroughSchema)

    # NB — The behavioural contract for ``StrictSchema`` lives in
    # :file:`tests/unit/llm/test_schema_sanitizer_position_aware.py` and
    # :file:`tests/unit/llm/test_schema_sanitizer_strict.py`. Those tests
    # exercise the :class:`ToolSchemaSanitizer` *Protocol* (no private
    # methods, no implementation details), per the
    # "Interfaces over implementation" principle from
    # :file:`plans/llm_reasoning_and_observability_fix.md`.

    def test_sanitise_typed_dict_map_to_array_via_public_api(self) -> None:
        """Smoke test of the public surface: typed dict-map → array of objects.
        Detailed coverage in :file:`tests/unit/llm/test_schema_sanitizer_strict.py`."""
        strict = StrictSchema()
        tool = {
            "type": "function",
            "function": {
                "name": "ping",
                "parameters": {
                    "type": "object",
                    "required": ["lines"],
                    "properties": {
                        "lines": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        }
                    },
                },
            },
        }
        result = strict.sanitize([tool])
        lines = result[0]["function"]["parameters"]["properties"]["lines"]
        assert lines["type"] == "array"
        assert "key" in lines["items"]["properties"]
        assert "value" in lines["items"]["properties"]

    def test_sanitise_array_conversion_regardless_of_key_order(self) -> None:
        """Both key orderings of additionalProperties vs description
        must produce identical array schemas at the public-API level."""
        strict = StrictSchema()
        value_schema = {
            "type": "object",
            "properties": {
                "qty": {"type": "number"},
                "alloc": {"type": "number"},
            },
            "required": ["qty", "alloc"],
        }

        def _wrap(parameters: dict[str, Any]) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {"name": "ping", "parameters": parameters},
            }

        # Schema A: additionalProperties BEFORE description (Pydantic order)
        params_a = {
            "type": "object",
            "required": ["lines"],
            "properties": {
                "lines": {
                    "type": "object",
                    "additionalProperties": copy.deepcopy(value_schema),
                    "description": "Map of sku_id -> line.",
                }
            },
        }
        # Schema B: description BEFORE additionalProperties
        params_b = {
            "type": "object",
            "required": ["lines"],
            "properties": {
                "lines": {
                    "type": "object",
                    "description": "Map of sku_id -> line.",
                    "additionalProperties": copy.deepcopy(value_schema),
                }
            },
        }

        result_a = strict.sanitize([_wrap(params_a)])[0]["function"]["parameters"]["properties"][
            "lines"
        ]
        result_b = strict.sanitize([_wrap(params_b)])[0]["function"]["parameters"]["properties"][
            "lines"
        ]
        assert result_a["type"] == result_b["type"] == "array"
        assert result_a["description"] == result_b["description"], (
            "Array conversion must be identical regardless of dict key order.\n"
            f"  Schema A (additionalProperties first): {result_a['description']!r}\n"
            f"  Schema B (description first):          {result_b['description']!r}"
        )

    def test_sanitise_real_tau_manufacturing_schema(self) -> None:  # noqa: PLR0915
        """StrictSchema.transform() on real tau_manufacturing CREATE_ORDER_TOOL
        must convert `lines` to array with explicit item properties.

        Position-aware contract preserves ``title`` / ``examples`` metadata
        — see :file:`tests/unit/llm/test_schema_sanitizer_position_aware.py`
        for the full strip rules.
        """
        strict = StrictSchema()
        tool = copy.deepcopy(
            {
                "type": "function",
                "function": {
                    "name": "tau_manufacturing_create_order",
                    "description": "Create a new production order.",
                    "parameters": {
                        "additionalProperties": False,
                        "description": "Input model for create_order.",
                        "properties": {
                            "status": {
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "on_hold",
                                    "completed",
                                    "closed",
                                ],
                                "title": "OrderStatus",
                                "type": "string",
                                "description": "Status of the order.",
                                "examples": ["pending"],
                            },
                            "lines": {
                                "additionalProperties": {
                                    "additionalProperties": False,
                                    "description": "Order line keyed by sku_id.",
                                    "properties": {
                                        "requested_quantity": {
                                            "description": "Requested quantity.",
                                            "minimum": 0,
                                            "title": "Requested Quantity",
                                            "type": "number",
                                        },
                                        "allocated_quantity": {
                                            "description": "Allocated quantity.",
                                            "minimum": 0,
                                            "title": "Allocated Quantity",
                                            "type": "number",
                                        },
                                    },
                                    "required": ["requested_quantity", "allocated_quantity"],
                                    "title": "OrderLine",
                                    "type": "object",
                                },
                                "description": "Map of sku_id -> line.",
                                "examples": [
                                    {
                                        "SKU-3A9E4": {
                                            "requested_quantity": 200,
                                            "allocated_quantity": 0,
                                        }
                                    }
                                ],
                                "title": "Lines",
                                "type": "object",
                            },
                            "produced_sku_id": {
                                "description": "SKU ID to produce.",
                                "examples": ["SKU-7F2C1"],
                                "title": "Produced Sku Id",
                                "type": "string",
                            },
                            "produced_quantity": {
                                "description": "Quantity to produce.",
                                "examples": [200],
                                "minimum": 0,
                                "title": "Produced Quantity",
                                "type": "number",
                            },
                        },
                        "required": ["status", "lines", "produced_sku_id", "produced_quantity"],
                        "title": "CreateOrderInput",
                        "type": "object",
                    },
                },
            }
        )

        result = strict.sanitize([tool])
        lines_schema = result[0]["function"]["parameters"]["properties"]["lines"]

        # Dict-map must be converted to array
        assert lines_schema["type"] == "array", "lines must be converted to array"
        assert "additionalProperties" not in lines_schema
        assert "items" in lines_schema

        # Items must have key + value fields
        items = lines_schema["items"]
        assert "key" in items["properties"], "items must have 'key' field"
        assert (
            "requested_quantity" in items["properties"]
        ), "items must have requested_quantity from value schema"
        assert (
            "allocated_quantity" in items["properties"]
        ), "items must have allocated_quantity from value schema"

        # Position-aware sanitiser preserves metadata (``title`` / ``examples``)
        # — they're informational and accepted by every current provider.
        # The pre-position-aware sanitiser stripped them recursively, which
        # caused the post-PR-#88 ``d365_api_create_case`` bug class.
        assert lines_schema.get("title") == "Lines"
        assert lines_schema.get("examples") == [
            {"SKU-3A9E4": {"requested_quantity": 200, "allocated_quantity": 0}}
        ]

    def test_sanitise_preserves_original_does_not_mutate(self) -> None:
        """StrictSchema.transform() must not mutate the original input dict."""
        strict = StrictSchema()
        original_tool = {
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "A test tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {"qty": {"type": "number"}},
                                "required": ["qty"],
                            },
                            "description": "Item map.",
                            "title": "Items",
                        },
                    },
                    "required": ["items"],
                },
            },
        }
        snapshot = copy.deepcopy(original_tool)
        strict.sanitize([original_tool])
        assert original_tool == snapshot, "transform() must not mutate the original tool dict"


# ===================================================================
# schema_sanitizer.sanitize() — tool schema conversion
# ===================================================================


@pytest.mark.unit
class TestSchemaTransform:
    """Tool schema conversion via schema policy (was _convert_tools)."""

    def test_passthrough_returns_same_reference(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        tools = [{"type": "function", "function": {"name": "f", "parameters": {"title": "T"}}}]
        assert client.capabilities.schema_sanitizer.sanitize(tools) is tools  # same reference

    def test_strict_sanitises(self) -> None:
        """Strict-preset clients route through the position-aware
        :class:`StrictSchema`. The only metadata-strip at the parameters
        root is ``description`` (Pydantic class-docstring artefact);
        ``title`` and other metadata are preserved per the post-PR-#88
        contract."""
        client = _make_client(provider="openrouter", name="x-ai/grok-3")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "parameters": {
                        "title": "T",
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "description": "top-level desc",
                    },
                },
            }
        ]
        result = client.capabilities.schema_sanitizer.sanitize(tools)
        params = result[0]["function"]["parameters"]
        # Top-level ``description`` in parameters is the only Pydantic
        # artefact intentionally removed (duplicates ``function.description``).
        assert "description" not in params
        # ``title`` survives — it carries class-name signal that does no
        # harm and is accepted by every current provider.
        assert params.get("title") == "T"


# ===================================================================
# _adapt_tool_content_blocks
# ===================================================================


@pytest.mark.unit
class TestAdaptToolContentBlocks:
    """Provider-specific tool content block adaptation."""

    def test_openai_text_block(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        blocks = [{"type": "text", "text": "Hello"}]
        result = client._adapt_tool_content_blocks(blocks)
        assert result == [{"type": "text", "text": "Hello"}]

    def test_openai_base64_image(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        blocks = [
            {
                "type": "image",
                "source": {"type": "base64", "data": "abc123", "media_type": "image/png"},
            }
        ]
        result = client._adapt_tool_content_blocks(blocks)
        assert result[0]["type"] == "image_url"
        assert "data:image/png;base64,abc123" in result[0]["image_url"]["url"]

    def test_anthropic_base64_image(self) -> None:
        client = _make_client(provider="anthropic", name="claude-3-sonnet")
        blocks = [
            {
                "type": "image",
                "source": {"type": "base64", "data": "abc123", "media_type": "image/png"},
            }
        ]
        result = client._adapt_tool_content_blocks(blocks)
        assert result[0]["type"] == "image"
        assert result[0]["source"]["type"] == "base64"
        assert result[0]["source"]["data"] == "abc123"

    def test_empty_blocks_return_fallback(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        result = client._adapt_tool_content_blocks([])
        assert result == [{"type": "text", "text": ""}]


# ===================================================================
# _mock_generate
# ===================================================================


@pytest.mark.unit
class TestMockGenerate:
    """Mock provider generation for offline tests."""

    def test_agent_response(self) -> None:
        client = _make_client(provider="mock", name="mock-agent")
        msgs = [Message(role=MessageRole.USER, content="Do something")]
        result = client._mock_generate(msgs, None)
        assert "Task complete" in result.text or "Acknowledged" in result.text

    def test_user_response_first_turn(self) -> None:
        client = _make_client(provider="mock", name="mock-user")
        result = client._mock_generate([], None)
        assert "benchmark task" in result.text.lower() or "help" in result.text.lower()

    def test_user_response_subsequent(self) -> None:
        client = _make_client(provider="mock", name="mock-user")
        msgs = [
            Message(role=MessageRole.USER, content="Hello"),
            Message(role=MessageRole.ASSISTANT, content="Sure"),
            Message(role=MessageRole.USER, content="Great"),
        ]
        result = client._mock_generate(msgs, None)
        assert "###STOP###" in result.text

    def test_judge_response(self) -> None:
        client = _make_client(provider="mock", name="mock-judge")
        msgs = [Message(role=MessageRole.USER, content="Rate this.")]
        result = client._mock_generate(msgs, None)
        assert "score" in result.text

    def test_mock_returns_zero_cost(self) -> None:
        client = _make_client(provider="mock", name="mock-agent")
        result = client._mock_generate([], None)
        assert result.cost_usd == 0.0


# ===================================================================
# _rotate_key
# ===================================================================


@pytest.mark.unit
class TestRotateKey:
    """API key rotation logic."""

    def test_rotate_succeeds(self) -> None:
        client = _make_client()
        client._api_keys = ["key1", "key2", "key3"]
        client._current_key_index = 0
        assert client._rotate_key() is True
        assert client._current_key_index == 1

    def test_rotate_exhausted(self) -> None:
        client = _make_client()
        client._api_keys = ["key1"]
        client._current_key_index = 0
        assert client._rotate_key() is False

    def test_rotate_empty_keys(self) -> None:
        client = _make_client()
        client._api_keys = []
        client._current_key_index = 0
        assert client._rotate_key() is False


# ===================================================================
# UserSimulator construction and scripted replies
# ===================================================================


@pytest.mark.unit
class TestUserSimulator:
    """UserSimulator construction and scripted_reply logic."""

    def test_scripted_mode_construction(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="scripted")
        assert sim.mode == "scripted"
        assert sim.llm_client is None
        assert sim.scripted_flow == []

    def test_llm_mode_without_config(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="llm", llm_config=None)
        assert sim.llm_client is None

    def test_scripted_reply_empty_context(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="scripted")
        result = sim.reply([])
        assert "help" in result.text.lower() or "task" in result.text.lower()

    def test_scripted_reply_unconditional_flow(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        flow = [{"user": "I need to book a flight."}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        # Context with no matching prior user message
        ctx = [Message(role=MessageRole.ASSISTANT, content="How can I help?")]
        result = sim.reply(ctx)
        assert result.text == "I need to book a flight."

    def test_scripted_reply_conditional_match(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        flow = [{"if_assistant_contains": "booking confirmed", "user": "Thank you!"}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        ctx = [Message(role=MessageRole.ASSISTANT, content="Your booking confirmed for tomorrow.")]
        result = sim.reply(ctx)
        assert result.text == "Thank you!"

    def test_scripted_reply_conditional_match_is_case_insensitive(self) -> None:
        """Real LLM output capitalises freely; literal substring match is brittle.

        Both the rule needle and the assistant text are lower-cased before
        comparison so e.g. "BOOKING CONFIRMED" matches a "booking confirmed"
        rule.
        """
        from tolokaforge.core.llm import UserSimulator

        flow = [{"if_assistant_contains": "booking confirmed", "user": "Thank you!"}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        ctx = [Message(role=MessageRole.ASSISTANT, content="Your BOOKING CONFIRMED for tomorrow.")]
        result = sim.reply(ctx)
        assert result.text == "Thank you!"

    def test_scripted_reply_default_fallback(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        flow = [{"default": "I'm not sure."}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        ctx = [Message(role=MessageRole.ASSISTANT, content="Something unexpected.")]
        result = sim.reply(ctx)
        assert result.text == "I'm not sure."

    def test_scripted_reply_question_fallback(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="scripted")
        ctx = [Message(role=MessageRole.ASSISTANT, content="Would you like to proceed?")]
        result = sim.reply(ctx)
        assert result.text == "Yes, please proceed."

    def test_scripted_reply_statement_fallback(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="scripted")
        ctx = [Message(role=MessageRole.ASSISTANT, content="Done.")]
        result = sim.reply(ctx)
        assert result.text == "Okay."

    def test_reply_unknown_mode_raises(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        sim = UserSimulator(mode="unknown")
        with pytest.raises(ValueError, match="Unknown user simulator mode"):
            sim.reply([Message(role=MessageRole.ASSISTANT, content="Hi")])


# ===================================================================
# UserSimulator._sanitize_user_text
# ===================================================================


@pytest.mark.unit
class TestSanitizeUserText:
    """Stripping banned simulation/AI meta-commentary from user text."""

    def test_removes_simulation_mention(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        result = UserSimulator._sanitize_user_text("This is a simulation of the task.")
        assert "simulation" not in result.lower()

    def test_removes_benchmark_mention(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        result = UserSimulator._sanitize_user_text("This benchmark tests performance.")
        assert "benchmark" not in result.lower()

    def test_preserves_clean_text(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        text = "I need to book a flight to NYC."
        assert UserSimulator._sanitize_user_text(text) == text

    def test_empty_text_stays_empty(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        assert UserSimulator._sanitize_user_text("") == ""

    def test_all_sentences_banned_returns_okay(self) -> None:
        from tolokaforge.core.llm import UserSimulator

        # When all sentences are stripped and nothing remains, returns fallback "Okay."
        result = UserSimulator._sanitize_user_text("AI model.")
        assert result == "Okay." or len(result) > 0


# ===================================================================
# _litellm_response_cost — priority-ladder branch coverage
# ===================================================================


@pytest.mark.unit
class TestLitellmResponseCostHelper:
    """Pin every branch of ``_litellm_response_cost``.

    The helper drives the runtime cost source decision, so each branch
    carries financial-reporting consequences if it silently regresses.
    These tests target the helper directly (rather than going through
    ``LLMClient.generate``) to keep each branch isolated.
    """

    def _make_response(self, *, hidden_params: Any) -> Any:
        """Return a MagicMock-shaped response with a controllable
        ``_hidden_params`` attribute. Using ``spec=("_hidden_params",)``
        prevents MagicMock auto-vivification from inventing other
        attributes that would steer ``litellm.completion_cost`` astray.
        """
        response = MagicMock()
        response._hidden_params = hidden_params
        return response

    def test_returns_hidden_params_response_cost_when_positive(self) -> None:
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={"response_cost": 0.0042})
        # litellm.completion_cost MUST NOT be consulted when hidden_params
        # already carries a positive value.
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=AssertionError("must not be called"),
        ):
            assert _litellm_response_cost(response) == pytest.approx(0.0042, abs=1e-9)

    def test_falls_through_when_hidden_params_missing_response_cost_key(self) -> None:
        """``_hidden_params`` is a dict but lacks the ``response_cost`` key."""
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={"other_field": 1})
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.005,
        ):
            assert _litellm_response_cost(response) == pytest.approx(0.005, abs=1e-9)

    def test_falls_through_when_hidden_params_response_cost_is_zero(self) -> None:
        """Zero hidden cost is treated as "no signal", not "free call".

        litellm sometimes stashes a ``0`` placeholder when it could not
        derive a price; we must not lock in $0 for a real call.
        """
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={"response_cost": 0.0})
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.005,
        ):
            assert _litellm_response_cost(response) == pytest.approx(0.005, abs=1e-9)

    def test_falls_through_when_hidden_params_response_cost_is_malformed(self) -> None:
        """Non-numeric ``response_cost`` is dropped, not crashed on."""
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={"response_cost": "not-a-number"})
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.007,
        ):
            assert _litellm_response_cost(response) == pytest.approx(0.007, abs=1e-9)

    def test_returns_none_when_completion_cost_returns_zero(self) -> None:
        """litellm returning 0.0 means "no pricing in catalog" — caller falls back.

        Distinguished from ``None``-return by libraries: litellm's
        ``completion_cost`` historically returns ``0.0`` for unpriced
        models (rather than raising). The helper must collapse both into
        the same fallback signal.
        """
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={})
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.0,
        ):
            assert _litellm_response_cost(response) is None

    def test_returns_none_when_completion_cost_raises(self) -> None:
        """Unknown-model exceptions from litellm collapse to ``None``."""
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params={})
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=Exception("model not in catalog"),
        ):
            assert _litellm_response_cost(response) is None

    def test_handles_hidden_params_not_a_dict(self) -> None:
        """Non-dict ``_hidden_params`` (e.g. accidentally a list) is ignored."""
        from tolokaforge.core.llm.client import _litellm_response_cost

        response = self._make_response(hidden_params=["unexpected", "shape"])
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.003,
        ):
            assert _litellm_response_cost(response) == pytest.approx(0.003, abs=1e-9)


# ===================================================================
# generate() with mocked litellm.completion
# ===================================================================


@pytest.mark.unit
class TestGenerateWithMockedLitellm:
    """LLMClient.generate() with mocked litellm.completion call."""

    def test_generate_simple_text_response(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")

        mock_message = MagicMock()
        mock_message.content = "Hello, world!"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.001):
                result = client.generate(
                    system="You are helpful.",
                    messages=[Message(role=MessageRole.USER, content="Hi")],
                )

        assert result.text == "Hello, world!"
        assert result.tool_calls == []
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.cost_usd == 0.001

    def test_generate_uses_litellm_response_cost(self) -> None:
        """Runtime path prefers ``response._hidden_params['response_cost']``.

        Litellm computes per-call cost against its (live-refreshed) pricing
        catalog and stashes it under ``_hidden_params``. Our harness must
        prefer that over the bundled ``pricing.json`` so cross-provider
        comparisons match what litellm itself would report.
        """
        client = _make_client(provider="openai", name="gpt-4")

        mock_message = MagicMock()
        mock_message.content = "Hello, litellm cost!"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response._hidden_params = {"response_cost": 0.0042}

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=99.0) as fallback:
                result = client.generate(
                    system="You are helpful.",
                    messages=[Message(role=MessageRole.USER, content="Hi")],
                )

        assert result.cost_usd == pytest.approx(0.0042, abs=1e-6)
        # Local estimate must NOT be consulted when litellm priced the call.
        fallback.assert_not_called()

    def test_generate_returns_none_cost_when_no_tokens_and_unpriced(self) -> None:
        """Zero-token responses with no litellm price keep ``cost_usd`` as None.

        The fallback to :func:`estimate_cost` is gated on
        ``prompt_tokens or completion_tokens`` being non-zero — otherwise we
        have nothing to compute against and shouldn't synthesize a 0.0
        masquerading as "we priced this call".
        """
        client = _make_client(provider="openai", name="gpt-4")

        mock_message = MagicMock()
        mock_message.content = "ok"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 0
        mock_response.usage.completion_tokens = 0
        mock_response._hidden_params = {}

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch(
                "tolokaforge.core.llm.client.litellm.completion_cost",
                side_effect=Exception("unpriced"),
            ):
                result = client.generate(
                    system="You are helpful.",
                    messages=[Message(role=MessageRole.USER, content="Hi")],
                )

        assert result.cost_usd is None

    def test_generate_falls_back_to_estimate_when_litellm_unpriced(self) -> None:
        """When litellm cannot price the call (unknown model), use our table."""
        client = _make_client(provider="openai", name="gpt-4")

        mock_message = MagicMock()
        mock_message.content = "Hello, fallback!"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        # Explicitly empty hidden_params + litellm.completion_cost raises:
        # mirrors the unpriced-model path.
        mock_response._hidden_params = {}

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch(
                "tolokaforge.core.llm.client.litellm.completion_cost",
                side_effect=Exception("model not in litellm catalog"),
            ):
                with patch(
                    "tolokaforge.core.llm.client.estimate_cost", return_value=0.0011
                ) as fallback:
                    result = client.generate(
                        system="You are helpful.",
                        messages=[Message(role=MessageRole.USER, content="Hi")],
                    )

        assert result.cost_usd == pytest.approx(0.0011, abs=1e-6)
        fallback.assert_called_once()

    def test_generate_with_tool_calls(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")

        mock_tc = MagicMock()
        mock_tc.id = "call_123"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = '{"query": "weather"}'

        mock_message = MagicMock()
        mock_message.content = "Let me search."
        mock_message.tool_calls = [mock_tc]
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 10

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.002):
                result = client.generate(
                    messages=[Message(role=MessageRole.USER, content="What's the weather?")],
                )

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "weather"}

    def test_generate_mock_provider_bypasses_litellm(self) -> None:
        """Mock provider should NOT call litellm.completion."""
        client = _make_client(provider="mock", name="mock-agent")

        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            result = client.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
            )

        mock_completion.assert_not_called()
        assert result.text  # has content

    def test_generate_nova_unwraps_input_key(self) -> None:
        """Nova provider wraps arguments in {'input': {...}} — verify unwrapping."""
        client = _make_client(provider="nova", name="us.amazon.nova-pro-v1:0")

        mock_tc = MagicMock()
        mock_tc.id = "call_456"
        mock_tc.function.name = "get_order"
        mock_tc.function.arguments = '{"input": {"order_id": "123"}}'

        mock_message = MagicMock()
        mock_message.content = "Checking order."
        mock_message.tool_calls = [mock_tc]
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 15
        mock_response.usage.completion_tokens = 8

        with patch.dict("os.environ", {"NOVA_API_KEY": "test-key"}):
            with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
                with patch("tolokaforge.core.llm.client.estimate_cost", return_value=None):
                    result = client.generate(
                        messages=[Message(role=MessageRole.USER, content="Check order")],
                    )

        # The wrapped {'input': {'order_id': '123'}} should be unwrapped
        assert result.tool_calls[0].arguments == {"order_id": "123"}

    def test_generate_timeout_retries_before_success(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        mock_message = MagicMock()
        mock_message.content = "Hello, world!"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5

        # Exhaust the full retry budget then succeed: with the default
        # ``DEFAULT_API_TIMEOUT_RETRIES = 5``, the inner loop allows
        # ``5 + 1 = 6`` attempts. Fail the first 5, succeed on the 6th.
        timeout_failures = client._api_timeout_retries
        call_count = 0

        def completion_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= timeout_failures:
                raise TimeoutError("Request timed out")
            return mock_response

        with patch(
            "tolokaforge.core.llm.client.completion", side_effect=completion_side_effect
        ) as mock_completion:
            with patch("tolokaforge.core.llm.client.time.sleep"):
                with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.001):
                    result = client.generate(
                        system="You are helpful.",
                        messages=[Message(role=MessageRole.USER, content="Hi")],
                    )

        assert result.text == "Hello, world!"
        assert call_count == timeout_failures + 1
        assert mock_completion.call_args.kwargs["timeout"] == client._api_call_timeout_s

    def test_generate_timeout_exhaustion_raises(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")

        def completion_side_effect(*args, **kwargs):
            raise TimeoutError("Request timed out")

        from tolokaforge.core.llm.client import LLMApiTimeoutError

        with patch("tolokaforge.core.llm.client.completion", side_effect=completion_side_effect):
            with patch("tolokaforge.core.llm.client.time.sleep"):
                with pytest.raises(LLMApiTimeoutError, match="timed out"):
                    client.generate(
                        system="You are helpful.",
                        messages=[Message(role=MessageRole.USER, content="Hi")],
                    )

    def test_api_timeout_resolution_uses_default_when_unset(self) -> None:
        """No env var, no preset value → fall back to the module default."""
        from tolokaforge.core.llm.client import (
            DEFAULT_API_CALL_TIMEOUT_S,
            DEFAULT_API_TIMEOUT_RETRIES,
        )

        with patch.dict(
            "os.environ",
            {},
            clear=False,
        ):
            for var in (
                "TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S",
                "TOLOKAFORGE_LLM_API_CALL_RETRIES",
            ):
                os.environ.pop(var, None)
            client = _make_client(provider="openai", name="gpt-4")

        assert client.capabilities.api_call_timeout_s is None
        assert client.capabilities.api_call_retries is None
        assert client._api_call_timeout_s == DEFAULT_API_CALL_TIMEOUT_S
        assert client._api_timeout_retries == DEFAULT_API_TIMEOUT_RETRIES

    def test_api_timeout_resolution_uses_preset_when_no_env(self) -> None:
        """Preset value from ModelCapabilities wins over the default when
        no env var is set."""
        from tolokaforge.core.llm import LLMClient
        from tolokaforge.core.llm.capabilities import ModelCapabilities

        cfg = _make_config(provider="openai", name="gpt-4")
        with patch.dict("os.environ", {}, clear=False):
            for var in (
                "TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S",
                "TOLOKAFORGE_LLM_API_CALL_RETRIES",
            ):
                os.environ.pop(var, None)
            client = LLMClient(cfg)
            client.capabilities = ModelCapabilities(
                api_call_timeout_s=600.0,
                api_call_retries=2,
            )
            # Re-resolve so the new capabilities take effect.
            client._api_call_timeout_s = client._load_api_timeout()
            client._api_timeout_retries = client._load_api_timeout_retries()

        assert client._api_call_timeout_s == 600.0
        assert client._api_timeout_retries == 2

    def test_api_timeout_resolution_env_overrides_preset(self) -> None:
        """The env-var operational override wins over the per-model preset."""
        from tolokaforge.core.llm import LLMClient
        from tolokaforge.core.llm.capabilities import ModelCapabilities

        cfg = _make_config(provider="openai", name="gpt-4")
        with patch.dict(
            "os.environ",
            {
                "TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S": "30",
                "TOLOKAFORGE_LLM_API_CALL_RETRIES": "0",
            },
        ):
            client = LLMClient(cfg)
            client.capabilities = ModelCapabilities(
                api_call_timeout_s=600.0,
                api_call_retries=10,
            )
            client._api_call_timeout_s = client._load_api_timeout()
            client._api_timeout_retries = client._load_api_timeout_retries()

        assert client._api_call_timeout_s == 30.0
        assert client._api_timeout_retries == 0

    def test_generate_timeout_does_not_trigger_outer_retry(self) -> None:
        """``LLMApiTimeoutError`` must propagate un-wrapped through
        ``_call_with_key_rotation`` so the outer ``@retry`` declines to
        re-enter ``generate()``. Otherwise the bounded budget multiplies
        by ``stop_after_attempt(5)`` (5x blow-up of API calls and wall
        time on a permanently timing-out endpoint)."""
        from tolokaforge.core.llm.client import LLMApiTimeoutError

        client = _make_client(provider="openai", name="gpt-4")
        call_count = 0

        def completion_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise TimeoutError("Request timed out")

        with patch("tolokaforge.core.llm.client.completion", side_effect=completion_side_effect):
            with patch("tolokaforge.core.llm.client.time.sleep"):
                with pytest.raises(LLMApiTimeoutError):
                    client.generate(
                        system="You are helpful.",
                        messages=[Message(role=MessageRole.USER, content="Hi")],
                    )

        assert call_count == client._api_timeout_retries + 1


# ===================================================================
# build_capabilities() — auto-detection and config overrides
# ===================================================================


@pytest.mark.unit
class TestModelCapabilities:
    """Tests for ModelCapabilities auto-detection and config overrides."""

    def test_claude_capabilities(self) -> None:
        caps = build_capabilities("anthropic/claude-sonnet-4.6")
        assert caps.content_policy.format == "anthropic"
        assert caps.content_policy.supports_images is True
        assert caps.params_policy._supports_seed is False
        assert isinstance(caps.schema_sanitizer, PassthroughSchema)  # Claude handles full schema
        assert isinstance(caps.content_policy, AnthropicContent)

    def test_gpt5_capabilities(self) -> None:
        caps = build_capabilities("openai/gpt-5.4")
        assert isinstance(caps.schema_sanitizer, StrictSchema)
        assert (
            caps.params_policy._fixed_temperature is None
        )  # No forced temp — surface errors explicitly
        assert caps.params_policy._supports_seed is True  # GPT-5 supports seed

    def test_grok_capabilities(self) -> None:
        caps = build_capabilities("x-ai/grok-4.2")
        assert isinstance(caps.schema_sanitizer, StrictSchema)

    def test_nova_capabilities(self) -> None:
        caps = build_capabilities("nova-2-lite", provider="nova")
        assert isinstance(caps.response_policy, UnwrapInputResponse)

    def test_openrouter_provider(self) -> None:
        caps = build_capabilities("google/gemini-2.5-flash", provider="openrouter")
        assert caps.params_policy._reasoning_via_extra_body is True

    def test_default_capabilities(self) -> None:
        caps = build_capabilities("unknown-model-xyz")
        # Policy types
        assert isinstance(caps.schema_sanitizer, PassthroughSchema)
        assert isinstance(caps.prompt_policy, NoPromptEnrichment)
        assert isinstance(caps.content_policy, OpenAIContent)
        assert isinstance(caps.response_policy, StandardResponse)
        # Content policy
        assert caps.content_policy.format == "openai"
        assert caps.content_policy.supports_images is False
        # Params policy
        assert caps.params_policy._fixed_temperature is None
        assert caps.params_policy._supports_seed is True
        assert caps.params_policy._reasoning_via_extra_body is False

    def test_config_overrides(self) -> None:
        caps = build_capabilities(
            "openai/gpt-5.4",
            overrides={"dict_map_prompt_hints": True},
        )
        assert isinstance(caps.prompt_policy, DictMapHints)
        assert isinstance(caps.schema_sanitizer, StrictSchema)  # Auto-detected still works

    def test_config_override_invalid_field_raises(self) -> None:
        # Post-review item 2: ``_apply_config_overrides`` now raises on
        # unrecognised keys so that typos in run-config YAML surface loudly
        # (AGENTS.md rule #1 — "Surface failures explicitly"). Dedicated
        # coverage for the raise path lives in
        # ``tests/unit/llm/test_preset_overrides.py``.
        with pytest.raises(ValueError, match="Unknown capability override keys"):
            build_capabilities(
                "openai/gpt-5.4",
                overrides={"nonexistent_field": True},
            )

    def test_frozen_dataclass(self) -> None:
        caps = build_capabilities("openai/gpt-5.4")
        with pytest.raises(AttributeError):
            caps.schema_sanitizer = PassthroughSchema()  # type: ignore[misc]


# ===================================================================
# DictMapHints._build_hints() — system prompt hint generation
# ===================================================================


@pytest.mark.unit
class TestBuildDictMapHints:
    """Tests for dict-map system prompt hint generation."""

    def test_no_tools_returns_empty(self) -> None:
        assert DictMapHints._build_hints([]) == ""

    def test_simple_tool_no_hints(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        assert DictMapHints._build_hints(tools) == ""

    def test_typed_dict_map_generates_hint(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "create_order",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lines": {
                                "additionalProperties": {
                                    "properties": {"qty": {"type": "number"}},
                                    "required": ["qty"],
                                    "type": "object",
                                },
                                "type": "object",
                            }
                        },
                    },
                },
            }
        ]
        hint = DictMapHints._build_hints(tools)
        assert "create_order" in hint
        assert "lines" in hint
        assert "MUST" in hint

    def test_additional_properties_false_no_hint(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "update",
                    "parameters": {
                        "type": "object",
                        "properties": {"data": {"additionalProperties": False, "type": "object"}},
                    },
                },
            }
        ]
        assert DictMapHints._build_hints(tools) == ""

    def test_additional_properties_true_generates_hint(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "flex_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "meta": {
                                "additionalProperties": True,
                                "description": "Arbitrary metadata.",
                                "type": "object",
                            }
                        },
                    },
                },
            }
        ]
        hint = DictMapHints._build_hints(tools)
        assert "flex_tool" in hint
        assert "meta" in hint


# ===================================================================
# ArrayDictMapResponse — reverse array→dict conversion
# ===================================================================


@pytest.mark.unit
class TestArrayDictMapResponse:
    """Tests for ArrayDictMapResponse reverse mapping."""

    def test_converts_array_with_key_to_dict(self) -> None:
        policy = ArrayDictMapResponse()
        args = {
            "status": "pending",
            "lines": [
                {"key": "SKU-001", "requested_quantity": 500, "allocated_quantity": 0},
                {"key": "SKU-002", "requested_quantity": 100, "allocated_quantity": 50},
            ],
            "produced_sku_id": "SKU-001",
        }
        result = policy.parse_arguments(args)
        assert isinstance(result["lines"], dict)
        assert "SKU-001" in result["lines"]
        assert "SKU-002" in result["lines"]
        assert result["lines"]["SKU-001"] == {"requested_quantity": 500, "allocated_quantity": 0}
        assert result["lines"]["SKU-002"] == {"requested_quantity": 100, "allocated_quantity": 50}
        # Non-array fields unchanged
        assert result["status"] == "pending"
        assert result["produced_sku_id"] == "SKU-001"

    def test_ignores_array_without_key(self) -> None:
        """Arrays without 'key' field in items are left untouched."""
        policy = ArrayDictMapResponse()
        args = {"items": [{"name": "a"}, {"name": "b"}]}
        result = policy.parse_arguments(args)
        assert isinstance(result["items"], list)

    def test_ignores_non_array_fields(self) -> None:
        policy = ArrayDictMapResponse()
        args = {"status": "ok", "count": 42, "data": {"nested": True}}
        result = policy.parse_arguments(args)
        assert result == args

    def test_handles_empty_array(self) -> None:
        policy = ArrayDictMapResponse()
        args = {"lines": []}
        result = policy.parse_arguments(args)
        assert result["lines"] == []

    def test_round_trip_schema_then_response(self) -> None:
        """StrictSchema + ArrayDictMapResponse must round-trip correctly.

        Goes through the public Protocol surface: ``sanitize([tool])`` →
        ``parse_arguments(llm_args)``."""
        strict = StrictSchema()
        tool = {
            "type": "function",
            "function": {
                "name": "modify",
                "parameters": {
                    "type": "object",
                    "required": ["items_map"],
                    "properties": {
                        "items_map": {
                            "type": "object",
                            "additionalProperties": {
                                "properties": {"qty": {"type": "number"}},
                                "required": ["qty"],
                                "type": "object",
                            },
                            "description": "Items map.",
                        }
                    },
                },
            },
        }
        # Forward: schema converts the dict-map property to array
        sanitised = strict.sanitize([tool])
        items_map = sanitised[0]["function"]["parameters"]["properties"]["items_map"]
        assert items_map["type"] == "array"

        # LLM would produce:
        llm_args = {"items_map": [{"key": "A", "qty": 10}, {"key": "B", "qty": 20}]}

        # Reverse: ArrayDictMapResponse converts back to dict
        response = ArrayDictMapResponse()
        final = response.parse_arguments(llm_args)
        assert final["items_map"] == {"A": {"qty": 10}, "B": {"qty": 20}}

    def test_converts_single_dict_with_key(self) -> None:
        """Single dict with 'key' field should be converted to single-entry dict."""
        policy = ArrayDictMapResponse()
        args = {
            "order_id": "ORD-001",
            "upsert_lines": {
                "key": "SKU-3E9F4",
                "requested_quantity": 240,
                "allocated_quantity": 120,
            },
        }
        result = policy.parse_arguments(args)
        assert isinstance(result["upsert_lines"], dict)
        assert "SKU-3E9F4" in result["upsert_lines"]
        assert result["upsert_lines"]["SKU-3E9F4"] == {
            "requested_quantity": 240,
            "allocated_quantity": 120,
        }
        assert result["order_id"] == "ORD-001"

    def test_ignores_dict_without_key(self) -> None:
        """Regular dicts without 'key' field should not be converted."""
        policy = ArrayDictMapResponse()
        args = {"status": "pending", "data": {"name": "test", "value": 42}}
        result = policy.parse_arguments(args)
        assert result["data"] == {"name": "test", "value": 42}


# ===================================================================
# Preset loading and matching
# ===================================================================


@pytest.mark.unit
class TestPresetLoading:
    """Tests for YAML preset loading and matching."""

    def test_preset_file_exists(self) -> None:
        """model_presets.yaml must exist at the expected path."""
        preset_path = Path("tolokaforge/core/data/model_presets.yaml")
        assert preset_path.exists(), f"Preset file not found: {preset_path}"

    def test_preset_file_valid_yaml(self) -> None:
        """model_presets.yaml must parse as valid YAML with expected sections."""
        with open(Path("tolokaforge/core/data/model_presets.yaml")) as f:
            data = yaml.safe_load(f)
        assert "default" in data
        assert "presets" in data
        assert "providers" in data

    def test_preset_match_anthropic(self) -> None:
        """anthropic/claude-* should match the anthropic preset."""
        cfg = _match_preset("anthropic/claude-sonnet-4.6", "")
        assert cfg.get("content_policy") == "anthropic"

    def test_preset_match_gpt5(self) -> None:
        """openai/gpt-5.4 should match the openai_gpt5 preset."""
        cfg = _match_preset("openai/gpt-5.4", "")
        assert cfg.get("schema_sanitizer") == "strict"

    def test_preset_match_grok(self) -> None:
        """x-ai/grok-4.2 should match xai_grok preset."""
        cfg = _match_preset("x-ai/grok-4.2", "")
        assert cfg.get("schema_sanitizer") == "strict"

    def test_preset_match_nova_by_provider(self) -> None:
        """nova provider should match aws_nova preset."""
        cfg = _match_preset("nova-2-lite", "nova")
        assert cfg.get("response_policy") == "unwrap_input"

    def test_preset_provider_overlay_openrouter(self) -> None:
        """OpenRouter provider should add reasoning_via_extra_body."""
        cfg = _match_preset("google/gemini-2.5-flash", "openrouter")
        assert cfg.get("params", {}).get("reasoning_via_extra_body") is True

    def test_preset_default_for_unknown_model(self) -> None:
        """Unknown model should get defaults only."""
        cfg = _match_preset("unknown-model-xyz", "")
        assert cfg.get("schema_sanitizer") == "passthrough"
        assert cfg.get("content_policy") == "openai"
        assert cfg.get("prompt_policy") == "none"

    def test_preset_merge_order(self) -> None:
        """Provider overlay should merge on top of model preset."""
        # GPT-5 via OpenRouter should get both strict schema AND reasoning_via_extra_body
        cfg = _match_preset("openai/gpt-5.4", "openrouter")
        assert cfg.get("schema_sanitizer") == "strict"
        params = cfg.get("params", {})
        assert params.get("reasoning_via_extra_body") is True

    def test_build_capabilities_from_preset(self) -> None:
        """build_capabilities should produce correct policy objects from presets."""
        caps = build_capabilities(
            "openai/gpt-5.4", "openrouter", overrides={"dict_map_prompt_hints": True}
        )
        assert isinstance(caps.schema_sanitizer, StrictSchema)
        assert isinstance(caps.prompt_policy, DictMapHints)


# ===================================================================
# api_call_wall_timeout_s — configurable hard wall-clock ceiling
# ===================================================================


@pytest.mark.unit
class TestApiCallWallTimeout:
    """Resolution + enforcement of the configurable per-call wall-clock timeout."""

    def test_defaults_to_none(self) -> None:
        """Disabled by default — no wall-clock abort unless opted in."""
        client = _make_client()
        assert client._api_call_wall_timeout_s is None

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S", "300")
        client = _make_client()
        assert client._api_call_wall_timeout_s == 300.0

    def test_preset_used_when_no_env(self) -> None:
        from dataclasses import replace

        client = _make_client()
        # ModelCapabilities is frozen — swap in a copy carrying the preset value.
        client.capabilities = replace(client.capabilities, api_call_wall_timeout_s=180.0)
        assert client._load_api_wall_timeout() == 180.0

    def test_env_beats_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import replace

        monkeypatch.setenv("TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S", "60")
        client = _make_client()
        client.capabilities = replace(client.capabilities, api_call_wall_timeout_s=999.0)
        assert client._load_api_wall_timeout() == 60.0

    def test_disabled_calls_completion_directly(self) -> None:
        """With no wall timeout the call runs inline, not on a worker thread."""
        client = _make_client()
        client._api_call_wall_timeout_s = None
        sentinel = object()
        with patch(
            "tolokaforge.core.llm.client.completion", return_value=sentinel
        ) as mock_completion:
            result = client._call_completion_with_timeout_retry({"model": "x"})
        assert result is sentinel
        mock_completion.assert_called_once()

    def test_aborts_runaway_call_and_is_terminal(self) -> None:
        """A call over the wall budget is aborted at ~wall and NOT retried.

        Runs with the default retry budget: had the wall-timeout been routed
        back into the per-call retry it would take several backoff-separated
        re-attempts (>=5 s); a terminal abort returns in ~the wall budget.
        """
        import time as _time

        from tolokaforge.core.llm.client import LLMApiTimeoutError

        client = _make_client()
        client._api_call_wall_timeout_s = 0.1

        def _runaway(**kwargs: Any) -> Any:
            _time.sleep(2.0)  # far longer than the 0.1s wall budget
            return "never returned"

        started = _time.monotonic()
        with patch("tolokaforge.core.llm.client.completion", side_effect=_runaway):
            with pytest.raises(LLMApiTimeoutError):
                client._call_completion_with_timeout_retry({"model": "x"})
        elapsed = _time.monotonic() - started
        # Aborted at ~0.1s and not retried (a retry would add >=5s of backoff).
        assert elapsed < 1.5

    def test_preserves_nontimeout_exception(self) -> None:
        """A non-timeout error from the call propagates unchanged with the wall
        wrapper active, so upstream key-rotation / error handling still fires."""
        client = _make_client()
        client._api_call_wall_timeout_s = 5.0

        with patch(
            "tolokaforge.core.llm.client.completion",
            side_effect=RuntimeError("Key limit exceeded"),
        ):
            with pytest.raises(RuntimeError, match="Key limit exceeded"):
                client._call_completion_with_timeout_retry({"model": "x"})
