"""Integration tests for LLM model parameter validation.

These tests make real OpenRouter API calls to verify that model parameters
are accepted by the upstream provider.  They require a valid
``OPENROUTER_API_KEY`` in the environment.

Run with::

    scripts/with_env.sh uv run pytest tests/integration/test_llm_model_params.py -v -m integration
"""

from __future__ import annotations

import os

import pytest

from tolokaforge.core.model_client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = [pytest.mark.integration, pytest.mark.requires_api, pytest.mark.llm]

_SIMPLE_MESSAGES = [Message(role=MessageRole.USER, content="Reply with exactly one word: hello")]


@pytest.fixture(autouse=True)
def _require_openrouter_key():
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")


# ---------------------------------------------------------------------------
# Reasoning parameter validation
# ---------------------------------------------------------------------------


class TestReasoningParameter:
    """Verify OpenRouter reasoning parameter is accepted across models.

    OpenRouter reasoning format:
        {"reasoning": {"effort": "<level>", "enabled": true}}
    See https://openrouter.ai/docs/reasoning
    """

    @pytest.mark.parametrize(
        "model_name",
        [
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1-mini",
        ],
    )
    def test_reasoning_medium_accepted(self, model_name: str):
        """Model should accept reasoning effort='medium' without error."""
        config = ModelConfig(
            provider="openrouter",
            name=model_name,
            temperature=0.0,
            reasoning="medium",
            max_tokens=64,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a test assistant.",
            messages=_SIMPLE_MESSAGES,
        )
        assert result.text

    @pytest.mark.parametrize(
        "effort",
        ["low", "medium", "high"],
    )
    def test_reasoning_effort_levels(self, effort: str):
        """Different effort levels should all be accepted."""
        config = ModelConfig(
            provider="openrouter",
            name="anthropic/claude-sonnet-4",
            temperature=0.0,
            reasoning=effort,
            max_tokens=64,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a test assistant.",
            messages=_SIMPLE_MESSAGES,
        )
        assert result.text

    def test_reasoning_off_succeeds(self):
        """reasoning='off' should NOT send the reasoning parameter."""
        config = ModelConfig(
            provider="openrouter",
            name="anthropic/claude-sonnet-4",
            temperature=0.0,
            reasoning="off",
            max_tokens=64,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a test assistant.",
            messages=_SIMPLE_MESSAGES,
        )
        assert result.text

    def test_reasoning_with_tool_use(self):
        """Reasoning + tool use should work together."""
        tool = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a city",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        config = ModelConfig(
            provider="openrouter",
            name="anthropic/claude-sonnet-4",
            temperature=0.0,
            reasoning="medium",
            max_tokens=256,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You MUST call get_weather for any weather question.",
            messages=[Message(role=MessageRole.USER, content="Weather in Tokyo?")],
            tools=tool,
            tool_choice="auto",
        )
        # Should produce either tool call or text (both valid with reasoning)
        assert result.text or len(result.tool_calls) >= 1


# ---------------------------------------------------------------------------
# Strict-schema model regression (tau_manufacturing)
# ---------------------------------------------------------------------------


class TestStrictSchemaModels:
    """Verify tool schema sanitisation for strict providers.

    xAI/Grok rejects tool schemas with ``additionalProperties: {schema}``
    (typed maps) and other Pydantic-generated JSON Schema extras.  The
    ``_convert_tools`` sanitiser must handle these before the API call.
    """

    def test_grok_with_nested_additional_properties(self):
        """Test with the EXACT tool schemas from the failing benchmark run.

        Uses create_order (nested additionalProperties map) and
        create_allocation (minProperties) which have the most complex schemas.
        """
        tools = [
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
            },
            {
                "type": "function",
                "function": {
                    "name": "tau_manufacturing_create_allocation",
                    "description": "Create a new allocation.",
                    "parameters": {
                        "additionalProperties": False,
                        "description": "Input model for create_allocation.",
                        "properties": {
                            "order_id": {"type": "string", "description": "Order ID."},
                            "sku_id": {"type": "string", "description": "SKU ID."},
                            "requested_quantity": {"type": "integer", "minimum": 0},
                            "lot_buffer": {"type": "integer", "minimum": 0},
                            "ddmrp_buffer": {"type": "integer", "minimum": 0},
                            "final_allocation_quantity": {"type": "integer", "minimum": 0},
                            "lots": {
                                "additionalProperties": {
                                    "additionalProperties": False,
                                    "properties": {
                                        "allocated_quantity": {
                                            "type": "integer",
                                            "minimum": 0,
                                        }
                                    },
                                    "required": ["allocated_quantity"],
                                    "title": "AllocationLot",
                                    "type": "object",
                                },
                                "description": "Map lot_id -> allocation.",
                                "minProperties": 1,
                                "title": "Lots",
                                "type": "object",
                            },
                        },
                        "required": [
                            "order_id",
                            "sku_id",
                            "requested_quantity",
                            "lot_buffer",
                            "ddmrp_buffer",
                            "final_allocation_quantity",
                            "lots",
                        ],
                        "title": "CreateAllocationInput",
                        "type": "object",
                    },
                },
            },
        ]
        config = ModelConfig(
            provider="openrouter",
            name="x-ai/grok-4.20",
            temperature=0.6,
            reasoning="medium",
            max_tokens=16384,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a manufacturing agent.",
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="Create an order for SKU-3A9E4 with 200 units",
                ),
            ],
            tools=tools,
            tool_choice="auto",
        )
        assert result.text or len(result.tool_calls) >= 1


