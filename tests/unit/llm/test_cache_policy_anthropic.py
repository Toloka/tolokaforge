"""Unit tests for :class:`AnthropicEphemeralCache` and its client wiring.

Guards the contract that Anthropic-family presets emit ``cache_control:
{type: ephemeral}`` markers on the system prompt + tools array before the
request leaves :class:`~tolokaforge.core.llm.client.LLMClient`, and that the
policy's ``apply_messages`` hook runs on the wire-shape messages returned by
``_convert_messages``.

Two groups:

* Pure policy-level tests against
  :class:`tolokaforge.core.llm.cache_policy.AnthropicEphemeralCache` and
  :class:`~tolokaforge.core.llm.cache_policy.NoCache` exercising the
  canonical litellm content-block shape on every input variant (string,
  list-of-blocks, empty, None, idempotency, tools) and the
  ``apply_messages`` passthrough contract.

* Client-level tests that patch ``litellm.completion`` and assert the
  request payload — Anthropic presets produce content-blocks + ``cache_control``,
  non-Anthropic presets pass the system prompt through as a plain string with
  no ``cache_control`` anywhere, and the ``apply_messages`` hook runs on
  the exact wire list ``litellm.completion`` receives.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.cache_policy import AnthropicEphemeralCache, NoCache
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# AnthropicEphemeralCache — pure policy contract
# ---------------------------------------------------------------------------


class TestAnthropicEphemeralCachePolicy:
    """Exercises the Anthropic ephemeral cache policy in isolation."""

    def test_string_system_becomes_single_content_block_with_marker(self) -> None:
        policy = AnthropicEphemeralCache()
        system, tools, messages = policy.apply(
            system="You are a helpful assistant.",
            tools=None,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert system == [
            {
                "type": "text",
                "text": "You are a helpful assistant.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        # Tools passthrough (None in, None out)
        assert tools is None
        # Messages are unchanged — Stage 6 only caches system + tools.
        assert messages == [{"role": "user", "content": "hi"}]

    def test_tools_array_marker_on_last_entry_only(self) -> None:
        policy = AnthropicEphemeralCache()
        tools_in = [
            {"type": "function", "function": {"name": "a", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {}}},
        ]
        _, tools_out, _ = policy.apply(system=None, tools=tools_in, messages=[])
        assert tools_out is not None
        assert "cache_control" not in tools_out[0]
        assert tools_out[-1]["cache_control"] == {"type": "ephemeral"}
        # First tool is untouched apart from the shallow copy — no stray keys.
        assert tools_out[0] == {"type": "function", "function": {"name": "a", "parameters": {}}}

    def test_list_of_content_blocks_system_marks_last_block(self) -> None:
        policy = AnthropicEphemeralCache()
        system_in = [
            {"type": "text", "text": "short preamble"},
            {"type": "text", "text": "long policy blob"},
        ]
        system_out, _, _ = policy.apply(system=system_in, tools=None, messages=[])
        assert isinstance(system_out, list)
        assert "cache_control" not in system_out[0]
        assert system_out[-1]["cache_control"] == {"type": "ephemeral"}
        # Text payloads untouched.
        assert system_out[0]["text"] == "short preamble"
        assert system_out[-1]["text"] == "long policy blob"

    def test_none_system_and_none_tools_noop(self) -> None:
        policy = AnthropicEphemeralCache()
        messages_in = [{"role": "user", "content": "hi"}]
        system, tools, messages = policy.apply(system=None, tools=None, messages=messages_in)
        assert system is None
        assert tools is None
        assert messages is messages_in  # exact same reference — pure passthrough

    def test_empty_string_system_noop(self) -> None:
        """Empty string must not be wrapped into a cached empty block."""
        policy = AnthropicEphemeralCache()
        system, _, _ = policy.apply(system="", tools=None, messages=[])
        assert system == ""

    def test_empty_tools_list_noop(self) -> None:
        """Empty tools list stays empty — nothing to cache."""
        policy = AnthropicEphemeralCache()
        _, tools, _ = policy.apply(system=None, tools=[], messages=[])
        assert tools == []

    def test_idempotent_on_string_system(self) -> None:
        """Running apply twice must not stack markers or re-wrap content-blocks."""
        policy = AnthropicEphemeralCache()
        once, _, _ = policy.apply(system="foo", tools=None, messages=[])
        twice, _, _ = policy.apply(system=once, tools=None, messages=[])
        assert once == twice
        assert isinstance(twice, list)
        assert len(twice) == 1
        assert twice[0]["cache_control"] == {"type": "ephemeral"}

    def test_idempotent_on_tools_array(self) -> None:
        policy = AnthropicEphemeralCache()
        tools_in = [{"type": "function", "function": {"name": "only", "parameters": {}}}]
        _, once, _ = policy.apply(system=None, tools=tools_in, messages=[])
        _, twice, _ = policy.apply(system=None, tools=once, messages=[])
        assert once == twice
        # Only the last (and only) tool carries the marker, exactly once.
        assert once is not None and twice is not None
        assert twice[-1]["cache_control"] == {"type": "ephemeral"}

    def test_pre_existing_cache_control_on_last_tool_is_replaced(self) -> None:
        """A caller-supplied cache_control must be overwritten, not stacked."""
        policy = AnthropicEphemeralCache()
        tools_in = [
            {"type": "function", "function": {"name": "a"}},
            {
                "type": "function",
                "function": {"name": "b"},
                "cache_control": {"type": "persistent"},  # contrived prior marker
            },
        ]
        _, tools_out, _ = policy.apply(system=None, tools=tools_in, messages=[])
        assert tools_out is not None
        assert tools_out[-1]["cache_control"] == {"type": "ephemeral"}

    def test_invalid_system_type_raises_typeerror(self) -> None:
        policy = AnthropicEphemeralCache()
        with pytest.raises(TypeError, match="system"):
            policy.apply(system=42, tools=None, messages=[])  # type: ignore[arg-type]

    def test_does_not_mutate_caller_inputs(self) -> None:
        """Policy must operate on shallow copies — caller's dicts untouched."""
        policy = AnthropicEphemeralCache()
        original_tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ]
        snapshot = [dict(t) for t in original_tools]
        _, _, _ = policy.apply(system=None, tools=original_tools, messages=[])
        assert original_tools == snapshot
        assert "cache_control" not in original_tools[-1]

    def test_nocache_is_inert(self) -> None:
        """Sanity check that the default policy returns inputs verbatim."""
        policy = NoCache()
        sys_in = "prompt"
        tools_in: list[dict] = [{"type": "function", "function": {"name": "x"}}]
        msgs_in: list[dict] = [{"role": "user", "content": "hi"}]
        sys_out, tools_out, msgs_out = policy.apply(system=sys_in, tools=tools_in, messages=msgs_in)
        assert sys_out == sys_in
        assert tools_out is tools_in
        assert msgs_out is msgs_in

    def test_apply_messages_nocache_returns_input_identity(self) -> None:
        """``NoCache.apply_messages`` is a pure passthrough — same object identity."""
        policy = NoCache()
        msgs: list[dict] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert policy.apply_messages(msgs) is msgs


