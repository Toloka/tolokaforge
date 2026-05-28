"""Unit tests for ``tolokaforge.core.llm.reasoning`` dataclasses.

Exercises the *protocol* guarantees of :class:`StructuredReasoning`,
:class:`ReasoningBlock`, and :class:`ReasoningConfig`:

- frozen / immutable
- ``is_empty`` semantics
- ``as_plain_text`` concatenation order
- default factories
"""

from __future__ import annotations

import dataclasses

import pytest

from tolokaforge.core.llm.reasoning import (
    ReasoningBlock,
    ReasoningConfig,
    StructuredReasoning,
)

pytestmark = pytest.mark.unit


class TestReasoningConfig:
    def test_default_is_off(self) -> None:
        cfg = ReasoningConfig()
        assert cfg.mode == "off"
        assert cfg.budget_tokens is None
        assert cfg.effort_hint is None
        assert cfg.display == "visible"

    def test_frozen(self) -> None:
        cfg = ReasoningConfig(mode="adaptive", effort_hint="medium")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.mode = "off"  # type: ignore[misc]

    def test_accepts_budget_struct(self) -> None:
        cfg = ReasoningConfig(mode="budget", budget_tokens=8000)
        assert cfg.budget_tokens == 8000


class TestReasoningBlock:
    def test_required_fields(self) -> None:
        block = ReasoningBlock(type="thinking", text="hello")
        assert block.type == "thinking"
        assert block.text == "hello"
        assert block.signature is None
        assert block.encrypted_data is None

    def test_frozen(self) -> None:
        block = ReasoningBlock(type="thinking", text="hello")
        with pytest.raises(dataclasses.FrozenInstanceError):
            block.text = "nope"  # type: ignore[misc]


class TestStructuredReasoning:
    def test_default_is_empty(self) -> None:
        reasoning = StructuredReasoning()
        assert reasoning.is_empty()
        assert reasoning.as_plain_text() == ""
        assert reasoning.blocks == ()

    def test_is_empty_when_blocks_have_no_text_and_no_summary(self) -> None:
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text="", signature="sig"),)
        )
        assert reasoning.is_empty()

    def test_not_empty_when_summary_set(self) -> None:
        reasoning = StructuredReasoning(summary="one-liner")
        assert not reasoning.is_empty()
        assert reasoning.as_plain_text() == "one-liner"

    def test_not_empty_when_block_has_text(self) -> None:
        reasoning = StructuredReasoning(blocks=(ReasoningBlock(type="thinking", text="ok"),))
        assert not reasoning.is_empty()

    def test_as_plain_text_joins_non_empty_blocks(self) -> None:
        reasoning = StructuredReasoning(
            blocks=(
                ReasoningBlock(type="thinking", text="first"),
                ReasoningBlock(type="thinking", text=""),
                ReasoningBlock(type="redacted_thinking", text="second"),
            )
        )
        assert reasoning.as_plain_text() == "first\n\nsecond"

    def test_as_plain_text_falls_back_to_summary(self) -> None:
        reasoning = StructuredReasoning(
            blocks=(ReasoningBlock(type="thinking", text="", signature="sig"),),
            summary="short form",
        )
        assert reasoning.as_plain_text() == "short form"

    def test_round_trip_preserves_signatures(self) -> None:
        original = StructuredReasoning(
            blocks=(
                ReasoningBlock(
                    type="thinking",
                    text="step 1",
                    signature="sig-abc",
                ),
                ReasoningBlock(
                    type="redacted_thinking",
                    text="",
                    encrypted_data="opaque-payload",
                ),
            ),
            summary=None,
            budget_used=512,
        )
        # Frozen dataclasses support equality via value semantics
        same = StructuredReasoning(
            blocks=(
                ReasoningBlock(type="thinking", text="step 1", signature="sig-abc"),
                ReasoningBlock(type="redacted_thinking", text="", encrypted_data="opaque-payload"),
            ),
            summary=None,
            budget_used=512,
        )
        assert original == same
