"""How many assistant turns replay their reasoning, and who gets to decide.

The codec decides what shape replayed reasoning takes; this policy decides how
many turns carry it, which is what costs input tokens on every later turn —
measured at 35% of one terminal-bench leg's input.

Three properties carry the weight here. A route that *mandates* replay must
override whatever a preset asked for, because dropping thinking blocks on such a
route is a provider error rather than a saving. The no-op paths must return the
input list itself, so a leg whose model emits no reasoning allocates nothing.
And the policy must govern every turn the wire carries, including the ones whose
reasoning a human cannot read — an opaque block costs the same tokens as a
readable one, so ``none`` that spared it would not be ``none``.

The route under test is the Gemini codec rather than the OpenAI one: OpenAI
replays nothing at all, so a policy applied over it is unobservable on the wire
and cannot tell a working filter from a broken one.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    GeminiReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
)
from tolokaforge.core.llm.reasoning_history import resolve_reasoning_history
from tolokaforge.core.models import Message, MessageRole

pytestmark = pytest.mark.unit

# Long enough to clear ``GeminiReasoningCodec._PLACEHOLDER_LENGTH_THRESHOLD``,
# so the codec treats it as a real opaque payload and replays it.
_OPAQUE_PAYLOAD = "Zm9v" * 400
# OpenRouter's constant no-real-thinking marker: payload-shaped, 48 chars, and
# dropped by the codec on replay.
_PLACEHOLDER_PAYLOAD = "e" * 48


def _reasoning(text: str, *, capture_only: bool = False) -> StructuredReasoning:
    return StructuredReasoning(
        blocks=(ReasoningBlock(type="thinking", text=text),), capture_only=capture_only
    )


def _opaque(data: str = _OPAQUE_PAYLOAD) -> StructuredReasoning:
    """Reasoning with no readable text that still carries a wire payload."""
    return StructuredReasoning(
        blocks=(ReasoningBlock(type="redacted_thinking", text="", encrypted_data=data),)
    )


def _assistant(text: str, reasoning: StructuredReasoning | None) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, reasoning=reasoning)


def _history() -> list[Message]:
    return [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("thought one")),
        Message(role=MessageRole.TOOL, content="out", tool_call_id="c"),
        _assistant("two", _reasoning("thought two")),
        Message(role=MessageRole.TOOL, content="out", tool_call_id="c"),
        _assistant("three", _reasoning("thought three")),
    ]


def _caps(history: str, codec: object = None) -> ModelCapabilities:
    return ModelCapabilities(
        reasoning_history=history, reasoning_codec=codec or GeminiReasoningCodec()
    )


def _wire(messages: list[Message], capabilities: ModelCapabilities) -> list[dict[str, Any]]:
    """The request bodies, assembled the way ``_generate_once`` assembles them."""
    client = LLMClient.__new__(LLMClient)
    client.capabilities = capabilities
    return client._convert_messages(None, resolve_reasoning_history(messages, capabilities))


def test_all_returns_the_input_list_untouched() -> None:
    wire = _history()

    assert resolve_reasoning_history(wire, _caps("all")) is wire


def test_none_drops_every_assistant_turn_s_reasoning() -> None:
    wire = _history()

    out = resolve_reasoning_history(wire, _caps("none"))

    assert [m.reasoning for m in out if m.role is MessageRole.ASSISTANT] == [None, None, None]
    # The recorded list is untouched — only the wire copy is affected.
    assert all(m.reasoning is not None for m in wire if m.role is MessageRole.ASSISTANT)


def test_last_keeps_only_the_most_recent_carrier() -> None:
    wire = _history()

    out = resolve_reasoning_history(wire, _caps("last"))

    carried = [m.reasoning is not None for m in out if m.role is MessageRole.ASSISTANT]
    assert carried == [False, False, True]
    # Untouched messages keep their identity, so the cached prefix is byte-identical
    # up to the first message the policy rewrote.
    assert out[0] is wire[0]


def test_a_route_that_mandates_replay_overrides_the_preset() -> None:
    wire = _history()

    # Anthropic requires thinking blocks to round-trip alongside tool results,
    # so asking for "none" here would be a 400, not a saving.
    out = resolve_reasoning_history(wire, _caps("none", AnthropicReasoningCodec()))

    assert out is wire


def test_capture_only_reasoning_is_not_treated_as_a_carrier() -> None:
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("replayable")),
        _assistant("two", _reasoning("read, not replayable", capture_only=True)),
    ]

    out = resolve_reasoning_history(wire, _caps("last"))

    # The genuinely replayable turn is the last carrier, so it survives. Counting
    # the capture-only message instead would have stripped it and sent nothing.
    assert out[1].reasoning is not None


def test_empty_reasoning_is_not_treated_as_a_carrier() -> None:
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("real thought")),
        _assistant("two", StructuredReasoning()),
    ]

    out = resolve_reasoning_history(wire, _caps("last"))

    assert out[1].reasoning is not None


def test_a_history_carrying_no_reasoning_is_returned_unchanged() -> None:
    wire = [Message(role=MessageRole.USER, content="task"), _assistant("one", None)]

    assert resolve_reasoning_history(wire, _caps("none")) is wire


def test_auto_defers_to_the_route_and_defaults_to_full_replay() -> None:
    wire = _history()

    for codec in (OpenAIReasoningCodec(), NoReasoningCodec(), AnthropicReasoningCodec()):
        assert resolve_reasoning_history(wire, _caps("auto", codec)) is wire


def test_text_less_reasoning_that_carries_a_payload_is_governed_by_the_policy() -> None:
    """``none`` means none, including the turns a human cannot read.

    A ``redacted_thinking`` block has no text and a full opaque payload. It
    costs input tokens on every later turn exactly like readable thinking, so a
    policy that skipped it would bill the operator for the saving they asked
    for.
    """
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _opaque()),
    ]
    caps = _caps("none")

    out = resolve_reasoning_history(wire, caps)

    assert out[1].reasoning is None
    assert all("reasoning_details" not in body for body in _wire(wire, caps))


def test_last_keeps_the_turn_the_wire_carries_not_the_turn_that_encodes_to_nothing() -> None:
    """The degenerate case the policy exists to avoid.

    OpenRouter's no-real-thinking placeholder is payload-shaped but the codec
    drops it on replay. Counting it as the most recent carrier would keep the
    turn that encodes to nothing and strip the one that does not, sending no
    reasoning at all under a setting that promised one turn of it.
    """
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("a real thought")),
        _assistant("two", _opaque(_PLACEHOLDER_PAYLOAD)),
    ]
    caps = _caps("last")

    out = resolve_reasoning_history(wire, caps)

    assert out[1].reasoning is not None
    assert [b["text"] for body in _wire(wire, caps) for b in body.get("reasoning_details", ())] == [
        "a real thought"
    ]


def test_an_anthropic_route_replays_everything_whatever_the_setting_says() -> None:
    """Thinking blocks — redacted ones included — must round-trip alongside tool
    results on Anthropic, so the route's ``forced_history`` short-circuits the
    policy before any turn can be dropped."""
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("a real thought")),
        _assistant("two", _opaque()),
    ]

    for setting in ("none", "last", "auto", "all"):
        caps = _caps(setting, AnthropicReasoningCodec())

        assert resolve_reasoning_history(wire, caps) is wire
        spliced = [body for body in _wire(wire, caps) if "thinking_blocks" in body]
        assert len(spliced) == 2


def test_a_mix_of_readable_and_opaque_turns_is_ranked_by_what_reaches_the_wire() -> None:
    """The live Gemini shape: one model emitting both block kinds.

    Coherent here means the policy ranks turns by the payload they put on the
    wire, never by whether that payload is readable. ``none`` leaves nothing
    behind on either kind, and ``last`` keeps the final turn that carries one —
    the opaque turn, because it is the most recent one the provider will be
    sent, not the most recent one a log reader can follow.
    """
    wire = [
        Message(role=MessageRole.USER, content="task"),
        _assistant("one", _reasoning("a readable thought")),
        _assistant("two", _opaque()),
    ]

    none_out = resolve_reasoning_history(wire, _caps("none"))
    assert [m.reasoning for m in none_out if m.role is MessageRole.ASSISTANT] == [None, None]
    assert all("reasoning_details" not in body for body in _wire(wire, _caps("none")))

    last_out = resolve_reasoning_history(wire, _caps("last"))
    assert last_out[1].reasoning is None
    assert last_out[2].reasoning is not None
    carried = [b for body in _wire(wire, _caps("last")) for b in body.get("reasoning_details", ())]
    assert [b["type"] for b in carried] == ["reasoning.encrypted"]