# ---------------------------------------------------------------------------
# LLMClient wiring — cache policy runs inside generate() for Anthropic presets
# ---------------------------------------------------------------------------


def _mock_completion_response() -> MagicMock:
    """Build a minimal litellm completion response that the client accepts."""
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = "ok"
    # Disable tool_calls extraction — the mock's default MagicMock is truthy.
    message.tool_calls = None
    # Reasoning codec extracts from message; an empty message means no reasoning.
    message.thinking_blocks = None
    message.reasoning_content = None
    choice.message = message
    response.choices = [choice]
    # Minimal usage shape — UsageExtractor tolerates missing sub-fields.
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


class TestLLMClientCachePolicyWiring:
    """Patches ``litellm.completion`` and asserts the request payload shape."""

    @staticmethod
    def _single_tool() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Returns the current time.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def test_anthropic_request_carries_content_blocks_and_cache_control(self) -> None:
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        tools = self._single_tool()

        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _mock_completion_response()
            client.generate(
                system="You help.",
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=tools,
            )
            assert mock_completion.call_count == 1
            kwargs = mock_completion.call_args.kwargs

        # System turn: content is a list of content-blocks with ephemeral cache
        # marker on the last block (the canonical litellm Anthropic shape).
        system_msg = kwargs["messages"][0]
        assert system_msg["role"] == "system"
        content = system_msg["content"]
        assert isinstance(content, list)
        assert content[-1]["cache_control"] == {"type": "ephemeral"}
        assert content[-1]["text"] == "You help."

        # Tools: cache_control marker on the last (and only) tool.
        assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_openai_request_keeps_plain_string_system_no_cache_control(self) -> None:
        """OpenAI presets (``default`` fallback) carry ``NoCache`` — zero transform."""
        client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o"))
        tools = self._single_tool()

        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _mock_completion_response()
            client.generate(
                system="You help.",
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=tools,
            )
            kwargs = mock_completion.call_args.kwargs

        system_msg = kwargs["messages"][0]
        assert system_msg["role"] == "system"
        # Non-Anthropic presets must keep the system prompt as a plain string.
        assert system_msg["content"] == "You help."
        # No cache_control on any tool.
        for tool in kwargs["tools"]:
            assert "cache_control" not in tool

    def test_anthropic_generic_preset_also_applies_ephemeral_cache(self) -> None:
        """The generic ``anthropic`` preset (4.5/4.6) must wire the same cache."""
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"))
        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _mock_completion_response()
            client.generate(
                system="Short system.",
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=self._single_tool(),
            )
            kwargs = mock_completion.call_args.kwargs

        system_msg = kwargs["messages"][0]
        assert isinstance(system_msg["content"], list)
        assert system_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_apply_messages_wired_after_convert_messages(self) -> None:
        """The wire hook runs on the exact list returned by ``_convert_messages``.

        Wraps the real ``AnthropicEphemeralCache.apply_messages`` in a
        ``MagicMock`` so the existing content-blocks + cache_control end-to-end
        assertion (see
        :meth:`test_anthropic_request_carries_content_blocks_and_cache_control`)
        keeps its guarantees while we also observe the call.
        """
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        policy = client.capabilities.cache_policy
        assert isinstance(policy, AnthropicEphemeralCache)
        real_apply_messages = policy.apply_messages
        real_convert = client._convert_messages
        captured: dict[str, list[dict]] = {}

        def convert_capturing(system, messages):  # type: ignore[no-untyped-def]
            out = real_convert(system, messages)
            captured["wire"] = out
            return out

        with (
            patch("tolokaforge.core.llm.client.completion") as mock_completion,
            patch.object(policy, "apply_messages", wraps=real_apply_messages) as mock_apply,
            patch.object(client, "_convert_messages", side_effect=convert_capturing),
        ):
            mock_completion.return_value = _mock_completion_response()
            client.generate(
                system="You help.",
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=self._single_tool(),
            )
            assert mock_apply.call_count == 1
            passed = mock_apply.call_args.args[0]
            assert passed is captured["wire"]
            # And the wire hook's return value is what litellm.completion sees.
            assert mock_completion.call_args.kwargs["messages"] is passed

    def test_effective_system_prompt_remains_concatenated_string(self) -> None:
        """``GenerationResult.effective_system_prompt`` must stay a str for
        downstream consumers (trajectory writer, analytics) even when the
        wire-level request carries content-blocks."""
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _mock_completion_response()
            result = client.generate(
                system="You help.",
                messages=[Message(role=MessageRole.USER, content="hi")],
                tools=self._single_tool(),
            )

        assert isinstance(result.effective_system_prompt, str)
        assert result.effective_system_prompt == "You help."
