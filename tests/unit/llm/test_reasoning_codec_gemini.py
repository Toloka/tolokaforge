"""Unit tests for :class:`GeminiReasoningCodec`.

Codec contract:

* **Two block types**: ``reasoning.text`` (Gemini 3.1 Pro, readable
  thinking) and ``reasoning.encrypted`` (Gemini 3 Flash, opaque
  payload — no readable text but Google still bills reasoning tokens
  for it). Both surface inside
  ``provider_specific_fields.reasoning_details``.
* **No signatures, no format**: unlike Anthropic-via-OpenRouter, Gemini
  does NOT carry a per-block ``signature`` and does NOT carry a
  ``format`` field. The codec must accept their absence; replay is
  byte-preserving but not signature-bearing.
* **Replay shape**: the same ``reasoning_details`` array round-trips
  back as a top-level message field. We pass it through verbatim.

Fixtures are realistic-but-truncated captures recorded in the JSON
files alongside this module. The codec's contract is exercised
against those exact byte shapes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import GeminiReasoningCodec

pytestmark = pytest.mark.unit

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((_FIXTURES_DIR / name).read_text())
    payload.pop("_comment", None)
    return payload


def _message_from_fixture(name: str) -> SimpleNamespace:
    payload = _load_fixture(name)
    return SimpleNamespace(**payload)


# ---------------------------------------------------------------------------
# Visible thinking — ``reasoning.text`` blocks (Gemini 3.1 Pro)
# ---------------------------------------------------------------------------


class TestGeminiExtractVisibleThinking:
    """``openrouter_gemini_reasoning_text_response.json`` — readable thinking."""

    def _extracted(self) -> StructuredReasoning:
        message = _message_from_fixture("openrouter_gemini_reasoning_text_response.json")
        reasoning = GeminiReasoningCodec().extract(message)
        assert reasoning is not None
        return reasoning

    def test_returns_structured_reasoning(self) -> None:
        assert isinstance(self._extracted(), StructuredReasoning)

    def test_extracts_two_text_blocks(self) -> None:
        assert len(self._extracted().blocks) == 2

    def test_blocks_have_thinking_type(self) -> None:
        for block in self._extracted().blocks:
            assert block.type == "thinking"

    def test_block_text_preserved_verbatim(self) -> None:
        fixture = _load_fixture("openrouter_gemini_reasoning_text_response.json")
        extracted = self._extracted().blocks
        for fixture_entry, extracted_block in zip(
            fixture["provider_specific_fields"]["reasoning_details"],
            extracted,
            strict=True,
        ):
            assert extracted_block.text == fixture_entry["text"]

    def test_no_signatures_carried(self) -> None:
        """Gemini does not emit signatures — ``signature`` must be ``None``."""
        for block in self._extracted().blocks:
            assert block.signature is None

    def test_transport_is_openrouter(self) -> None:
        """Replay shape selector — must mark the openrouter transport."""
        assert self._extracted().transport == "openrouter"

    def test_summary_is_none_when_only_blocks(self) -> None:
        """Without a fixture-side ``reasoning_content`` summary distinct from
        the blocks, the codec must not synthesise one. Concretely: the
        summary equals the joined block text (de-duplicated to ``None``)
        when ``reasoning_content`` carries the same content."""
        # Summary is set from reasoning_content when present and DIFFERENT
        # from the joined block text. In our fixture reasoning_content is
        # a strict prefix of joined blocks, so the dedup keeps it.
        summary = self._extracted().summary
        assert summary is not None  # carried from reasoning_content


# ---------------------------------------------------------------------------
# Encrypted thinking — ``reasoning.encrypted`` blocks (Gemini 3 Flash)
# ---------------------------------------------------------------------------


class TestGeminiExtractEncryptedThinking:
    """``openrouter_gemini_reasoning_encrypted_response.json`` — opaque blob."""

    def _extracted(self) -> StructuredReasoning:
        message = _message_from_fixture("openrouter_gemini_reasoning_encrypted_response.json")
        reasoning = GeminiReasoningCodec().extract(message)
        assert reasoning is not None
        return reasoning

    def test_extracts_one_encrypted_block(self) -> None:
        assert len(self._extracted().blocks) == 1

    def test_block_type_is_redacted_thinking(self) -> None:
        """``reasoning.encrypted`` semantically maps to ``redacted_thinking``."""
        assert self._extracted().blocks[0].type == "redacted_thinking"

    def test_block_text_is_empty(self) -> None:
        """Encrypted blocks carry no readable text."""
        assert self._extracted().blocks[0].text == ""

    def test_encrypted_payload_preserved_verbatim(self) -> None:
        """Byte-for-byte preservation — the replay must carry the same bytes back."""
        fixture = _load_fixture("openrouter_gemini_reasoning_encrypted_response.json")
        expected = fixture["provider_specific_fields"]["reasoning_details"][0]["data"]
        assert self._extracted().blocks[0].encrypted_data == expected

    def test_summary_is_none(self) -> None:
        """Flash has no ``reasoning_content`` summary either."""
        assert self._extracted().summary is None


# ---------------------------------------------------------------------------
# Empty-message fast path
# ---------------------------------------------------------------------------


def test_extract_returns_none_on_empty_message() -> None:
    empty = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields=None,
    )
    assert GeminiReasoningCodec().extract(empty) is None


def test_extract_returns_none_on_message_without_reasoning_details() -> None:
    """A response carrying ``provider_specific_fields`` but no
    ``reasoning_details`` (e.g. a plain refusal) must extract to None."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={"refusal": None},
    )
    assert GeminiReasoningCodec().extract(message) is None


