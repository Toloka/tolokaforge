"""Unit tests for :class:`AnthropicReasoningCodec` (Stage 3, fixes P4a + P4c).

The codec must preserve every byte of the provider's ``thinking_blocks``
shape: block types (``thinking`` / ``redacted_thinking``), signatures, and
``redacted_thinking.data`` payloads. These are the bytes Anthropic requires
us to echo back unchanged on the next turn to sustain interleaved thinking.

Fixtures live in [`tests/unit/llm/fixtures/`](fixtures/) and are synthetic-but-
realistic captures of the litellm ``ModelResponse.choices[0].message`` shape
documented at https://docs.litellm.ai/docs/reasoning_content.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import AnthropicReasoningCodec
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((_FIXTURES_DIR / name).read_text())
    payload.pop("_comment", None)
    return payload


def _message_from_fixture(name: str) -> SimpleNamespace:
    """Build a litellm-message-shaped object from a fixture dict.

    ``SimpleNamespace`` mirrors attribute-access semantics used by the
    real ``litellm.ModelResponse.choices[0].message`` without requiring a
    live litellm import in unit tests.
    """
    payload = _load_fixture(name)
    # tool_calls stays as plain dicts (enough for codec; LLMClient tests mock
    # this separately when needed).
    return SimpleNamespace(**payload)


# ---------------------------------------------------------------------------
# Fixture-backed extraction tests — guard P4a (signatures) + P4c (thinking_blocks
# preferred over reasoning_content)
# ---------------------------------------------------------------------------


class TestAnthropicExtractVisibleThinking:
    """``anthropic_thinking_response.json`` — visible thinking + redacted block."""

    def _extracted(self) -> StructuredReasoning:
        message = _message_from_fixture("anthropic_thinking_response.json")
        reasoning = AnthropicReasoningCodec().extract(message)
        assert reasoning is not None
        return reasoning

    def test_returns_structured_reasoning(self) -> None:
        reasoning = self._extracted()
        assert isinstance(reasoning, StructuredReasoning)

    def test_extracts_at_least_two_blocks(self) -> None:
        reasoning = self._extracted()
        assert len(reasoning.blocks) >= 2

    def test_first_block_is_thinking_with_signature(self) -> None:
        fixture = _load_fixture("anthropic_thinking_response.json")
        reasoning = self._extracted()
        first = reasoning.blocks[0]
        assert first.type == "thinking"
        assert first.text == fixture["thinking_blocks"][0]["thinking"]
        # Byte-for-byte signature preservation is the entire point.
        assert first.signature == fixture["thinking_blocks"][0]["signature"]

    def test_redacted_block_preserves_encrypted_data(self) -> None:
        fixture = _load_fixture("anthropic_thinking_response.json")
        reasoning = self._extracted()
        redacted = next(b for b in reasoning.blocks if b.type == "redacted_thinking")
        assert redacted.text == ""
        assert redacted.encrypted_data == fixture["thinking_blocks"][1]["data"]

    def test_summary_preserved_when_distinct_from_blocks(self) -> None:
        """``reasoning_content`` is a *shorter abstract* (122 chars) than
        the verbatim ``thinking_blocks[0].thinking`` (219 chars) — the
        codec must keep ``summary`` because it carries unique signal
        the joined block text does not. Pin the legitimate-summary case."""
        fixture = _load_fixture("anthropic_thinking_response.json")
        reasoning = self._extracted()
        assert reasoning.summary == fixture["reasoning_content"]
        joined = "\n\n".join(b.text for b in reasoning.blocks if b.text)
        assert joined != reasoning.summary, (
            "Test fixture must carry a summary distinct from the joined "
            "block text — otherwise this test exercises the redundant "
            "branch instead of the legitimate-summary branch."
        )


class TestAnthropicExtractDisplayOmitted:
    """``anthropic_display_omitted_response.json`` — Claude 4.7 default.

    Even with empty ``.thinking`` text, signatures must survive so Stage 4's
    interleaved-thinking replay can round-trip them on the next turn.
    """

    def _extracted(self) -> StructuredReasoning:
        message = _message_from_fixture("anthropic_display_omitted_response.json")
        reasoning = AnthropicReasoningCodec().extract(message)
        assert reasoning is not None
        return reasoning

    def test_blocks_have_empty_text(self) -> None:
        reasoning = self._extracted()
        assert all(block.text == "" for block in reasoning.blocks)

    def test_blocks_have_populated_signatures(self) -> None:
        fixture = _load_fixture("anthropic_display_omitted_response.json")
        reasoning = self._extracted()
        expected_sigs = [b["signature"] for b in fixture["thinking_blocks"]]
        actual_sigs = [b.signature for b in reasoning.blocks]
        assert actual_sigs == expected_sigs
        assert all(sig for sig in actual_sigs)

    def test_is_not_empty_because_signatures_carry_state(self) -> None:
        reasoning = self._extracted()
        # ``StructuredReasoning.is_empty()`` checks text + summary. Our
        # definition: blocks with only signatures are *structurally* not
        # content, but the codec still returns them — Stage 4 needs the
        # bytes. The conservative assertion here: the codec returned a
        # populated structure (None would lose the signatures entirely).
        assert len(reasoning.blocks) == 2

    def test_as_plain_text_is_empty(self) -> None:
        reasoning = self._extracted()
        # No visible text → human-readable projection is empty. Signature
        # bytes are retained but not rendered.
        assert reasoning.as_plain_text() == ""


# ---------------------------------------------------------------------------
# Validation — malformed blocks must raise, not be silently dropped.
# ---------------------------------------------------------------------------


class TestAnthropicExtractFromOpenRouterReasoningDetails:
    """OpenRouter-routed Claude responses surface structured reasoning under
    ``provider_specific_fields.reasoning_details`` (with signatures).
    ``thinking_blocks`` is None on this transport — litellm only populates it
    for direct-Anthropic routing.

    Verified against a live probe of ``openrouter/anthropic/claude-opus-4.7``
    on 2026-04-27. The codec must read this shape so multi-turn thinking
    replay survives OpenRouter routing — the highest-impact bug from the
    ``tau_manufacturing_v2`` post-fix diagnosis (0 / 2119 reasoning blocks
    captured).
    """

    def _extracted(self) -> StructuredReasoning:
        message = _message_from_fixture("openrouter_anthropic_reasoning_response.json")
        reasoning = AnthropicReasoningCodec().extract(message)
        assert reasoning is not None
        return reasoning

    def test_returns_structured_reasoning(self) -> None:
        reasoning = self._extracted()
        assert isinstance(reasoning, StructuredReasoning)

    def test_extracts_block_text_from_reasoning_details(self) -> None:
        fixture = _load_fixture("openrouter_anthropic_reasoning_response.json")
        reasoning = self._extracted()
        assert len(reasoning.blocks) == 1
        block = reasoning.blocks[0]
        expected_text = fixture["provider_specific_fields"]["reasoning_details"][0]["text"]
        assert block.text == expected_text
        assert block.type == "thinking"

    def test_preserves_signature_for_replay(self) -> None:
        fixture = _load_fixture("openrouter_anthropic_reasoning_response.json")
        reasoning = self._extracted()
        block = reasoning.blocks[0]
        expected_sig = fixture["provider_specific_fields"]["reasoning_details"][0]["signature"]
        assert block.signature == expected_sig, (
            "Signature byte preservation is the entire point — without it, "
            "interleaved-thinking replay across turns is impossible."
        )

    def test_summary_dropped_when_redundant_with_block_text(self) -> None:
        """OpenRouter populates both ``reasoning_content`` and
        ``provider_specific_fields.reasoning_details`` from the same
        Anthropic thinking emission, so they're byte-identical. The codec
        must collapse the duplicate by setting ``summary`` to ``None`` —
        the data is still recoverable via ``as_plain_text()`` from the
        blocks (which carry the replay-required signature)."""
        fixture = _load_fixture("openrouter_anthropic_reasoning_response.json")
        reasoning = self._extracted()
        # Pre-condition: fixture exhibits the redundant case.
        joined = "\n\n".join(b.text for b in reasoning.blocks if b.text)
        assert (
            joined == fixture["reasoning_content"]
        ), "Fixture must carry summary == joined block text to exercise the redundant branch."
        # Contract: redundant summary is dropped.
        assert reasoning.summary is None
        # Round-trip: joined block text recovers the original content.
        assert reasoning.as_plain_text() == fixture["reasoning_content"]

    def test_reasoning_details_wins_when_thinking_blocks_is_none(self) -> None:
        """``thinking_blocks=None`` (the OpenRouter case) MUST NOT cause the
        codec to return None — it must fall through to ``reasoning_details``."""
        message = SimpleNamespace(
            reasoning_content=None,
            thinking_blocks=None,
            provider_specific_fields={
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": "step A",
                        "format": "anthropic-claude-v1",
                        "signature": "sig-A",
                    },
                ],
            },
        )
        reasoning = AnthropicReasoningCodec().extract(message)
        assert reasoning is not None
        assert reasoning.blocks[0].text == "step A"
        assert reasoning.blocks[0].signature == "sig-A"

    def test_native_thinking_blocks_take_precedence_over_reasoning_details(self) -> None:
        """When both exist (theoretical: direct-Anthropic + OpenRouter mix),
        ``thinking_blocks`` is the canonical surface and wins. Locks in the
        priority for the rare both-populated case."""
        message = SimpleNamespace(
            reasoning_content="summary",
            thinking_blocks=[
                {"type": "thinking", "thinking": "native", "signature": "native-sig"},
            ],
            provider_specific_fields={
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": "openrouter",
                        "format": "anthropic-claude-v1",
                        "signature": "or-sig",
                    },
                ],
            },
        )
        reasoning = AnthropicReasoningCodec().extract(message)
        assert reasoning is not None
        assert [b.text for b in reasoning.blocks] == ["native"]
        assert [b.signature for b in reasoning.blocks] == ["native-sig"]


class TestAnthropicExtractValidation:
    def test_unknown_block_type_raises(self) -> None:
        message = SimpleNamespace(
            reasoning_content=None,
            thinking_blocks=[{"type": "other_type", "thinking": "x"}],
        )
        with pytest.raises(ValueError, match="Unknown Anthropic block type"):
            AnthropicReasoningCodec().extract(message)

    def test_non_dict_block_raises(self) -> None:
        message = SimpleNamespace(reasoning_content=None, thinking_blocks=["not a dict"])
        with pytest.raises(ValueError, match="must be dict"):
            AnthropicReasoningCodec().extract(message)

    def test_returns_none_when_no_reasoning_fields(self) -> None:
        message = SimpleNamespace(reasoning_content=None, thinking_blocks=None)
        assert AnthropicReasoningCodec().extract(message) is None

    def test_returns_none_when_all_fields_empty(self) -> None:
        message = SimpleNamespace(reasoning_content="", thinking_blocks=[])
        assert AnthropicReasoningCodec().extract(message) is None


# ---------------------------------------------------------------------------
# Replay encoding — shape must match the canonical litellm first-class-field
# contract (plan § Canonical litellm surface).
# ---------------------------------------------------------------------------


class TestAnthropicEncodeForReplay:
    def test_encodes_thinking_block_with_signature(self) -> None:
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text="step A", signature="sig-1"),),
        )
        encoded = AnthropicReasoningCodec().encode_for_replay(reasoning)
        assert encoded == {
            "thinking_blocks": [
                {"type": "thinking", "thinking": "step A", "signature": "sig-1"},
            ],
        }

    def test_encodes_redacted_thinking_without_thinking_field(self) -> None:
        """Redacted blocks carry ``data`` only — no ``thinking`` / ``signature``
        keys in the litellm replay shape."""
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="redacted_thinking", text="", encrypted_data="payload"),),
        )
        encoded = AnthropicReasoningCodec().encode_for_replay(reasoning)
        assert encoded == {
            "thinking_blocks": [{"type": "redacted_thinking", "data": "payload"}],
        }

    def test_returns_empty_when_no_replayable_blocks(self) -> None:
        assert AnthropicReasoningCodec().encode_for_replay(StructuredReasoning()) == {}

    def test_rejects_summary_text_blocks(self) -> None:
        """``summary_text`` is OpenAI-shape — Anthropic can't replay it."""
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="summary_text", text="summary"),),
        )
        with pytest.raises(ValueError, match="Cannot encode block type 'summary_text'"):
            AnthropicReasoningCodec().encode_for_replay(reasoning)

    def test_openrouter_transport_emits_reasoning_details(self) -> None:
        """OpenRouter-sourced reasoning replays as ``reasoning_details`` —
        the per-message field OpenRouter accepts for signed-reasoning
        round-trips. Direct Anthropic's ``thinking_blocks`` shape is
        incompatible with OpenRouter's OpenAI-compatible message format."""
        reasoning = StructuredReasoning(
            blocks=(
                ReasoningBlock(type="thinking", text="step A", signature="sig-A"),
                ReasoningBlock(type="thinking", text="step B", signature="sig-B"),
            ),
            transport="openrouter",
        )
        encoded = AnthropicReasoningCodec().encode_for_replay(reasoning)
        assert "thinking_blocks" not in encoded
        assert encoded == {
            "reasoning_details": [
                {
                    "type": "reasoning.text",
                    "text": "step A",
                    "format": "anthropic-claude-v1",
                    "signature": "sig-A",
                },
                {
                    "type": "reasoning.text",
                    "text": "step B",
                    "format": "anthropic-claude-v1",
                    "signature": "sig-B",
                },
            ],
        }

    def test_native_transport_still_emits_thinking_blocks(self) -> None:
        """Direct-Anthropic transport keeps the canonical litellm ``thinking_blocks``
        replay shape — the bytes echoed back to the live Anthropic API."""
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text="step", signature="sig"),),
            transport="anthropic_native",
        )
        encoded = AnthropicReasoningCodec().encode_for_replay(reasoning)
        assert encoded == {
            "thinking_blocks": [
                {"type": "thinking", "thinking": "step", "signature": "sig"},
            ],
        }


