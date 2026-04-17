"""Unit tests for LLM provider clients.

These tests verify that the model_client correctly handles various LLM providers
and their specific requirements (message ordering, content validation, etc.)
without requiring API keys or network connectivity.
"""

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.model_client import GenerationResult, LLMClient, UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit


class TestMessageConversion:
    """Test message conversion for provider compatibility.

    These tests verify that empty content handling works correctly for AWS
    Bedrock/Nova compatibility, which rejects messages with blank content blocks.
    """

    def test_empty_assistant_content_gets_fallback(self):
        """AWS Bedrock rejects empty content blocks. Verify fallback is applied."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="tc1", name="some_tool", arguments={"arg": "value"})],
            )
        ]

        converted = client._convert_messages(None, messages)

        # Should have fallback content instead of empty string
        assert converted[0]["content"] != ""
        assert converted[0]["content"] == "I'll help you with that."

    def test_empty_user_content_gets_fallback(self):
        """Verify USER messages with empty content get fallback."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="")]

        converted = client._convert_messages(None, messages)

        assert converted[0]["content"] == "Please continue."

    def test_empty_tool_content_gets_fallback(self):
        """Verify TOOL messages with empty content get fallback."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        messages = [Message(role=MessageRole.TOOL, content="", tool_call_id="tc1")]

        converted = client._convert_messages(None, messages)

        assert converted[0]["content"] == "{}"

    def test_whitespace_only_content_gets_fallback(self):
        """Verify whitespace-only content is treated as empty."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="   \n\t  ")]

        converted = client._convert_messages(None, messages)

        assert converted[0]["content"] == "Please continue."

    def test_normal_content_preserved(self):
        """Verify non-empty content is not modified."""
        config = ModelConfig(provider="openrouter", name="test-model")
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="Hello, world!")]

        converted = client._convert_messages(None, messages)

        assert converted[0]["content"] == "Hello, world!"

    def test_tool_calls_preserved_with_content_fallback(self):
        """Verify tool_calls are preserved when content gets fallback."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="tc1", name="get_user", arguments={"user_id": "123"})],
            )
        ]

        converted = client._convert_messages(None, messages)

        assert "tool_calls" in converted[0]
        assert len(converted[0]["tool_calls"]) == 1
        assert converted[0]["tool_calls"][0]["function"]["name"] == "get_user"

    def test_none_content_gets_fallback(self):
        """Verify None content is handled correctly."""
        config = ModelConfig(provider="nova", name="test-model")
        client = LLMClient(config)

        # Create message and set content to None to test runtime handling
        msg = Message(role=MessageRole.USER, content="")
        msg.content = None  # type: ignore[assignment]
        messages = [msg]

        converted = client._convert_messages(None, messages)

        assert converted[0]["content"] == "Please continue."


class TestUserSimulatorMessageOrdering:
    """Test UserSimulator handles message ordering for Nova compatibility."""

    def test_removes_leading_assistant_message(self):
        """Nova rejects conversations starting with ASSISTANT role."""
        # Create mock LLM client
        mock_llm = MagicMock()
        mock_llm.generate.return_value = GenerationResult(
            text="Hello", tool_calls=[], token_usage={"input": 10, "output": 5}
        )

        config = ModelConfig(provider="nova", name="test-model")
        simulator = UserSimulator(
            mode="llm", llm_config=config, persona="user", backstory="Test user"
        )
        simulator.llm_client = mock_llm

        # Context that would start with ASSISTANT after role reversal
        context = [
            Message(role=MessageRole.USER, content="Previous user message"),
            Message(role=MessageRole.ASSISTANT, content="Agent response"),
        ]

        simulator.reply(context)

        # Verify generate was called
        mock_llm.generate.assert_called_once()

        # Get the messages passed to generate
        call_kwargs = mock_llm.generate.call_args[1]
        sim_messages = call_kwargs["messages"]

        # First message should be USER (from agent's perspective, which is USER for simulator)
        if sim_messages:
            assert sim_messages[0].role == MessageRole.USER


class TestNovaProviderConfiguration:
    """Test Nova provider-specific configuration (unit tests, no API call)."""

    def test_nova_uses_correct_provider_setting(self):
        """Nova should be recognized as nova provider."""
        config = ModelConfig(provider="nova", name="Nova Pro v3")
        client = LLMClient(config)

        assert client.provider == "nova"

    def test_nova_model_name_formatting(self):
        """Nova model names should not have extra prefixes."""
        config = ModelConfig(provider="nova", name="Nova Pro v3")
        client = LLMClient(config)

        # Model name should be unchanged for Nova
        assert client.model_name == "Nova Pro v3"


class TestProviderBaseUrlIsolation:
    """Test that provider-specific base URLs don't interfere with each other.

    Regression test for bug where Nova's api_base was set globally, causing
    OpenRouter requests to be sent to Amazon's Nova API endpoint.
    """

    def test_nova_client_does_not_affect_litellm_api_base(self):
        """Creating a Nova client should not set litellm.api_base globally."""
        import litellm

        # Clear any existing api_base
        original_api_base = getattr(litellm, "api_base", None)
        litellm.api_base = None

        try:
            # Create a Nova client
            nova_config = ModelConfig(provider="nova", name="Nova Pro v3")
            LLMClient(nova_config)

            # litellm.api_base should still be None (Nova base URL is set per-request)
            assert litellm.api_base is None, "Nova client should not set global litellm.api_base"
        finally:
            # Restore original
            litellm.api_base = original_api_base

    def test_openrouter_not_affected_by_nova_client(self):
        """OpenRouter client should not be affected by prior Nova client creation."""
        import litellm

        # Clear any existing api_base
        original_api_base = getattr(litellm, "api_base", None)
        litellm.api_base = None

        try:
            # Create Nova client first (simulates config with Nova agent + OpenRouter user)
            nova_config = ModelConfig(provider="nova", name="Nova Pro v3")
            LLMClient(nova_config)

            # Now create OpenRouter client
            openrouter_config = ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.5")
            LLMClient(openrouter_config)

            # litellm.api_base should NOT point to Nova
            assert (
                litellm.api_base != "https://api.nova.amazon.com/v1"
            ), "OpenRouter client should not inherit Nova's api_base"
        finally:
            # Restore original
            litellm.api_base = original_api_base


class TestProviderModelNameFormatting:
    """Test model name formatting for different providers."""

    def test_standard_provider_model_name(self):
        """Standard providers should prepend provider to model name."""
        config = ModelConfig(provider="anthropic", name="claude-3-sonnet")
        client = LLMClient(config)

        assert client.model_name == "anthropic/claude-3-sonnet"

    def test_already_prefixed_model_name(self):
        """Model names already with provider prefix should not be double-prefixed."""
        config = ModelConfig(provider="openrouter", name="openrouter/anthropic/claude-3")
        client = LLMClient(config)

        # Should keep as-is since it already has prefix
        assert client.model_name == "openrouter/anthropic/claude-3"


class TestReasoningParameter:
    """Verify that the reasoning effort kwarg is built correctly.

    After the fix, ``generate()`` must pass ``reasoning_effort`` as a
    native LiteLLM keyword argument and must NOT inject an ``extra_body``
    reasoning dict.
    """

    def _build_kwargs(self, provider: str, name: str, reasoning: str) -> dict:
        """Create an LLMClient and inspect the kwargs it would build.

        We monkey-patch the module-level ``completion`` reference inside
        ``tolokaforge.core.model_client`` to capture the kwargs.
        """
        import tolokaforge.core.model_client as mc_module

        captured: dict = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "ok"
            mock_resp.choices[0].message.tool_calls = None
            mock_resp.usage = MagicMock()
            mock_resp.usage.prompt_tokens = 10
            mock_resp.usage.completion_tokens = 5
            return mock_resp

        config = ModelConfig(provider=provider, name=name, reasoning=reasoning)
        client = LLMClient(config)

        original = mc_module.completion
        mc_module.completion = fake_completion
        try:
            client.generate(
                system="test",
                messages=[Message(role=MessageRole.USER, content="hello")],
            )
        finally:
            mc_module.completion = original

        return captured

    def test_reasoning_off_no_kwarg(self):
        """When reasoning='off', no reasoning_effort should appear."""
        kwargs = self._build_kwargs("openrouter", "minimax/minimax-m2.7", "off")
        assert "reasoning_effort" not in kwargs
        extra = kwargs.get("extra_body", {})
        assert "reasoning" not in extra

    def test_openrouter_reasoning_sends_effort_and_enabled(self):
        """OpenRouter uses {"reasoning": {"effort": "<level>", "enabled": true}}."""
        kwargs = self._build_kwargs("openrouter", "google/gemini-3-flash-preview", "medium")
        extra = kwargs.get("extra_body", {})
        assert extra.get("reasoning") == {"effort": "medium", "enabled": True}
        # Must NOT use reasoning_effort (LiteLLM rejects it for openrouter)
        assert "reasoning_effort" not in kwargs

    def test_openrouter_reasoning_preserves_effort_level(self):
        """Effort level is forwarded to OpenRouter."""
        kwargs = self._build_kwargs("openrouter", "openai/o3-mini", "high")
        extra = kwargs.get("extra_body", {})
        assert extra.get("reasoning") == {"effort": "high", "enabled": True}
        assert "reasoning_effort" not in kwargs

    def test_openrouter_reasoning_applies_to_all_models(self):
        """OpenRouter reasoning is sent for all models (OpenRouter handles support)."""
        for model in ["anthropic/claude-opus-4.6", "x-ai/grok-4.20", "minimax/minimax-m2.7"]:
            kwargs = self._build_kwargs("openrouter", model, "medium")
            extra = kwargs.get("extra_body", {})
            assert extra.get("reasoning") == {
                "effort": "medium",
                "enabled": True,
            }, f"Failed for {model}"

    def test_native_provider_uses_reasoning_effort_kwarg(self):
        """Non-OpenRouter providers use LiteLLM's native reasoning_effort."""
        kwargs = self._build_kwargs("anthropic", "claude-opus-4.6", "medium")
        assert kwargs.get("reasoning_effort") == "medium"
        extra = kwargs.get("extra_body", {})
        assert "reasoning" not in extra

    def test_default_reasoning_is_off(self):
        """ModelConfig default reasoning must be 'off' (opt-in)."""
        config = ModelConfig(provider="openrouter", name="test/model")
        assert config.reasoning == "off"
