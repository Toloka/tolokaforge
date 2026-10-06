"""How many assistant turns replay their reasoning, and who gets to decide.

The codec decides what shape replayed reasoning takes; this policy decides how
many turns carry it, which is what costs input tokens on every later turn —
measured at 35% of one terminal-bench leg's input.

Two properties carry the weight here. A route that *mandates* replay must
override whatever a preset asked for, because dropping thinking blocks on such a
route is a provider error rather than a saving. And the no-op paths must return
the input list itself, so a leg whose model emits no reasoning allocates nothing.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
)
from tolokaforge.core.llm.reasoning_history import resolve_reasoning_history
from tolokaforge.core.models import Message, MessageRole

pytestmark = pytest.mark.unit


def _reasoning(text: str, *, capture_only: bool = False) -> StructuredReasoning:
    return StructuredReasoning(
        blocks=(ReasoningBlock(type="summary_text", text=text),), capture_only=capture_only
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
        reasoning_history=history, reasoning_codec=codec or OpenAIReasoningCodec()
    )


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
