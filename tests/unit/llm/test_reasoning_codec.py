"""Unit tests for :class:`NoReasoningCodec` (default no-op).

Per-provider codec tests live in:

* [`test_reasoning_codec_anthropic.py`](test_reasoning_codec_anthropic.py:1)
* [`test_reasoning_codec_openai.py`](test_reasoning_codec_openai.py:1)
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import (
    NoReasoningCodec,
    ReasoningCodec,
)

pytestmark = pytest.mark.unit


def _sample_reasoning() -> StructuredReasoning:
    return StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text="step A", signature="sig-1"),),
    )


class TestNoReasoningCodec:
    def test_implements_protocol(self) -> None:
        codec: ReasoningCodec = NoReasoningCodec()
        assert isinstance(codec, ReasoningCodec)

    def test_extract_returns_none(self) -> None:
        codec = NoReasoningCodec()
        assert codec.extract(object()) is None

    def test_encode_for_replay_returns_empty_dict(self) -> None:
        codec = NoReasoningCodec()
        assert codec.encode_for_replay(_sample_reasoning()) == {}
