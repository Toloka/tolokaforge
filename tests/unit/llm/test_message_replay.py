"""Stage 4 unit test — thinking-block replay in ``_convert_messages`` (P4b).

Anthropic's interleaved-thinking contract requires the prior assistant turn's
thinking blocks (including the ``signature`` on each one) to be echoed back
in the next request's assistant message dict. Today
:meth:`tolokaforge.core.llm.client.LLMClient._convert_messages` serialises
only ``content`` + ``tool_calls`` — ``thinking_blocks`` are dropped, which is
exactly the root cause of the Opus 4.7 regression identified in
[`plans/eval_output_new_diagnosis.md`](../../../plans/eval_output_new_diagnosis.md)
Part 4.

These tests pin the post-fix contract: the assistant dict produced by
``_convert_messages`` carries a ``thinking_blocks`` first-class field whenever
the capability-level :class:`ReasoningCodec` emits one — and emits *nothing*
for non-reasoning codecs or empty reasoning. No provider-specific branches
in Python; the codec is the only abstraction the client interacts with.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import LLMClient
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _anthropic_client(model: str = "anthropic/claude-opus-4.7") -> LLMClient:
    """Build an ``LLMClient`` with an Anthropic reasoning codec wired."""
    return LLMClient(ModelConfig(provider="openrouter", name=model))


def _openai_client(model: str = "openai/gpt-4o") -> LLMClient:
    """Build an ``LLMClient`` whose codec is ``NoReasoningCodec``.

    ``openai/gpt-4o`` hits neither the ``anthropic`` nor the ``openai_gpt5``
    preset — it falls back to the ``default`` preset with
    ``reasoning_codec: none``.
    """
    return LLMClient(ModelConfig(provider="openrouter", name=model))


def _assistant_dict(converted: list[dict]) -> dict:
    return next(m for m in converted if m["role"] == "assistant")


# ---------------------------------------------------------------------------
# Primary contract — Anthropic codec round-trips thinking blocks onto replay
# ---------------------------------------------------------------------------


def test_anthropic_assistant_message_includes_thinking_blocks_on_replay() -> None:
    """P4b: ``_convert_messages`` must splice ``thinking_blocks`` onto the
    assistant dict when the reasoning codec produces them."""
    client = _anthropic_client()
    prior_reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="thinking",
                text="I should check stock first.",
                signature="sig_ABC123==",
            ),
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data="enc_XYZ"),
        ),
        summary="Planning",
    )
    messages = [
        Message(role=MessageRole.USER, content="What's in stock?"),
        Message(
            role=MessageRole.ASSISTANT,
            content="Let me check.",
            reasoning=prior_reasoning,
            tool_calls=[ToolCall(id="t1", name="get_stock", arguments={})],
        ),
        Message(role=MessageRole.TOOL, content='{"sku":"A","qty":5}', tool_call_id="t1"),
    ]

    converted = client._convert_messages(system="You help.", messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "thinking_blocks" in assistant_dict  # P4b: echo signed blocks on next turn
    blocks = assistant_dict["thinking_blocks"]
    assert len(blocks) == 2
    assert blocks[0] == {
        "type": "thinking",
        "thinking": "I should check stock first.",
        "signature": "sig_ABC123==",
    }
    assert blocks[1] == {"type": "redacted_thinking", "data": "enc_XYZ"}


# ---------------------------------------------------------------------------
# Negative contract — no ``thinking_blocks`` key when it shouldn't exist
# ---------------------------------------------------------------------------


def test_assistant_without_reasoning_has_no_thinking_blocks_key() -> None:
    """``msg.reasoning is None`` → no ``thinking_blocks`` key (no empty lists)."""
    client = _anthropic_client()
    messages = [
        Message(role=MessageRole.USER, content="Hi"),
        Message(role=MessageRole.ASSISTANT, content="Hello.", reasoning=None),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "thinking_blocks" not in assistant_dict


def test_no_reasoning_codec_never_emits_thinking_blocks() -> None:
    """A non-reasoning preset must not emit ``thinking_blocks`` even if
    ``msg.reasoning`` is populated (e.g. from a cross-provider replay)."""
    client = _openai_client()
    # Sanity: capability must indeed be the no-op codec.
    from tolokaforge.core.llm.reasoning_codec import NoReasoningCodec

    assert isinstance(client.capabilities.reasoning_codec, NoReasoningCodec)

    stray_reasoning = StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text="leaked", signature="sig"),),
    )
    messages = [
        Message(role=MessageRole.USER, content="Hi"),
        Message(role=MessageRole.ASSISTANT, content="Hello.", reasoning=stray_reasoning),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "thinking_blocks" not in assistant_dict


def test_empty_blocks_tuple_emits_no_thinking_blocks_key() -> None:
    """``StructuredReasoning(blocks=())`` → ``encode_for_replay`` returns ``{}``
    so no ``thinking_blocks`` key is added."""
    client = _anthropic_client()
    empty_reasoning = StructuredReasoning(blocks=(), summary="only summary")
    messages = [
        Message(role=MessageRole.USER, content="Hi"),
        Message(role=MessageRole.ASSISTANT, content="Hello.", reasoning=empty_reasoning),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "thinking_blocks" not in assistant_dict


# ---------------------------------------------------------------------------
# Multi-turn — every assistant with reasoning gets its own thinking_blocks
# ---------------------------------------------------------------------------


def test_multi_turn_each_assistant_keeps_its_own_thinking_blocks() -> None:
    """Three assistant messages in sequence, each with its own reasoning —
    the output preserves per-turn fidelity."""
    client = _anthropic_client()

    def _r(text: str, sig: str) -> StructuredReasoning:
        return StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text=text, signature=sig),),
        )

    messages = [
        Message(role=MessageRole.USER, content="Start."),
        Message(role=MessageRole.ASSISTANT, content="a1", reasoning=_r("think1", "sig1")),
        Message(role=MessageRole.USER, content="next"),
        Message(role=MessageRole.ASSISTANT, content="a2", reasoning=_r("think2", "sig2")),
        Message(role=MessageRole.USER, content="next"),
        Message(role=MessageRole.ASSISTANT, content="a3", reasoning=_r("think3", "sig3")),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dicts = [m for m in converted if m["role"] == "assistant"]
    assert len(assistant_dicts) == 3
    assert [d["thinking_blocks"][0]["thinking"] for d in assistant_dicts] == [
        "think1",
        "think2",
        "think3",
    ]
    assert [d["thinking_blocks"][0]["signature"] for d in assistant_dicts] == [
        "sig1",
        "sig2",
        "sig3",
    ]


# ---------------------------------------------------------------------------
# Replay also works on assistant turns that carry tool_calls
# ---------------------------------------------------------------------------


def test_thinking_blocks_present_alongside_tool_calls() -> None:
    """The replay merge must not conflict with the existing ``tool_calls``
    serialisation — both keys coexist on the same dict."""
    client = _anthropic_client()
    reasoning = StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text="plan", signature="sig"),),
    )
    messages = [
        Message(role=MessageRole.USER, content="Do X"),
        Message(
            role=MessageRole.ASSISTANT,
            content="calling",
            reasoning=reasoning,
            tool_calls=[ToolCall(id="t", name="do_x", arguments={"arg": 1})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "tool_calls" in assistant_dict
    assert "thinking_blocks" in assistant_dict
    assert assistant_dict["tool_calls"][0]["function"]["name"] == "do_x"
    assert assistant_dict["thinking_blocks"][0]["signature"] == "sig"


# ---------------------------------------------------------------------------
# Empty-content filler is gated by ``ToolContentPolicy.inject_empty_assistant_filler``
# ---------------------------------------------------------------------------


def test_gemini_empty_assistant_content_with_tool_calls_stays_empty() -> None:
    """Regression: harness used to inject ``"I'll help you with that."`` for
    every empty assistant-with-tool_calls turn. Gemini pattern-matches it and
    echoes it back as its own content (~26-38% of trials in the 2026-04-30 OTS
    eval). The Gemini preset's ``ToolContentPolicy`` now declares
    ``inject_empty_assistant_filler=False`` so the wire content stays empty.
    """
    client = LLMClient(ModelConfig(provider="openrouter", name="google/gemini-3.1-pro-preview"))
    messages = [
        Message(role=MessageRole.USER, content="Look up record ABC."),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t1", name="lookup", arguments={"id": "ABC"})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert assistant_dict["content"] == "", (
        "Gemini preset must NOT inject the filler — got "
        f"{assistant_dict['content']!r}. The filler poisons context: Gemini "
        "echoes it back on subsequent turns."
    )
    # Tool calls survive — only content is gated.
    assert "tool_calls" in assistant_dict
    assert assistant_dict["tool_calls"][0]["function"]["name"] == "lookup"


def test_openai_default_empty_assistant_content_with_tool_calls_stays_empty() -> None:
    """The default preset (``OpenAIContent``) also has the filler off — the
    OpenAI chat-completions API accepts empty assistant content alongside
    ``tool_calls``. No filler tax for the broad fleet."""
    client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o"))
    messages = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t", name="tool", arguments={})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assert _assistant_dict(converted)["content"] == ""


def test_nova_empty_assistant_content_with_tool_calls_gets_filler() -> None:
    """Nova/Bedrock validation rejects empty assistant content alongside
    ``tool_calls``. The Nova preset's ``ToolContentPolicy`` declares
    ``inject_empty_assistant_filler=True`` so the original Bedrock fix
    (commit 73e01e9e6) is preserved exactly where it's needed."""
    client = LLMClient(ModelConfig(provider="nova", name="nova-pro"))
    messages = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t", name="tool", arguments={})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assert _assistant_dict(converted)["content"] == "I'll help you with that."