# ---------------------------------------------------------------------------
# Models WITHOUT reasoning support
# ---------------------------------------------------------------------------


class TestNonReasoningModels:
    """Models that should work fine with reasoning='off'."""

    @pytest.mark.parametrize(
        "model_name",
        [
            "openai/gpt-4.1-mini",
            "anthropic/claude-sonnet-4",
        ],
    )
    def test_no_reasoning_succeeds(self, model_name: str):
        """Basic call without reasoning should succeed."""
        config = ModelConfig(
            provider="openrouter",
            name=model_name,
            temperature=0.0,
            reasoning="off",
            max_tokens=64,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a test assistant.",
            messages=_SIMPLE_MESSAGES,
        )
        assert result.text


# ---------------------------------------------------------------------------
# Max-tokens parameter
# ---------------------------------------------------------------------------


class TestMaxTokensParameter:
    """Verify that ``max_tokens`` is accepted by different model families."""

    @pytest.mark.parametrize(
        "model_name,max_tokens",
        [
            ("openai/gpt-4.1-mini", 128),
            ("anthropic/claude-sonnet-4", 128),
        ],
    )
    def test_max_tokens_accepted(self, model_name: str, max_tokens: int):
        config = ModelConfig(
            provider="openrouter",
            name=model_name,
            temperature=0.0,
            reasoning="off",
            max_tokens=max_tokens,
        )
        client = LLMClient(config)
        result = client.generate(
            system="You are a test assistant.",
            messages=_SIMPLE_MESSAGES,
        )
        assert result.text


# ---------------------------------------------------------------------------
# Function calling / tool use
# ---------------------------------------------------------------------------

# OpenAI function calling format: tools must be wrapped in
# {"type": "function", "function": {...}}
_SIMPLE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. You MUST call this tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    }
]


class TestFunctionCalling:
    """Verify that tool/function calling works across model families."""

    @pytest.mark.parametrize(
        "model_name",
        [
            "openai/gpt-4.1-mini",
            "anthropic/claude-sonnet-4",
        ],
    )
    def test_tool_use_produces_tool_call(self, model_name: str):
        """Model should emit at least one tool call when prompted."""
        config = ModelConfig(
            provider="openrouter",
            name=model_name,
            temperature=0.0,
            reasoning="off",
            max_tokens=256,
        )
        client = LLMClient(config)
        result = client.generate(
            system=(
                "You are a weather assistant. You MUST use the get_weather tool "
                "for any weather question. Do NOT answer directly."
            ),
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="What is the weather in Paris?",
                )
            ],
            tools=_SIMPLE_TOOL,
            tool_choice="auto",
        )
        assert len(result.tool_calls) >= 1, f"Expected tool call but got text: {result.text!r}"
        assert result.tool_calls[0].name == "get_weather"


# ---------------------------------------------------------------------------
# Config validator end-to-end with real LiteLLM model info
# ---------------------------------------------------------------------------


class TestConfigValidatorLive:
    """End-to-end config validator tests that call ``litellm`` helpers."""

    def test_validate_good_config(self):
        """A well-formed config should pass validation cleanly."""
        from tolokaforge.core.config_validator import validate_run_config

        raw = {
            "models": {
                "agent": {
                    "provider": "openrouter",
                    "name": "anthropic/claude-sonnet-4",
                    "temperature": 0.6,
                    "reasoning": "medium",
                },
                "user": {
                    "provider": "openrouter",
                    "name": "anthropic/claude-sonnet-4",
                    "temperature": 0.0,
                },
            },
            "orchestrator": {
                "workers": 5,
                "repeats": 3,
                "max_turns": 30,
                "runtime": "docker",
            },
            "evaluation": {
                "tasks_glob": "tasks/**/task.yaml",
                "output_dir": "output",
            },
        }
        result = validate_run_config(raw)
        assert result.ok, [str(i) for i in result.errors]

    def test_validate_minimax_reasoning_warns(self):
        """minimax with reasoning should produce a warning."""
        from tolokaforge.core.config_validator import Severity, validate_run_config

        raw = {
            "models": {
                "agent": {
                    "provider": "openrouter",
                    "name": "minimax/minimax-m2.7",
                    "temperature": 0.6,
                    "reasoning": "medium",
                },
                "user": {
                    "provider": "openrouter",
                    "name": "anthropic/claude-sonnet-4",
                    "temperature": 0.0,
                },
            },
            "orchestrator": {
                "workers": 5,
                "repeats": 3,
                "max_turns": 30,
                "runtime": "docker",
            },
            "evaluation": {
                "tasks_glob": "tasks/**/task.yaml",
                "output_dir": "output",
            },
        }
        result = validate_run_config(raw)
        warnings = [
            i for i in result.issues if i.severity == Severity.WARNING and "reasoning" in i.path
        ]
        assert len(warnings) >= 1
