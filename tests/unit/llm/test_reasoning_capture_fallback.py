"""Capture must not depend on the preset having picked the right codec.

A preset chooses a reasoning codec by glob, before the model has said
anything, and presets are routinely written for an unrelated reason — a
sampling rule, a temperature quirk — leaving reasoning to whatever the default
was. When that guess is wrong the provider's deliberation used to land nowhere.
It is now recovered, and the mis-routing is reported instead of being paid for
twice.

Uses the ``MagicMock`` / ``patch`` idiom of
:mod:`tests.unit.llm.test_generation_result_finish_reason`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm import client as client_module
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


def _response(**reasoning_fields: object) -> MagicMock:
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = "done"
    message.tool_calls = None
    message.thinking_blocks = None
    message.reasoning_content = reasoning_fields.get("reasoning_content")
    message.provider_specific_fields = reasoning_fields.get("provider_specific_fields")
    choice.message = message
    choice.finish_reason = "stop"
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


def _generate(monkeypatch: pytest.MonkeyPatch, model: str, response: MagicMock):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-reasoning-capture")
    monkeypatch.setattr(client_module, "_RECOVERED_WARNED", set())
    client = LLMClient(ModelConfig(provider="openrouter", name=model))
    client._retry_sleep = lambda _s: None
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
            return client, client.generate(
                system="s", messages=[Message(role=MessageRole.USER, content="hi")]
            )


#: Resolves to ``NoReasoningCodec``: this lineage has no reasoning surface, so
#: the preset reads nothing. A sibling model landing on the same glob and
#: surfacing reasoning is exactly the case this fallback exists for.
_KEEPS_NOTHING = "google/gemma-3-27b-it"

#: Resolves to ``OpenAIReasoningCodec``, which reads ``reasoning_content``.
_READS_REASONING_CONTENT = "openai/o4-mini"


def test_reasoning_is_kept_when_the_presets_codec_reads_none_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _generate(
        monkeypatch,
        _KEEPS_NOTHING,
        _response(reasoning_content="I checked the log first."),
    )
    assert result.reasoning is not None
    assert [b.text for b in result.reasoning.blocks] == ["I checked the log first."]


def test_structured_details_are_kept_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _generate(
        monkeypatch,
        _KEEPS_NOTHING,
        _response(
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.text", "text": "checked the log"}]
            }
        ),
    )
    assert result.reasoning is not None
    assert [b.text for b in result.reasoning.blocks] == ["checked the log"]


def test_an_opaque_blob_is_still_kept_as_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no text in an encrypted payload, so there is nothing to recover
    and nothing to warn about. Treating it as a loss would fire on most calls
    of the routes that are behaving correctly."""
    _, result = _generate(
        monkeypatch,
        _KEEPS_NOTHING,
        _response(
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "xxxx"}]
            }
        ),
    )
    assert result.reasoning is None


def test_the_presets_codec_still_wins_when_it_extracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is reached only on an empty extraction, so a codec that
    deliberately reads one channel keeps deciding what the trajectory carries."""
    _, result = _generate(
        monkeypatch,
        _READS_REASONING_CONTENT,
        _response(
            reasoning_content="what the codec reads",
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.text", "text": "what it ignores"}]
            },
        ),
    )
    assert result.reasoning is not None
    assert [b.text for b in result.reasoning.blocks] == ["what the codec reads"]


def test_recovering_warns_once_per_model_and_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning is the actionable half: nothing was lost, but the preset is
    routed too narrowly and the next model landing on it will be too. Once per
    model and upstream, because it is a configuration fact, not an event."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-reasoning-capture")
    monkeypatch.setattr(client_module, "_RECOVERED_WARNED", set())
    client = LLMClient(ModelConfig(provider="openrouter", name=_KEEPS_NOTHING))
    client._retry_sleep = lambda _s: None
    client.logger = MagicMock()

    response = _response(reasoning_content="I checked the log first.")
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
            for _ in range(3):
                client.generate(system="s", messages=[Message(role=MessageRole.USER, content="hi")])

    assert client.logger.warning.call_count == 1
    kwargs = client.logger.warning.call_args.kwargs
    assert kwargs["model"] == client.model_name
    assert kwargs["preset"] == "gemma"
    assert kwargs["codec"] == "NoReasoningCodec"
    assert kwargs["arrived_in"] == ["reasoning_content"]


class TestWhatTheRunGetsToldAboutIt:
    """Two signals, because the old single one could not tell a mis-routed
    preset from a channel nobody has ever seen -- and fired on neither as
    loudly as it fired on the encrypted-only calls that were fine."""

    def test_recovery_is_reported_as_a_config_fact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, result = _generate(
            monkeypatch, _KEEPS_NOTHING, _response(reasoning_content="checked the log")
        )
        assert result.reasoning_recovered_by_fallback is True
        assert result.reasoning_channel_unknown is False

    def test_an_opaque_payload_is_neither(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What the old counter got wrong: billed, nothing kept, nothing wrong."""
        response = _response(
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "xxxx"}]
            }
        )
        response.usage.completion_tokens_details = MagicMock(reasoning_tokens=512)
        _, result = _generate(monkeypatch, _KEEPS_NOTHING, response)

        assert result.usage.reasoning_tokens == 512
        assert result.reasoning_recovered_by_fallback is False
        assert result.reasoning_channel_unknown is False

    def test_a_channel_we_do_not_know_is_the_remaining_loss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Billed for deliberation that arrived nowhere we look. It cannot be
        captured -- an enumeration reads an unknown channel as silence -- but
        the bill says it happened, which is what notices it."""
        response = _response(provider_specific_fields={"reasoning_somewhere_new": "thought"})
        response.usage.completion_tokens_details = MagicMock(reasoning_tokens=512)
        _, result = _generate(monkeypatch, _KEEPS_NOTHING, response)

        assert result.reasoning is None
        assert result.reasoning_channel_unknown is True


def test_a_codec_result_is_never_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even one carrying no text.

    Gemini builds exactly that from an encrypted-only envelope: blocks with
    ``text=""`` whose ``encrypted_data`` and ``id`` are what the next turn
    needs — stripping the ``id`` halved turn-2 reasoning tokens in a measured
    A/B. A route that fills the readable mirror *and* the encrypted envelope
    would, on an earlier form of this fallback, have had that payload swapped
    for text that cannot be replayed at all.
    """
    response = _response(
        provider_specific_fields={
            "reasoning": "readable mirror",
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "rsn_abc", "id": "call_7", "index": 0}
            ],
        }
    )
    _, result = _generate(monkeypatch, "google/gemini-3.1-pro-preview", response)

    assert result.reasoning is not None
    assert result.reasoning.capture_only is False
    block = result.reasoning.blocks[0]
    assert block.encrypted_data == "rsn_abc"
    assert dict(block.extras)["id"] == "call_7"
    assert result.reasoning_recovered_by_fallback is False