def test_non_empty_assistant_content_unchanged_regardless_of_policy() -> None:
    """When the model emits real content, it survives untouched on every preset.
    The filler gate only fires on empty / whitespace-only content."""
    real_content = "I removed the priority boarding ancillary. I will now process the refund."
    for provider, name in [
        ("openrouter", "google/gemini-3.1-pro-preview"),
        ("openrouter", "openai/gpt-4o"),
        ("nova", "nova-pro"),
    ]:
        client = LLMClient(ModelConfig(provider=provider, name=name))
        messages = [
            Message(role=MessageRole.USER, content="x"),
            Message(
                role=MessageRole.ASSISTANT,
                content=real_content,
                tool_calls=[ToolCall(id="t", name="t", arguments={})],
            ),
        ]
        converted = client._convert_messages(system=None, messages=messages)
        got = _assistant_dict(converted)["content"]
        assert got == real_content, f"{name}: real content got mutated to {got!r}"


# ---------------------------------------------------------------------------
# Gemini placeholder-signature wire shape — 2026-05-21 A/B
#
# Mirrors the codec-level tests in ``test_reasoning_codec_gemini.py`` but
# at the higher-level ``_convert_messages`` boundary so we can prove the
# capability override flows through the preset stack and reaches the
# outgoing ``litellm_msg`` dict. This is the contract that matters for
# what OpenRouter actually sees on the wire.
# ---------------------------------------------------------------------------