# ---------------------------------------------------------------------------
# Round-trip — extract → encode → the thinking_blocks list restored verbatim
# (ignoring irrelevant keys). This is the guarantee Stage 4 depends on.
# ---------------------------------------------------------------------------


class TestAnthropicRoundTrip:
    @pytest.mark.parametrize(
        "fixture_name",
        ["anthropic_thinking_response.json", "anthropic_display_omitted_response.json"],
    )
    def test_extract_then_encode_restores_thinking_blocks(self, fixture_name: str) -> None:
        fixture = _load_fixture(fixture_name)
        message = _message_from_fixture(fixture_name)

        codec = AnthropicReasoningCodec()
        reasoning = codec.extract(message)
        assert reasoning is not None

        encoded = codec.encode_for_replay(reasoning)
        original_blocks = fixture["thinking_blocks"]
        round_tripped = encoded["thinking_blocks"]

        # One block in → one block out, preserving block type + payload keys.
        assert len(round_tripped) == len(original_blocks)
        for original, restored in zip(original_blocks, round_tripped, strict=True):
            assert restored["type"] == original["type"]
            if original["type"] == "thinking":
                assert restored["thinking"] == original.get("thinking", "")
                assert restored.get("signature") == original.get("signature")
            elif original["type"] == "redacted_thinking":
                assert restored["data"] == original["data"]
                # Redacted replay shape explicitly omits the ``thinking`` key.
                assert "thinking" not in restored