# ---------------------------------------------------------------------------
# Encode-for-replay round trip
# ---------------------------------------------------------------------------


class TestGeminiEncodeForReplay:
    """Replay must emit the original ``reasoning_details`` shape verbatim."""

    def test_replay_text_blocks_round_trip(self) -> None:
        extracted = GeminiReasoningCodec().extract(
            _message_from_fixture("openrouter_gemini_reasoning_text_response.json")
        )
        assert extracted is not None
        replay = GeminiReasoningCodec().encode_for_replay(extracted)
        assert "reasoning_details" in replay
        details = replay["reasoning_details"]
        assert len(details) == 2
        for entry in details:
            assert entry["type"] == "reasoning.text"
            assert "text" in entry
            # No signature inserted on replay where the source had none.
            assert "signature" not in entry

    def test_replay_encrypted_block_carries_data(self) -> None:
        extracted = GeminiReasoningCodec().extract(
            _message_from_fixture("openrouter_gemini_reasoning_encrypted_response.json")
        )
        assert extracted is not None
        replay = GeminiReasoningCodec().encode_for_replay(extracted)
        assert "reasoning_details" in replay
        details = replay["reasoning_details"]
        assert len(details) == 1
        entry = details[0]
        assert entry["type"] == "reasoning.encrypted"
        assert entry["data"]  # non-empty
        assert "text" not in entry

    def test_replay_byte_preserves_encrypted_payload(self) -> None:
        fixture = _load_fixture("openrouter_gemini_reasoning_encrypted_response.json")
        original = fixture["provider_specific_fields"]["reasoning_details"][0]["data"]
        extracted = GeminiReasoningCodec().extract(
            _message_from_fixture("openrouter_gemini_reasoning_encrypted_response.json")
        )
        assert extracted is not None
        replay = GeminiReasoningCodec().encode_for_replay(extracted)
        assert replay["reasoning_details"][0]["data"] == original

    def test_replay_empty_when_no_blocks(self) -> None:
        empty = StructuredReasoning(blocks=())
        assert GeminiReasoningCodec().encode_for_replay(empty) == {}


# ---------------------------------------------------------------------------
# Loud-fail on unknown block types
# ---------------------------------------------------------------------------


