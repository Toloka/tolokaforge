"""``GenerationResult.finish_reason`` ingestion contract.

Pins that :meth:`LLMClient._assemble_result` sources
:attr:`GenerationResult.finish_reason` from the litellm-post-mapped
``choice.finish_reason`` verbatim — the OpenAI-compatible signal that
every provider litellm supports today post-maps its max-tokens truncation
to (``"length"``). This is the wire-shape observation the engine loop's
output-length retry seam reads: a false-negative (dropping ``"length"``
during parse) would silently disable the seam for opted-in presets.

Uses the ``MagicMock`` / ``patch`` idiom already established in
:mod:`tests.unit.llm.test_synthetic_envelope_detection`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


def _response(finish_reason: str, content: str = "hello") -> MagicMock:
    """Build a minimal litellm-shape response carrying ``finish_reason``."""
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = content
    message.tool_calls = None
    message.thinking_blocks = None
    message.reasoning_content = None
    message.provider_specific_fields = None
    choice.message = message
    choice.finish_reason = finish_reason
    choice.provider_specific_fields = None
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _make_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-finish-reason")
    client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o"))
    client._retry_sleep = lambda _s: None
    return client


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_finish_reason_roundtrips_from_choice(
    monkeypatch: pytest.MonkeyPatch, finish_reason: str
) -> None:
    """``choice.finish_reason`` reaches ``GenerationResult.finish_reason``
    verbatim for both a clean stop and a max-tokens truncation. If parse
    drops the field, the engine's output-length seam goes silent."""
    client = _make_client(monkeypatch)
    response = _response(finish_reason)
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
            result = client.generate(
                system="s",
                messages=[Message(role=MessageRole.USER, content="hi")],
            )
    assert result.finish_reason == finish_reason