# ---------------------------------------------------------------------------
# LLMClient wiring — verifies the codec is called via
# ``capabilities.reasoning_codec.extract`` (no inline branching).
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal shape compatible with ``LLMClient.generate``'s response reads."""

    def __init__(self, message: SimpleNamespace) -> None:
        self.choices = [SimpleNamespace(message=message)]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)


class TestLLMClientDelegatesToReasoningCodec:
    def _build_client(self):
        from tolokaforge.core.llm.client import LLMClient

        return LLMClient(
            ModelConfig(
                provider="openrouter",
                name="anthropic/claude-opus-4.7",
            )
        )

    def test_generate_populates_structured_reasoning_from_fixture(self) -> None:
        client = self._build_client()
        message = _message_from_fixture("anthropic_thinking_response.json")
        # Strip tool_calls — minimal path asserts reasoning extraction only.
        message.tool_calls = None
        fake = _MockResponse(message)

        with patch("tolokaforge.core.llm.client.completion", return_value=fake):
            result = client.generate(
                system="system",
                messages=[Message(role=MessageRole.USER, content="hi")],
            )

        fixture = _load_fixture("anthropic_thinking_response.json")
        assert result.reasoning is not None
        # Non-empty human-readable projection on the visible-thinking fixture.
        assert result.reasoning.as_plain_text()
        first = result.reasoning.blocks[0]
        assert first.signature == fixture["thinking_blocks"][0]["signature"]
        assert first.type == "thinking"

    def test_generate_preserves_signatures_even_when_text_is_empty(self) -> None:
        client = self._build_client()
        message = _message_from_fixture("anthropic_display_omitted_response.json")
        message.tool_calls = None
        fake = _MockResponse(message)

        with patch("tolokaforge.core.llm.client.completion", return_value=fake):
            result = client.generate(
                system="system",
                messages=[Message(role=MessageRole.USER, content="hi")],
            )

        fixture = _load_fixture("anthropic_display_omitted_response.json")
        assert result.reasoning is not None
        assert result.reasoning.as_plain_text() == ""
        extracted_sigs = [b.signature for b in result.reasoning.blocks]
        expected_sigs = [b["signature"] for b in fixture["thinking_blocks"]]
        assert extracted_sigs == expected_sigs
