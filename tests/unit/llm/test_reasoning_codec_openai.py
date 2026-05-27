"""Unit tests for :class:`OpenAIReasoningCodec`.

OpenAI-family models surface reasoning as a single ``reasoning_content``
string; no structured thinking blocks, no signatures. The codec therefore:

* Produces a single ``summary_text`` :class:`ReasoningBlock` carrying the
  ``reasoning_content`` text. ``StructuredReasoning.summary`` is set to
  ``None`` because the block already covers the full content — see
  ``_dedup_summary`` in :mod:`reasoning_codec`. ``summary`` is reserved
  for the *distinct, server-shipped abstract* case (a future direct-API
  scenario), not a redundant mirror of the verbatim block text.
* Returns ``{}`` from :meth:`encode_for_replay` — OpenAI has no stateful
  interleaved-thinking contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec

pytestmark = pytest.mark.unit

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((_FIXTURES_DIR / name).read_text())
    payload.pop("_comment", None)
    return payload


def _message_from_fixture(name: str) -> SimpleNamespace:
    return SimpleNamespace(**_load_fixture(name))


class TestOpenAIExtract:
    def test_fixture_produces_single_summary_text_block(self) -> None:
        message = _message_from_fixture("openai_gpt5_reasoning_response.json")
        reasoning = OpenAIReasoningCodec().extract(message)
        assert reasoning is not None

        fixture = _load_fixture("openai_gpt5_reasoning_response.json")
        assert len(reasoning.blocks) == 1
        only = reasoning.blocks[0]
        assert only == ReasoningBlock(
            type="summary_text",
            text=fixture["reasoning_content"],
            signature=None,
        )

    def test_summary_dropped_when_redundant_with_block_text(self) -> None:
        """OpenAI's only data source is ``reasoning_content``; the codec
        must not double-store it under both ``blocks[0].text`` and
        ``summary``. ``summary`` is reserved for the case where the
        provider ships a *distinct* shorter abstract."""
        message = _message_from_fixture("openai_gpt5_reasoning_response.json")
        reasoning = OpenAIReasoningCodec().extract(message)
        assert reasoning is not None
        assert reasoning.summary is None
        # The data is still recoverable via the canonical projection.
        assert (
            reasoning.as_plain_text()
            == _load_fixture("openai_gpt5_reasoning_response.json")["reasoning_content"]
        )

    def test_empty_reasoning_content_returns_none(self) -> None:
        message = SimpleNamespace(reasoning_content="")
        assert OpenAIReasoningCodec().extract(message) is None

    def test_missing_attribute_returns_none(self) -> None:
        message = SimpleNamespace()  # no reasoning_content attr at all
        assert OpenAIReasoningCodec().extract(message) is None

    def test_none_reasoning_content_returns_none(self) -> None:
        message = SimpleNamespace(reasoning_content=None)
        assert OpenAIReasoningCodec().extract(message) is None


class TestOpenAIEncodeForReplay:
    def test_is_noop(self) -> None:
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="summary_text", text="summary"),),
            summary="summary",
        )
        assert OpenAIReasoningCodec().encode_for_replay(reasoning) == {}

    def test_empty_reasoning_returns_empty_dict(self) -> None:
        assert OpenAIReasoningCodec().encode_for_replay(StructuredReasoning()) == {}