def test_extract_raises_on_unknown_block_type() -> None:
    """Per AGENTS.md "surface failures explicitly" — unknown block types
    must raise rather than silently dropping the block."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [
                {"type": "reasoning.unobtanium", "text": "future google extension"}
            ]
        },
    )
    with pytest.raises(ValueError, match="Unknown Gemini reasoning_details type"):
        GeminiReasoningCodec().extract(message)


def test_replay_raises_on_unsupported_block_type() -> None:
    """``summary_text`` blocks (OpenAI-only) cannot be re-emitted as Gemini
    reasoning_details — must raise."""
    reasoning = StructuredReasoning(
        blocks=(ReasoningBlock(type="summary_text", text="bogus"),),
    )
    with pytest.raises(ValueError, match="Cannot encode block type"):
        GeminiReasoningCodec().encode_for_replay(reasoning)


# ---------------------------------------------------------------------------
# Placeholder-UUID drop on replay
# ---------------------------------------------------------------------------

# OpenRouter sends a constant 48-char base64 blob carrying the literal
# UUID ``e24830a7-5cd6-42fe-998b-ee539e72b9c3`` as the ``data`` field of
# a ``reasoning.encrypted`` block when Gemini emitted no real thinking
# on a turn (purely a tool-call follow-up turn). Real opaque blobs from
# Gemini are 1000-3000+ base64 chars. Replaying the placeholder is
# pure token-cost waste — the model derives no continuity from a 36-byte
# UUID — and live probes confirm output is unchanged whether we replay
# the placeholder or send empty ``reasoning_details``. The codec drops
# any ``redacted_thinking`` block whose payload is shorter than 100
# chars so we don't echo the placeholder back. Detection is
# length-based (not exact-match) to stay robust against future
# OpenRouter changes to the placeholder string.

_PLACEHOLDER_BASE64 = "ZTI0ODMwYTctNWNkNi00MmZlLTk5OGItZWU1MzllNzJiOWMz"
"""Base64 of ``e24830a7-5cd6-42fe-998b-ee539e72b9c3``. Exactly 48 chars."""

# A realistic-shape opaque payload >100 chars (matches the existing
# encrypted-response fixture order of magnitude).
_REAL_OPAQUE_BLOB = (
    "CikBjz1rX7rClHLBpZPfF3sRryQeWKtnLkCukSpc9+E2Pxx8tG7UbOiz9ApeAY89a1/X"
    "ZShyzVqCxsr+QJobKlZJ3qyJpgwyIbcA0E/dKqmpEGfb1LtWWzh0OZkCcZqU3rE9"
)


def test_encode_for_replay_drops_placeholder_uuid_block() -> None:
    """Replay must omit the constant 48-char placeholder UUID block."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_PLACEHOLDER_BASE64,
            ),
        ),
        transport="openrouter",
    )
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    # Placeholder-only input → empty replay (no ``reasoning_details`` key).
    assert replay == {}, f"Placeholder-only reasoning must not surface on the wire. Got: {replay!r}"


def test_encode_for_replay_keeps_real_opaque_block_alongside_dropped_placeholder() -> None:
    """Mixed replay: drop the placeholder, keep the real opaque blob."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_REAL_OPAQUE_BLOB,
            ),
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_PLACEHOLDER_BASE64,
            ),
        ),
        transport="openrouter",
    )
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    assert "reasoning_details" in replay
    details = replay["reasoning_details"]
    assert len(details) == 1, f"Expected 1 entry (placeholder dropped, real kept). Got: {details!r}"
    assert details[0]["type"] == "reasoning.encrypted"
    assert details[0]["data"] == _REAL_OPAQUE_BLOB


def test_encode_for_replay_keeps_thinking_text_blocks_alongside_dropped_placeholder() -> None:
    """Real ``reasoning.text`` blocks survive even when the only encrypted
    block is the placeholder."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="thinking",
                text="**Planning the lookup**\n\nI need to retrieve the booking first.",
            ),
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_PLACEHOLDER_BASE64,
            ),
        ),
        transport="openrouter",
    )
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    details = replay["reasoning_details"]
    assert len(details) == 1
    assert details[0]["type"] == "reasoning.text"
    assert details[0]["text"].startswith("**Planning the lookup**")


def test_encode_for_replay_threshold_is_strictly_under_100_chars() -> None:
    """The drop rule is ``len(encrypted_data) < 100``. A 99-char block is
    dropped (defensive — just over placeholder size, still unrealistic for
    real opaque data); a 100-char block is preserved (any real opaque blob
    would comfortably exceed this)."""
    short_blob = "x" * 99
    long_blob = "y" * 100
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data=short_blob),
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data=long_blob),
        ),
        transport="openrouter",
    )
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    details = replay["reasoning_details"]
    assert len(details) == 1
    assert details[0]["data"] == long_blob


