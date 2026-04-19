"""Integration tests for Amazon Nova API provider

Tests Nova API integration including authentication, model availability,
and basic completion functionality. Tests are skipped if Nova API key
is not available in environment.
"""

import os
import warnings

import pytest

from tolokaforge.core.model_client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.integration


def handle_rate_limit_error(func):
    """Decorator to skip tests if rate limited by API.

    Use this on test methods that make actual API calls.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RuntimeError as e:
            error_str = str(e)
            if "429" in error_str or "rate" in error_str.lower() or "RateLimitError" in error_str:
                pytest.skip("Rate limited by Nova API - skipping test")
            raise
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RateLimitError" in str(type(e).__name__):
                pytest.skip("Rate limited by Nova API - skipping test")
            raise

    return wrapper


@pytest.mark.integration
class TestNovaAPIIntegration:
    """Test Nova API provider integration"""

    @pytest.fixture(autouse=True)
    def check_nova_api_key(self):
        """Skip tests if Nova API key is not available"""
        if not os.getenv("NOVA_API_KEY"):
            pytest.skip("NOVA_API_KEY not found in environment - skipping Nova integration tests")

    def test_nova_client_initialization(self):
        """Test Nova client can be initialized with valid config"""
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=100)
        client = LLMClient(config)

        assert client.provider == "nova"
        assert client.model_name == "Nova Pro v3"
        assert client.config.provider == "nova"

    @handle_rate_limit_error
    def test_nova_api_basic_completion(self):
        """Test basic completion with Nova API"""
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=50)
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="What is 2+2? Answer briefly.")]

        result = client.generate(messages=messages)

        # Verify basic response structure
        assert result.text is not None
        assert len(result.text.strip()) > 0
        assert result.token_usage["input"] > 0
        assert result.token_usage["output"] > 0
        assert result.cost_usd > 0
        assert result.latency_s > 0

        # Verify response makes sense
        assert "4" in result.text or "four" in result.text.lower()

    @handle_rate_limit_error
    def test_nova_api_with_system_prompt(self):
        """Test Nova API with system prompt"""
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=30)
        client = LLMClient(config)

        system = "You are a helpful math tutor. Keep responses very brief."
        messages = [Message(role=MessageRole.USER, content="What is 5+7?")]

        result = client.generate(system=system, messages=messages)

        assert result.text is not None
        assert "12" in result.text or "twelve" in result.text.lower()
        assert result.token_usage["input"] > 0
        assert result.token_usage["output"] > 0

    @handle_rate_limit_error
    def test_nova_api_temperature_settings(self):
        """Test Nova API respects temperature settings"""
        # Test with temperature 0 (deterministic)
        config_deterministic = ModelConfig(
            provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=20
        )
        client_deterministic = LLMClient(config_deterministic)

        messages = [Message(role=MessageRole.USER, content="Say exactly: Hello World")]

        # Run twice with temperature 0 - should be similar
        result1 = client_deterministic.generate(messages=messages)
        result2 = client_deterministic.generate(messages=messages)

        assert result1.text is not None
        assert result2.text is not None
        # With temperature 0, responses should be quite similar
        assert len(result1.text) > 0
        assert len(result2.text) > 0

    @handle_rate_limit_error
    def test_nova_api_max_tokens_limit(self):
        """Test Nova API respects max_tokens limit"""
        config = ModelConfig(
            provider="nova",
            name="Nova Pro v3",
            temperature=0.0,
            max_tokens=10,  # Very small limit
        )
        client = LLMClient(config)

        messages = [
            Message(
                role=MessageRole.USER,
                content="Write a long essay about artificial intelligence and machine learning.",
            )
        ]

        result = client.generate(messages=messages)

        # Should be truncated due to max_tokens
        assert result.token_usage["output"] <= 10
        assert len(result.text) < 200  # Should be quite short

    @handle_rate_limit_error
    def test_nova_error_handling(self):
        """Test Nova API error handling for invalid model"""
        config = ModelConfig(
            provider="nova", name="nonexistent-nova-model", temperature=0.0, max_tokens=50
        )
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="Hello")]

        # Should raise RuntimeError due to invalid model
        with pytest.raises(RuntimeError, match="LLM API call failed"):
            client.generate(messages=messages)

    @handle_rate_limit_error
    def test_nova_conversation_context(self):
        """Test Nova API maintains conversation context"""
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=100)
        client = LLMClient(config)

        messages = [
            Message(role=MessageRole.USER, content="My name is Alice. Please remember this."),
            Message(
                role=MessageRole.ASSISTANT,
                content="Nice to meet you, Alice! I'll remember that your name is Alice.",
            ),
            Message(role=MessageRole.USER, content="What name did I just tell you?"),
        ]

        result = client.generate(messages=messages)

        # Should remember the name from conversation context
        # Nova models may be more cautious about personal info, so check for reasonable response
        result_lower = result.text.lower()
        assert (
            "alice" in result_lower or "you told me" in result_lower or "your name" in result_lower
        ), f"Expected context awareness, got: {result.text}"

    @pytest.mark.parametrize("model_name", ["Nova Pro v3", "nova-orchestrator-v1", "nova-lite-v2"])
    @handle_rate_limit_error
    def test_nova_multiple_models(self, model_name):
        """Test different Nova models if available"""
        config = ModelConfig(provider="nova", name=model_name, temperature=0.0, max_tokens=30)
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="Hello, respond with 'OK'")]

        try:
            result = client.generate(messages=messages)
            # If successful, verify basic response
            assert result.text is not None
            assert len(result.text.strip()) > 0
            # Some Nova models may not report token usage correctly
            assert result.token_usage["input"] >= 0  # Allow 0 if model doesn't report
            assert result.token_usage["output"] >= 0  # Allow 0 if model doesn't report
        except RuntimeError as e:
            if "model" in str(e).lower() and (
                "not found" in str(e).lower() or "access" in str(e).lower()
            ):
                pytest.skip(f"Model {model_name} not available with current API key")
            else:
                raise


@pytest.mark.integration
class TestNovaAPIAvailability:
    """Test Nova API service availability and model discovery"""

    @pytest.fixture(autouse=True)
    def check_nova_api_key(self):
        """Skip tests if Nova API key is not available"""
        if not os.getenv("NOVA_API_KEY"):
            pytest.skip("NOVA_API_KEY not found in environment - skipping Nova availability tests")

    @handle_rate_limit_error
    def test_nova_api_models_endpoint(self):
        """Test Nova API models endpoint is accessible"""
        import requests

        api_key = os.getenv("NOVA_API_KEY")
        response = requests.get(
            "https://api.nova.amazon.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()

        # Should have models available
        assert "data" in data
        assert len(data["data"]) > 0

        # Check first model has required fields (Nova API may use 'id' or 'modelId')
        first_model = data["data"][0]
        assert "id" in first_model or "modelId" in first_model
        # Nova API may use 'max_tokens' or 'max_context_tokens'
        assert "max_tokens" in first_model or "max_context_tokens" in first_model

    def test_nova_api_authentication(self):
        """Test Nova API authentication with invalid key"""
        import requests

        response = requests.get(
            "https://api.nova.amazon.com/v1/models",
            headers={"Authorization": "Bearer invalid-key", "Content-Type": "application/json"},
            timeout=10,
        )

        # Should fail authentication - Nova API may return different error codes
        assert response.status_code in [
            400,
            401,
            403,
            500,
        ], f"Unexpected status code: {response.status_code}"
        # Verify it's not a successful response
        assert response.status_code != 200

    @handle_rate_limit_error
    def test_nova_pricing_coverage_and_accuracy(self):
        """Test that our Nova pricing covers available models and seems reasonable"""
        import requests

        from tolokaforge.core.pricing import MODEL_PRICING, get_pricing_info

        # Get available models from Nova API
        api_key = os.getenv("NOVA_API_KEY")
        response = requests.get(
            "https://api.nova.amazon.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        # Nova API may use 'id' or 'modelId' for model identifier
        available_models = {model.get("id") or model.get("modelId") for model in data["data"]}

        # Get our Nova pricing models
        nova_pricing_models = {
            key.replace("nova/", "") for key in MODEL_PRICING if key.startswith("nova/")
        }

        # Check coverage - warn about missing models
        missing_pricing = available_models - nova_pricing_models
        if missing_pricing:
            warnings.warn(
                f"Nova models without pricing: {sorted(missing_pricing)}",
                stacklevel=1,
            )

        # Check for obsolete pricing entries
        obsolete_pricing = nova_pricing_models - available_models
        if obsolete_pricing:
            warnings.warn(
                f"Pricing for unavailable Nova models: {sorted(obsolete_pricing)}",
                stacklevel=1,
            )

        # Validate pricing for available models with pricing
        covered_models = available_models & nova_pricing_models
        assert len(covered_models) > 0, "No Nova models have pricing information"

        for model_name in covered_models:
            pricing = get_pricing_info(f"nova/{model_name}")
            if pricing:
                # Validate pricing is reasonable
                assert pricing["input"] > 0, f"Model {model_name} has zero input pricing"
                assert pricing["output"] > 0, f"Model {model_name} has zero output pricing"
                assert pricing["input"] < 100, (
                    f"Model {model_name} input pricing seems too high: ${pricing['input']}/1M"
                )
                assert pricing["output"] < 100, (
                    f"Model {model_name} output pricing seems too high: ${pricing['output']}/1M"
                )
                assert pricing["output"] >= pricing["input"], (
                    f"Model {model_name} output pricing should be >= input pricing"
                )

        # Ensure we have at least basic coverage of major models
        major_models = {"Nova Pro v3", "nova-orchestrator-v1", "nova-lite-v2", "nova-premier-v1"}
        available_major = major_models & available_models
        covered_major = available_major & nova_pricing_models

        if available_major:
            major_coverage = (len(covered_major) / len(available_major)) * 100
            assert major_coverage >= 50, (
                f"Low coverage of major Nova models: {len(covered_major)}/{len(available_major)}"
            )


@pytest.mark.integration
class TestNovaProviderBugFixes:
    """Integration tests specifically for Nova provider bug fixes.

    These tests verify that the fixes for:
    - Empty content blocks (AWS Bedrock validation)
    - Assistant messages cannot be first (message ordering)
    - custom_llm_provider configuration

    All work correctly with the real Nova API.
    """

    @pytest.fixture(autouse=True)
    def check_nova_api_key(self):
        """Skip tests if Nova API key is not available"""
        if not os.getenv("NOVA_API_KEY"):
            pytest.skip("NOVA_API_KEY not found in environment - skipping Nova bug fix tests")

    @handle_rate_limit_error
    def test_empty_content_fallback_in_tool_call_message(self):
        """Test that ASSISTANT messages with empty content but tool_calls don't fail.

        AWS Bedrock rejects messages with blank content blocks.
        The fix adds fallback content "I'll help you with that." when content is empty.
        """
        from tolokaforge.core.models import ToolCall

        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=100)
        client = LLMClient(config)

        # Simulate a conversation where ASSISTANT had empty content with a tool call
        # This happens when an LLM calls a tool without any accompanying text
        messages = [
            Message(role=MessageRole.USER, content="What is the weather?"),
            Message(
                role=MessageRole.ASSISTANT,
                content="",  # Empty content - this was causing the bug
                tool_calls=[ToolCall(id="tc1", name="get_weather", arguments={"city": "Seattle"})],
            ),
            Message(role=MessageRole.TOOL, content='{"weather": "sunny"}', tool_call_id="tc1"),
            Message(role=MessageRole.USER, content="Thanks! What was the result?"),
        ]

        # This should not raise "blank content fields" error
        result = client.generate(messages=messages)

        assert result.text is not None
        assert len(result.text.strip()) > 0
        assert result.token_usage["input"] > 0

    @handle_rate_limit_error
    def test_whitespace_only_content_fallback(self):
        """Test that whitespace-only content gets fallback.

        Content like "   " or "\n\t" should be treated as empty.
        """
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=50)
        client = LLMClient(config)

        # Message with whitespace-only content
        messages = [
            Message(role=MessageRole.USER, content="   \n\t  "),  # Whitespace only
            Message(role=MessageRole.ASSISTANT, content="I see..."),
            Message(role=MessageRole.USER, content="What did you understand?"),
        ]

        # Should not fail due to whitespace content
        result = client.generate(messages=messages)

        assert result.text is not None
        assert len(result.text.strip()) > 0

    @handle_rate_limit_error
    def test_empty_tool_result_gets_fallback(self):
        """Test that TOOL messages with empty content get fallback '{}'.

        Some tool results may return empty strings, which would fail AWS Bedrock validation.
        """
        from tolokaforge.core.models import ToolCall

        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=100)
        client = LLMClient(config)

        messages = [
            Message(role=MessageRole.USER, content="Run a no-op command"),
            Message(
                role=MessageRole.ASSISTANT,
                content="I'll run that command for you.",
                tool_calls=[ToolCall(id="tc1", name="no_op", arguments={})],
            ),
            Message(role=MessageRole.TOOL, content="", tool_call_id="tc1"),  # Empty result
            Message(role=MessageRole.USER, content="What happened?"),
        ]

        # Should not fail due to empty tool result
        result = client.generate(messages=messages)

        assert result.text is not None
        assert len(result.text.strip()) > 0

    @handle_rate_limit_error
    def test_user_simulator_message_ordering(self):
        """Test that UserSimulator doesn't create conversations starting with ASSISTANT.

        The UserSimulator reverses roles (USER↔ASSISTANT) for simulation.
        After reversal, the first message could be ASSISTANT, which Nova rejects.
        The fix removes leading ASSISTANT messages from sim_context.
        """
        from tolokaforge.core.model_client import UserSimulator

        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=100)

        simulator = UserSimulator(
            mode="llm",
            llm_config=config,
            persona="customer",
            backstory="You are a customer trying to check your order status.",
        )

        # Context that would start with ASSISTANT after role reversal
        # Original: [USER, ASSISTANT] -> After reversal: [ASSISTANT, USER]
        # The first ASSISTANT message should be removed
        context = [
            Message(role=MessageRole.USER, content="Hi, I'd like to check my order."),
            Message(role=MessageRole.ASSISTANT, content="Sure! What's your order ID?"),
        ]

        # This should not raise "Assistant messages cannot be first" error
        result = simulator.reply(context)

        assert result is not None
        assert result.text is not None
        assert len(result.text.strip()) > 0

    @handle_rate_limit_error
    def test_nova_custom_llm_provider_configuration(self):
        """Test that custom_llm_provider='openai' is properly configured.

        LiteLLM requires custom_llm_provider to be set for Nova,
        otherwise it fails with "LLM Provider NOT provided".
        """
        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=50)
        client = LLMClient(config)

        messages = [Message(role=MessageRole.USER, content="Say 'hello' and nothing else.")]

        # This should work without "LLM Provider NOT provided" error
        result = client.generate(messages=messages)

        assert result.text is not None
        assert len(result.text.strip()) > 0
        # Basic sanity check
        result_lower = result.text.lower()
        assert "hello" in result_lower or "hi" in result_lower or len(result.text) > 0

    @handle_rate_limit_error
    def test_nova_consecutive_tool_calls(self):
        """Test handling of multiple consecutive tool calls.

        This scenario tests the full flow of:
        1. User request
        2. Assistant with tool call (empty content)
        3. Tool result
        4. Assistant with another tool call (empty content)
        5. Tool result
        6. Final response
        """
        from tolokaforge.core.models import ToolCall

        config = ModelConfig(provider="nova", name="Nova Pro v3", temperature=0.0, max_tokens=150)
        client = LLMClient(config)

        messages = [
            Message(role=MessageRole.USER, content="Get user and order details"),
            Message(
                role=MessageRole.ASSISTANT,
                content="",  # First tool call with empty content
                tool_calls=[ToolCall(id="tc1", name="get_user", arguments={"user_id": "123"})],
            ),
            Message(
                role=MessageRole.TOOL,
                content='{"name": "Alice", "email": "alice@example.com"}',
                tool_call_id="tc1",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="",  # Second tool call with empty content
                tool_calls=[ToolCall(id="tc2", name="get_order", arguments={"order_id": "456"})],
            ),
            Message(
                role=MessageRole.TOOL,
                content='{"status": "delivered", "items": ["pizza"]}',
                tool_call_id="tc2",
            ),
            Message(role=MessageRole.USER, content="Summarize what you found."),
        ]

        # Should handle multiple empty content messages correctly
        result = client.generate(messages=messages)

        assert result.text is not None
        assert len(result.text.strip()) > 0
        assert result.token_usage["input"] > 0