_GEMINI_PLACEHOLDER_BASE64 = "ZTI0ODMwYTctNWNkNi00MmZlLTk5OGItZWU1MzllNzJiOWMz"


def _gemini_placeholder_reasoning() -> StructuredReasoning:
    """A turn-N assistant reasoning state where the only block is the
    48-char placeholder OpenRouter emits when Gemini reported no real
    thinking — the exact shape that triggers the codec's placeholder
    guard."""
    return StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_GEMINI_PLACEHOLDER_BASE64,
            ),
        ),
        transport="openrouter",
    )


def test_gemini_default_drops_placeholder_from_outgoing_message() -> None:
    """Default Gemini preset (``drop_placeholder_signature=True`` implicit)
    must NOT emit ``reasoning_details`` on the wire when the assistant
    turn carried only the placeholder. Pins PR #101's contract end-to-end."""
    client = LLMClient(ModelConfig(provider="openrouter", name="google/gemini-3.1-pro-preview"))
    messages = [
        Message(role=MessageRole.USER, content="Look up booking BK-001."),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            reasoning=_gemini_placeholder_reasoning(),
            tool_calls=[ToolCall(id="t1", name="lookup", arguments={"id": "BK-001"})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "reasoning_details" not in assistant_dict, (
        "Default codec must drop the 48-char placeholder. Found "
        f"reasoning_details on the wire: {assistant_dict.get('reasoning_details')!r}"
    )


def test_gemini_drop_disabled_keeps_placeholder_on_outgoing_message() -> None:
    """With ``gemini_drop_placeholder_signature=False`` set via capability
    overrides, the placeholder must round-trip back as
    ``reasoning_details: [{type: reasoning.encrypted, data: <48-char blob>}]``
    on the outgoing assistant dict. This is the A/B knob for the
    2026-05-21 OTS regression hypothesis."""
    client = LLMClient(
        ModelConfig(
            provider="openrouter",
            name="google/gemini-3.1-pro-preview",
            capabilities={"gemini_drop_placeholder_signature": False},
        )
    )
    messages = [
        Message(role=MessageRole.USER, content="Look up booking BK-001."),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            reasoning=_gemini_placeholder_reasoning(),
            tool_calls=[ToolCall(id="t1", name="lookup", arguments={"id": "BK-001"})],
        ),
    ]

    converted = client._convert_messages(system=None, messages=messages)
    assistant_dict = _assistant_dict(converted)

    assert "reasoning_details" in assistant_dict, (
        "Override should let the placeholder round-trip but the outgoing "
        "assistant dict has no reasoning_details key."
    )
    details = assistant_dict["reasoning_details"]
    assert len(details) == 1
    assert details[0]["type"] == "reasoning.encrypted"
    assert details[0]["data"] == _GEMINI_PLACEHOLDER_BASE64


def test_gemini_unknown_capability_key_still_rejected() -> None:
    """Sanity check: the override-keys allowlist still rejects typos.
    Guards against accidentally widening the surface when adding the
    new key."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Unknown capability override keys"):
        LLMClient(
            ModelConfig(
                provider="openrouter",
                name="google/gemini-3.1-pro-preview",
                capabilities={"gemini_drop_placeholder_signatuer": False},  # typo
            )
        )