def test_extract_still_records_placeholder_so_trajectory_yaml_is_truthful() -> None:
    """``extract`` is unchanged: we still ingest the placeholder so
    ``trajectory.yaml`` records what the wire actually returned. Only
    ``encode_for_replay`` drops it. This keeps the on-disk artifact
    honest about what came back from the API while preventing the
    placeholder from polluting subsequent requests."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.encrypted", "data": _PLACEHOLDER_BASE64}]
        },
    )
    extracted = GeminiReasoningCodec().extract(message)
    assert extracted is not None
    assert len(extracted.blocks) == 1
    assert extracted.blocks[0].type == "redacted_thinking"
    assert extracted.blocks[0].encrypted_data == _PLACEHOLDER_BASE64


# ---------------------------------------------------------------------------
# ``drop_placeholder_signature`` flag — the 2026-05-21 OTS regression A/B
#
# The PR #101 default drops the 48-char placeholder on replay, on the
# theory that it is OpenRouter's "no real thinking" sentinel. The
# 2026-05-21 OTS investigation surfaced an alternative reading: the
# placeholder may be the dummy thought_signature OpenRouter inserts to
# satisfy Google's per-functionCall signature requirement (every
# functionCall part needs one; missing prior-turn signatures "may lead
# to degraded model performance" per Google's docs). The flag lets us
# A/B without churning the API surface. Tests below pin both modes.
# ---------------------------------------------------------------------------


def test_encode_for_replay_drop_disabled_keeps_placeholder() -> None:
    """When constructed with ``drop_placeholder_signature=False`` the codec
    must round-trip the placeholder block on replay so OpenRouter sees
    the same reasoning_details we received. Pins the flag-off contract."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_PLACEHOLDER_BASE64,
            ),
        ),
        transport="openrouter",
    )
    codec = GeminiReasoningCodec(drop_placeholder_signature=False)
    replay = codec.encode_for_replay(reasoning)
    assert "reasoning_details" in replay
    details = replay["reasoning_details"]
    assert len(details) == 1
    assert details[0]["type"] == "reasoning.encrypted"
    assert details[0]["data"] == _PLACEHOLDER_BASE64


def test_encode_for_replay_drop_disabled_keeps_placeholder_alongside_real_blob() -> None:
    """With drop disabled, BOTH the real opaque blob and the placeholder
    survive — order preserved, payloads byte-equal."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data=_REAL_OPAQUE_BLOB),
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data=_PLACEHOLDER_BASE64),
        ),
        transport="openrouter",
    )
    codec = GeminiReasoningCodec(drop_placeholder_signature=False)
    replay = codec.encode_for_replay(reasoning)
    details = replay["reasoning_details"]
    assert [d["data"] for d in details] == [_REAL_OPAQUE_BLOB, _PLACEHOLDER_BASE64]


def test_default_construction_still_drops_placeholder() -> None:
    """Backstop: the default constructor (no kwarg) preserves the PR #101
    drop behavior. Future changes to the default must opt-in explicitly,
    not regress this contract by accident."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(type="redacted_thinking", text="", encrypted_data=_PLACEHOLDER_BASE64),
        ),
        transport="openrouter",
    )
    assert GeminiReasoningCodec().encode_for_replay(reasoning) == {}


def test_drop_disabled_does_not_change_extract_behavior() -> None:
    """The flag governs replay only; ``extract`` records what the wire
    returned regardless. Trajectory faithfulness is independent of replay
    policy."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.encrypted", "data": _PLACEHOLDER_BASE64}]
        },
    )
    extracted = GeminiReasoningCodec(drop_placeholder_signature=False).extract(message)
    assert extracted is not None
    assert extracted.blocks[0].encrypted_data == _PLACEHOLDER_BASE64


# ---------------------------------------------------------------------------
# ``extras`` round-trip — the 2026-05-21 OTS investigation discovered that
# OpenRouter attaches an ``id`` field to ``reasoning.encrypted`` blocks that
# **literally matches the corresponding tool_call.id**. The pre-fix codec
# dropped ``id`` / ``format`` / ``index``, breaking the thought_signature ↔
# tool_call binding on multi-turn replay. Empirically, this halved Pro's
# turn-2 reasoning tokens (148 → 76). Tests below pin the byte-preserving
# contract.
# ---------------------------------------------------------------------------


def test_extract_captures_id_format_index_on_encrypted_block() -> None:
    """``reasoning.encrypted`` block's provider-specific extras must
    survive extract → ReasoningBlock.extras as sorted ``(k, v)`` tuples."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [
                {
                    "type": "reasoning.encrypted",
                    "data": "CikBjz1rXxxxxxx" * 50,  # large enough to dodge placeholder guard
                    "id": "tool_get_weather_yyPxUPE2pPLgukg6ouy1",
                    "format": "google-gemini-v1",
                    "index": 1,
                }
            ]
        },
    )
    extracted = GeminiReasoningCodec().extract(message)
    assert extracted is not None
    assert len(extracted.blocks) == 1
    block = extracted.blocks[0]
    assert block.type == "redacted_thinking"
    # extras is sorted alphabetically by key.
    assert block.extras == (
        ("format", "google-gemini-v1"),
        ("id", "tool_get_weather_yyPxUPE2pPLgukg6ouy1"),
        ("index", 1),
    )


