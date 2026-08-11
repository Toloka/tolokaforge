"""Unit tests for :class:`OpenAISummaryReplayReasoningCodec`.

The DeepSeek V4 route over OpenRouter reports reasoning as a flat
``reasoning_content`` summary with **no per-block signature**. The codec
therefore:

* Inherits :class:`OpenAIReasoningCodec`'s ``extract`` verbatim — one
  ``summary_text`` block, no behavioural delta on the read path.
* Overrides ``encode_for_replay`` to rebuild the unsigned OpenRouter
  ``reasoning_details`` envelope (``type="reasoning.text"``), so the
  payload-level ``UNSIGNED_THINKING_REPLAY`` contract is satisfied where
  the parent's no-op emitted nothing.

Two invariants are load-bearing and easy to regress, so they are pinned
here: no synthesised ``signature`` (which would wrongly promote the signed
``THINKING_REPLAY_ROUNDTRIP`` contract) and no invented ``format`` value.
See the codec docstring for why the DeepSeek route ignores the replayed
envelope in practice.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec

pytestmark = pytest.mark.unit

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FIXTURE = "openrouter_deepseek_v4_reasoning_response.json"


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((_FIXTURES_DIR / name).read_text())
    payload.pop("_comment", None)
    return payload


def _message_from_fixture(name: str) -> SimpleNamespace:
    return SimpleNamespace(**_load_fixture(name))


def _reasoning(*texts: str) -> StructuredReasoning:
    return StructuredReasoning(
        blocks=tuple(ReasoningBlock(type="summary_text", text=t) for t in texts)
    )


class TestExtractMatchesParent:
    """The read path must stay byte-identical to the plain OpenAI codec."""

    def test_real_deepseek_fixture_extracts_single_summary_block(self) -> None:
        message = _message_from_fixture(_FIXTURE)
        reasoning = OpenAISummaryReplayReasoningCodec().extract(message)
        assert reasoning is not None
        assert len(reasoning.blocks) == 1
        assert reasoning.blocks[0].type == "summary_text"
        assert reasoning.blocks[0].text == _load_fixture(_FIXTURE)["reasoning_content"]

    def test_extract_is_not_overridden(self) -> None:
        message = _message_from_fixture(_FIXTURE)
        assert OpenAISummaryReplayReasoningCodec().extract(
            message
        ) == OpenAIReasoningCodec().extract(message)


class TestEncodeForReplay:
    def test_parent_emits_nothing_subclass_emits_envelope(self) -> None:
        reasoning = _reasoning("Checked divisors up to sqrt(17).")
        assert OpenAIReasoningCodec().encode_for_replay(reasoning) == {}
        assert OpenAISummaryReplayReasoningCodec().encode_for_replay(reasoning) == {
            "reasoning_details": [
                {"type": "reasoning.text", "text": "Checked divisors up to sqrt(17)."}
            ]
        }

    def test_never_synthesises_a_signature(self) -> None:
        """A signature here would wrongly satisfy the SIGNED replay contract."""
        payload = OpenAISummaryReplayReasoningCodec().encode_for_replay(_reasoning("abc"))
        assert all("signature" not in d for d in payload["reasoning_details"])

    def test_omits_the_format_key(self) -> None:
        """The route only ever reports ``format: "unknown"``; don't invent one."""
        payload = OpenAISummaryReplayReasoningCodec().encode_for_replay(_reasoning("abc"))
        assert all("format" not in d for d in payload["reasoning_details"])

    def test_one_entry_per_non_empty_block(self) -> None:
        payload = OpenAISummaryReplayReasoningCodec().encode_for_replay(_reasoning("a", "b"))
        assert [d["text"] for d in payload["reasoning_details"]] == ["a", "b"]

    @pytest.mark.parametrize(
        "reasoning",
        [
            StructuredReasoning(blocks=()),
            _reasoning(""),
            _reasoning("", ""),
        ],
        ids=["no-blocks", "one-empty-block", "all-empty-blocks"],
    )
    def test_returns_empty_dict_when_there_is_no_text(self, reasoning: StructuredReasoning) -> None:
        """``{}`` keeps the assistant dict untouched — never an empty envelope."""
        assert OpenAISummaryReplayReasoningCodec().encode_for_replay(reasoning) == {}

    def test_round_trips_the_real_fixture_text(self) -> None:
        extracted = OpenAISummaryReplayReasoningCodec().extract(_message_from_fixture(_FIXTURE))
        assert extracted is not None
        payload = OpenAISummaryReplayReasoningCodec().encode_for_replay(extracted)
        details = payload["reasoning_details"]
        assert len(details) == 1
        assert details[0]["text"] == _load_fixture(_FIXTURE)["reasoning_content"]


class TestPresetWiring:
    def test_registered_under_its_yaml_policy_name(self) -> None:
        from tolokaforge.core.llm.presets import _REASONING_CODECS

        assert _REASONING_CODECS["openai_summary_replay"] is OpenAISummaryReplayReasoningCodec

    def test_the_0731_route_resolves_to_this_codec(self) -> None:
        """Pins the first-match-wins ordering against the shared ``*deepseek-v4*`` glob."""
        from tolokaforge.core.llm.presets import build_capabilities, resolve_effective_preset

        name = "deepseek/deepseek-v4-flash-0731"
        assert resolve_effective_preset(name, "openrouter") == "deepseek_v4_flash_0731_resolve"
        codec = build_capabilities(name, "openrouter").reasoning_codec
        assert isinstance(codec, OpenAISummaryReplayReasoningCodec)

    def test_siblings_keep_the_shared_preset(self) -> None:
        from tolokaforge.core.llm.presets import build_capabilities, resolve_effective_preset

        for sibling in ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"):
            assert (
                resolve_effective_preset(sibling, "openrouter")
                == "openrouter_dict_stringify_recovery"
            )
            codec = build_capabilities(sibling, "openrouter").reasoning_codec
            assert type(codec) is OpenAIReasoningCodec
