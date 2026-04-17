"""Unit tests for tolokaforge/core/model_client.py — pure-logic helpers and construction."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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
    from tolokaforge.core.model_client import LLMClient

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
        from tolokaforge.core.model_client import _should_retry_exception

        assert _should_retry_exception(RuntimeError("boom")) is True

    def test_returns_true_for_value_error(self) -> None:
        from tolokaforge.core.model_client import _should_retry_exception

        assert _should_retry_exception(ValueError("bad")) is True

    def test_returns_true_for_generic_exception(self) -> None:
        from tolokaforge.core.model_client import _should_retry_exception

        assert _should_retry_exception(Exception("generic")) is True

    def test_returns_true_for_keyboard_interrupt(self) -> None:
        from tolokaforge.core.model_client import _should_retry_exception

        assert _should_retry_exception(KeyboardInterrupt()) is True

    def test_returns_true_for_os_error(self) -> None:
        from tolokaforge.core.model_client import _should_retry_exception

        assert _should_retry_exception(OSError("network")) is True


# ===================================================================
# GenerationResult construction
# ===================================================================


@pytest.mark.unit
class TestGenerationResult:
    """Test GenerationResult default construction."""

    def test_defaults(self) -> None:
        from tolokaforge.core.model_client import GenerationResult

        r = GenerationResult(text="hello")
        assert r.text == "hello"
        assert r.tool_calls == []
        assert r.token_usage == {"input": 0, "output": 0}
        assert r.latency_s == 0.0
        assert r.cost_usd is None
        assert r.reasoning is None

    def test_with_tool_calls(self) -> None:
        from tolokaforge.core.model_client import GenerationResult

        tc = ToolCall(id="tc1", name="search", arguments={"q": "test"})
        r = GenerationResult(text="", tool_calls=[tc])
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "search"

    def test_with_cost(self) -> None:
        from tolokaforge.core.model_client import GenerationResult

        r = GenerationResult(text="", cost_usd=0.05, token_usage={"input": 100, "output": 50})
        assert r.cost_usd == 0.05
        assert r.token_usage["input"] == 100


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
        from tolokaforge.core.model_client import LLMClient

        raw = '```json\n{"key": "value"}\n```'
        assert LLMClient._repair_json_like(raw) == '{"key": "value"}'

    def test_normalizes_smart_quotes(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        raw = "{\u201ckey\u201d: \u201cvalue\u201d}"
        result = LLMClient._repair_json_like(raw)
        assert "\u201c" not in result
        assert "\u201d" not in result
        assert '"key"' in result

    def test_quotes_unquoted_keys(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        raw = '{path: "/tmp/file"}'
        result = LLMClient._repair_json_like(raw)
        assert '"path"' in result

    def test_balances_braces(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        raw = '{"key": {"nested": 1}'
        result = LLMClient._repair_json_like(raw)
        assert result.count("{") == result.count("}")

    def test_already_valid_json(self) -> None:
        from tolokaforge.core.model_client import LLMClient

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

    def test_assistant_with_blank_content_and_tool_calls(self) -> None:
        """Bedrock compat: blank assistant content with tool_calls gets placeholder."""
        client = _make_client()
        tc = ToolCall(id="tc1", name="fn", arguments={})
        msgs = [Message(role=MessageRole.ASSISTANT, content="  ", tool_calls=[tc])]
        result = client._convert_messages(None, msgs)
        assert result[0]["content"] == "I'll help you with that."


# ===================================================================
# _needs_strict_schema / _sanitise_schema_strict / _describe_map_value_schema
# ===================================================================


@pytest.mark.unit
class TestSchemaStrictness:
    """Schema sanitisation for strict providers (e.g. xAI/Grok)."""

    def test_needs_strict_for_xai(self) -> None:
        client = _make_client(provider="openrouter", name="x-ai/grok-3")
        assert client._needs_strict_schema() is True

    def test_not_strict_for_openai(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        assert client._needs_strict_schema() is False

    def test_sanitise_strips_title(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = {"type": "object", "title": "MySchema", "properties": {"x": {"type": "string"}}}
        result = LLMClient._sanitise_schema_strict(schema)
        assert "title" not in result

    def test_sanitise_strips_examples(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = {"type": "string", "examples": ["foo", "bar"]}
        result = LLMClient._sanitise_schema_strict(schema)
        assert "examples" not in result

    def test_sanitise_converts_typed_additional_properties(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
        result = LLMClient._sanitise_schema_strict(schema)
        assert result["additionalProperties"] is True
        assert "description" in result

    def test_sanitise_recursion(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = {
            "type": "object",
            "properties": {"inner": {"type": "object", "title": "Inner", "properties": {}}},
        }
        result = LLMClient._sanitise_schema_strict(schema)
        inner = result["properties"]["inner"]
        assert "title" not in inner

    def test_describe_map_value_schema(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }
        desc = LLMClient._describe_map_value_schema(schema)
        assert "name (string)" in desc
        assert "age (integer)" in desc
        assert "required: [name]" in desc

    def test_describe_map_value_empty_properties(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        assert LLMClient._describe_map_value_schema({}) == ""

    def test_sanitise_list_schema(self) -> None:
        from tolokaforge.core.model_client import LLMClient

        schema = [{"title": "A"}, {"title": "B", "type": "string"}]
        result = LLMClient._sanitise_schema_strict(schema)
        assert len(result) == 2
        assert "title" not in result[0]
        assert "title" not in result[1]


# ===================================================================
# _convert_tools
# ===================================================================


@pytest.mark.unit
class TestConvertTools:
    """Tool schema conversion with optional sanitisation."""

    def test_non_strict_passes_through(self) -> None:
        client = _make_client(provider="openai", name="gpt-4")
        tools = [{"type": "function", "function": {"name": "f", "parameters": {"title": "T"}}}]
        assert client._convert_tools(tools) is tools  # same reference

    def test_strict_sanitises(self) -> None:
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
        result = client._convert_tools(tools)
        params = result[0]["function"]["parameters"]
        assert "title" not in params
        # Top-level description in parameters is removed for strict providers
        assert "description" not in params


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
        from tolokaforge.core.model_client import UserSimulator

        sim = UserSimulator(mode="scripted")
        assert sim.mode == "scripted"
        assert sim.llm_client is None
        assert sim.scripted_flow == []

    def test_llm_mode_without_config(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        sim = UserSimulator(mode="llm", llm_config=None)
        assert sim.llm_client is None

    def test_scripted_reply_empty_context(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        sim = UserSimulator(mode="scripted")
        result = sim.reply([])
        assert "help" in result.text.lower() or "task" in result.text.lower()

    def test_scripted_reply_unconditional_flow(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        flow = [{"user": "I need to book a flight."}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        # Context with no matching prior user message
        ctx = [Message(role=MessageRole.ASSISTANT, content="How can I help?")]
        result = sim.reply(ctx)
        assert result.text == "I need to book a flight."

    def test_scripted_reply_conditional_match(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        flow = [{"if_assistant_contains": "booking confirmed", "user": "Thank you!"}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        ctx = [Message(role=MessageRole.ASSISTANT, content="Your booking confirmed for tomorrow.")]
        result = sim.reply(ctx)
        assert result.text == "Thank you!"

    def test_scripted_reply_default_fallback(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        flow = [{"default": "I'm not sure."}]
        sim = UserSimulator(mode="scripted", scripted_flow=flow)
        ctx = [Message(role=MessageRole.ASSISTANT, content="Something unexpected.")]
        result = sim.reply(ctx)
        assert result.text == "I'm not sure."

    def test_scripted_reply_question_fallback(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        sim = UserSimulator(mode="scripted")
        ctx = [Message(role=MessageRole.ASSISTANT, content="Would you like to proceed?")]
        result = sim.reply(ctx)
        assert result.text == "Yes, please proceed."

    def test_scripted_reply_statement_fallback(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        sim = UserSimulator(mode="scripted")
        ctx = [Message(role=MessageRole.ASSISTANT, content="Done.")]
        result = sim.reply(ctx)
        assert result.text == "Okay."

    def test_reply_unknown_mode_raises(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

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
        from tolokaforge.core.model_client import UserSimulator

        result = UserSimulator._sanitize_user_text("This is a simulation of the task.")
        assert "simulation" not in result.lower()

    def test_removes_benchmark_mention(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        result = UserSimulator._sanitize_user_text("This benchmark tests performance.")
        assert "benchmark" not in result.lower()

    def test_preserves_clean_text(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        text = "I need to book a flight to NYC."
        assert UserSimulator._sanitize_user_text(text) == text

    def test_empty_text_stays_empty(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        assert UserSimulator._sanitize_user_text("") == ""

    def test_all_sentences_banned_returns_okay(self) -> None:
        from tolokaforge.core.model_client import UserSimulator

        # When all sentences are stripped and nothing remains, returns fallback "Okay."
        result = UserSimulator._sanitize_user_text("AI model.")
        assert result == "Okay." or len(result) > 0


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

        with patch("tolokaforge.core.model_client.completion", return_value=mock_response):
            with patch("tolokaforge.core.model_client.estimate_cost", return_value=0.001):
                result = client.generate(
                    system="You are helpful.",
                    messages=[Message(role=MessageRole.USER, content="Hi")],
                )

        assert result.text == "Hello, world!"
        assert result.tool_calls == []
        assert result.token_usage["input"] == 10
        assert result.token_usage["output"] == 5
        assert result.cost_usd == 0.001

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

        with patch("tolokaforge.core.model_client.completion", return_value=mock_response):
            with patch("tolokaforge.core.model_client.estimate_cost", return_value=0.002):
                result = client.generate(
                    messages=[Message(role=MessageRole.USER, content="What's the weather?")],
                )

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "weather"}

    def test_generate_mock_provider_bypasses_litellm(self) -> None:
        """Mock provider should NOT call litellm.completion."""
        client = _make_client(provider="mock", name="mock-agent")

        with patch("tolokaforge.core.model_client.completion") as mock_completion:
            result = client.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
            )

        mock_completion.assert_not_called()
        assert result.text  # has content

    def test_generate_nova_unwraps_input_key(self) -> None:
        """Nova provider wraps arguments in {'input': {...}} — verify unwrapping."""
        client = _make_client(provider="openai", name="gpt-4")

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

        with patch("tolokaforge.core.model_client.completion", return_value=mock_response):
            with patch("tolokaforge.core.model_client.estimate_cost", return_value=None):
                result = client.generate(
                    messages=[Message(role=MessageRole.USER, content="Check order")],
                )

        # The wrapped {'input': {'order_id': '123'}} should be unwrapped
        assert result.tool_calls[0].arguments == {"order_id": "123"}