def test_extract_captures_extras_on_text_block_too() -> None:
    """Text blocks also carry ``format`` / ``index`` from OpenRouter —
    preserve them symmetrically."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [
                {
                    "type": "reasoning.text",
                    "text": "**Planning**",
                    "format": "google-gemini-v1",
                    "index": 0,
                }
            ]
        },
    )
    extracted = GeminiReasoningCodec().extract(message)
    assert extracted is not None
    block = extracted.blocks[0]
    assert block.type == "thinking"
    assert block.text == "**Planning**"
    assert block.extras == (("format", "google-gemini-v1"), ("index", 0))


def test_extract_empty_extras_when_no_extra_keys() -> None:
    """Backward compatibility: if OpenRouter only sends the known keys
    (``type`` / ``data``), extras is an empty tuple, not missing or None."""
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "Cik" * 50}]
        },
    )
    extracted = GeminiReasoningCodec().extract(message)
    assert extracted is not None
    assert extracted.blocks[0].extras == ()


def test_encode_for_replay_emits_extras_on_encrypted_block() -> None:
    """The ``id`` linking encrypted reasoning to a specific ``tool_call``
    must round-trip back on replay so OpenRouter can reconstruct the
    thought_signature binding on the next turn."""
    block = ReasoningBlock(
        type="redacted_thinking",
        text="",
        encrypted_data="CikBjz1rXxxxxxx" * 50,
        extras=(
            ("format", "google-gemini-v1"),
            ("id", "tool_get_weather_yyPxUPE2pPLgukg6ouy1"),
            ("index", 1),
        ),
    )
    reasoning = StructuredReasoning(blocks=(block,), transport="openrouter")
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    details = replay["reasoning_details"]
    assert len(details) == 1
    entry = details[0]
    assert entry["type"] == "reasoning.encrypted"
    assert entry["data"] == block.encrypted_data
    assert entry["id"] == "tool_get_weather_yyPxUPE2pPLgukg6ouy1"
    assert entry["format"] == "google-gemini-v1"
    assert entry["index"] == 1


def test_encode_for_replay_emits_extras_on_text_block_too() -> None:
    """Text blocks must also round-trip their extras symmetrically."""
    block = ReasoningBlock(
        type="thinking",
        text="**Planning**",
        extras=(("format", "google-gemini-v1"), ("index", 0)),
    )
    reasoning = StructuredReasoning(blocks=(block,), transport="openrouter")
    replay = GeminiReasoningCodec().encode_for_replay(reasoning)
    entry = replay["reasoning_details"][0]
    assert entry["type"] == "reasoning.text"
    assert entry["text"] == "**Planning**"
    assert entry["format"] == "google-gemini-v1"
    assert entry["index"] == 0


def test_round_trip_byte_preserves_extras() -> None:
    """End-to-end: extract a wire-shape envelope, replay it, and assert the
    output dict is byte-equal to the input dict. This is the contract the
    expert review demanded — preserve raw provider envelope verbatim
    rather than rebuilding from modeled fields only."""
    original = {
        "type": "reasoning.encrypted",
        "data": "Cik" * 50,
        "id": "tool_x_123",
        "format": "google-gemini-v1",
        "index": 7,
    }
    message = SimpleNamespace(
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields={"reasoning_details": [original]},
    )
    extracted = GeminiReasoningCodec().extract(message)
    assert extracted is not None
    replay = GeminiReasoningCodec().encode_for_replay(extracted)
    assert replay["reasoning_details"][0] == original
