"""Reading reasoning must not depend on the preset having guessed right.

A preset picks a codec by glob before the model has said anything, and the
model is not consistent: five probes of ``openai/o4-mini`` on 2026-10-02
returned readable ``reasoning_content`` once and an opaque blob four times.
The fallback exists so the one readable sample is kept anyway.

The load-bearing test here is
:func:`test_recovering_reasoning_does_not_change_the_wire` — recovery is only
safe because replay still asks the preset's codec.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_transport import (
    PermissiveReasoningReader,
    arriving_reasoning,
)
from tolokaforge.core.models.trajectory import Message, MessageRole

pytestmark = pytest.mark.unit


class _Message:
    def __init__(self, reasoning_content=None, provider_specific_fields=None):
        self.reasoning_content = reasoning_content
        self.provider_specific_fields = provider_specific_fields


class TestWhatArrived:
    def test_reasoning_content(self) -> None:
        got = arriving_reasoning(_Message(reasoning_content="I read the log."))
        assert got.readable == ("reasoning_content",)
        assert got.opaque is False

    def test_the_openrouter_mirror(self) -> None:
        got = arriving_reasoning(_Message(provider_specific_fields={"reasoning": "thought"}))
        assert got.readable == ("reasoning",)

    def test_readable_details(self) -> None:
        got = arriving_reasoning(
            _Message(
                provider_specific_fields={
                    "reasoning_details": [{"type": "reasoning.text", "text": "a"}]
                }
            )
        )
        assert got.readable == ("reasoning_details",)

    def test_encrypted_only_is_not_readable(self) -> None:
        """The distinction the old token-based signal could not make.

        ``openai/o3`` bills reasoning tokens on every call and sends an opaque
        blob. Reporting that as a loss would cry wolf on the routes that are
        behaving correctly.
        """
        got = arriving_reasoning(
            _Message(
                provider_specific_fields={
                    "reasoning_details": [{"type": "reasoning.encrypted", "data": "x"}]
                }
            )
        )
        assert got.readable == ()
        assert got.opaque is True
        assert got.anything is True

    def test_readable_wins_when_a_blob_rides_alongside(self) -> None:
        """Grok emits a summary and an encrypted block in the same list."""
        got = arriving_reasoning(
            _Message(
                provider_specific_fields={
                    "reasoning_details": [
                        {"type": "reasoning.summary", "summary": "s"},
                        {"type": "reasoning.encrypted", "data": "rsn_..."},
                    ]
                }
            )
        )
        assert got.readable == ("reasoning_details",)
        assert got.opaque is False

    def test_nothing_at_all(self) -> None:
        got = arriving_reasoning(_Message())
        assert got.readable == ()
        assert got.opaque is False
        assert got.anything is False

    def test_a_malformed_message_reads_as_nothing(self) -> None:
        """Never raise: this runs on every call, and a mocked or odd response
        must not take down the turn that carried it."""
        assert arriving_reasoning(object()).anything is False
        assert arriving_reasoning(_Message(provider_specific_fields="not-a-dict")).anything is False
        assert (
            arriving_reasoning(
                _Message(provider_specific_fields={"reasoning_details": "not-a-list"})
            ).anything
            is False
        )


class TestThePermissiveReader:
    def test_recovers_each_readable_channel(self) -> None:
        got = PermissiveReasoningReader().extract(
            _Message(
                reasoning_content="first",
                provider_specific_fields={
                    "reasoning_details": [{"type": "reasoning.summary", "summary": "second"}]
                },
            )
        )
        assert got is not None
        assert [b.text for b in got.blocks] == ["first", "second"]

    def test_keeps_nothing_from_an_opaque_blob(self) -> None:
        got = PermissiveReasoningReader().extract(
            _Message(
                provider_specific_fields={
                    "reasoning_details": [{"type": "reasoning.encrypted", "data": "x"}]
                }
            )
        )
        assert got is None

    def test_does_not_raise_on_an_unfamiliar_block_type(self) -> None:
        """``GeminiReasoningCodec`` raises here, which is right for a codec that
        must round-trip a shape and wrong for a reader that must not lose text."""
        got = PermissiveReasoningReader().extract(
            _Message(
                reasoning_content="kept",
                provider_specific_fields={
                    "reasoning_details": [{"type": "reasoning.some_future_thing", "text": "?"}]
                },
            )
        )
        assert got is not None
        assert [b.text for b in got.blocks] == ["kept"]

    def test_does_not_double_count_the_mirror(self) -> None:
        """Routes that fill both channels repeat themselves."""
        got = PermissiveReasoningReader().extract(
            _Message(reasoning_content="once", provider_specific_fields={"reasoning": "once"})
        )
        assert got is not None
        assert len(got.blocks) == 1


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5",  # codec replays nothing
        "moonshotai/kimi-k2.7-code",  # codec replays summary blocks
        "google/gemini-3.1-pro-preview",  # codec *raises* on a summary block
        "anthropic/claude-sonnet-4.5",  # so does this one
    ],
)
def test_recovered_reasoning_never_reaches_the_wire(model: str) -> None:
    """The safety claim, on every codec family rather than a convenient one.

    A reader does not know which shape the route round-trips, so the blocks it
    builds are not ones the codec can necessarily encode. Gemini's and
    Anthropic's codecs raise ``ValueError`` on a ``summary_text`` block, and
    because the splice runs while assembling the *next* request, that raise
    would kill a turn rather than lose a capture.

    ``capture_only`` is what prevents it: the splice skips the reasoning
    entirely, so the serialised messages are identical with and without it.
    """
    client = LLMClient.__new__(LLMClient)
    client.capabilities = build_capabilities(model, "openrouter")

    recovered = PermissiveReasoningReader().extract(
        _Message(provider_specific_fields={"reasoning": "I should list files first."})
    )
    assert recovered is not None and recovered.capture_only

    with_reasoning = [Message(role=MessageRole.ASSISTANT, content="hi", reasoning=recovered)]
    without = [Message(role=MessageRole.ASSISTANT, content="hi")]

    assert client._convert_messages(None, with_reasoning) == client._convert_messages(None, without)
    # Nor is it a *dropped* replay: nothing promised to send it back.
    assert client._reasoning_replay_dropped_for(with_reasoning) is False


def test_the_splice_is_otherwise_live() -> None:
    """So the test above means something.

    Without this, a ``_convert_messages`` that ignored ``reasoning`` altogether
    would satisfy every assertion in it.
    """
    client = LLMClient.__new__(LLMClient)
    client.capabilities = build_capabilities("moonshotai/kimi-k2.7-code", "openrouter")

    extracted = StructuredReasoning(
        blocks=(ReasoningBlock(type="summary_text", text="what the codec read"),)
    )
    with_reasoning = [Message(role=MessageRole.ASSISTANT, content="hi", reasoning=extracted)]
    without = [Message(role=MessageRole.ASSISTANT, content="hi")]

    assert client._convert_messages(None, with_reasoning) != client._convert_messages(None, without)
